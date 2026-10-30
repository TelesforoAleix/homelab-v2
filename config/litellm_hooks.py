"""Proxy policy and content-free HTTP audit; routing stays in litellm.yaml."""

from __future__ import annotations

import hashlib
import json
import logging
import os
import re
import sys
from contextvars import ContextVar
from datetime import UTC, datetime
from decimal import Decimal, InvalidOperation
from pathlib import Path
from time import monotonic

import httpx
import yaml
from fastapi import HTTPException
from litellm.constants import RETURN_RAW_MODEL_NAME_METADATA_KEY
from litellm.integrations.custom_logger import CustomLogger
from litellm.llms.custom_httpx.http_handler import AsyncHTTPHandler
from litellm.proxy.route_llm_request import ProxyModelNotFoundError
from openai import AsyncOpenAI
from openai.types.chat.completion_create_params import CompletionCreateParamsBase
from openai.types.embedding_create_params import EmbeddingCreateParams
from openai.types.image_edit_params import ImageEditParamsBase
from openai.types.image_generate_params import ImageGenerateParamsBase
from starlette.middleware.base import BaseHTTPMiddleware
from starlette.responses import JSONResponse, Response

# Read the single config, rather than duplicating its aliases or real model names in code.
config = yaml.safe_load(Path(os.environ["CONFIG_FILE_PATH"]).read_text())
model_info = {row["model_name"]: row.get("model_info", {}) for row in config["model_list"]}
model_specs = {row["model_name"]: row["litellm_params"] for row in config["model_list"]}
# LiteLLM executes this file again when loading callbacks. Reuse the context
# installed by the middleware instead of creating an isolated copy.
audit: ContextVar[dict | None] = getattr(sys.modules.get(__name__), "audit", None) or ContextVar(
    "model_call_audit", default=None
)
logger = logging.getLogger("homelab.models")
MAC_LOAD_FAILURE = "The Mac could not load this model now (often for lack of memory); try later"


def usd_amount(value):
    if isinstance(value, (bool, dict, list)) or value is None:
        return None
    try:
        amount = Decimal(str(value))
        if amount.is_finite() and amount >= 0 and (amount == 0 or -18 <= amount.adjusted() <= 18):
            return amount
    except (InvalidOperation, ValueError):
        pass
    return None


def image_event(event):
    return (
        event is not None and model_info.get(event["purpose"], {}).get("mode") == "image_generation"
    )


def capture_metadata(body, event):
    if not isinstance(body, dict):
        return
    metadata = body.get("providerMetadata", body.get("provider_metadata", {}))
    if not isinstance(metadata, dict) or not isinstance(metadata.get("gateway"), dict):
        return
    gateway = metadata["gateway"]
    # gatewayCost is the actual balance debit, including surcharges. The direct
    # image proof also returned the identical amount in cost; inferenceCost is not billed cost.
    amount = usd_amount(gateway.get("gatewayCost"))
    if amount is None:
        amount = usd_amount(gateway.get("cost"))
    if amount is not None:
        event["vercel_metadata_cost"] = amount


def strip_provider_metadata(value):
    if isinstance(value, dict):
        return {
            key: strip_provider_metadata(item)
            for key, item in value.items()
            if key not in {"providerMetadata", "provider_metadata"}
        }
    if isinstance(value, list):
        return [strip_provider_metadata(item) for item in value]
    return value


def clean_image_body(raw, content_type, event):
    if "text/event-stream" not in content_type:
        body = json.loads(raw)
        capture_metadata(body, event)
        return json.dumps(strip_provider_metadata(body), separators=(",", ":")).encode()
    blocks = []
    for block in raw.decode().replace("\r\n", "\n").split("\n\n"):
        lines = block.splitlines()
        data = "\n".join(line[5:].lstrip(" ") for line in lines if line.startswith("data:"))
        if data and data != "[DONE]":
            body = json.loads(data)
            capture_metadata(body, event)
            lines = [line for line in lines if not line.startswith("data:")]
            lines.append(
                "data: " + json.dumps(strip_provider_metadata(body), separators=(",", ":"))
            )
        if lines:
            blocks.append("\n".join(lines))
    return ("\n\n".join(blocks) + "\n\n").encode()


async def capture_image_response(response):
    event = audit.get()
    if not image_event(event):
        return
    # No billed-cost header was present in the live proof. Recognize the explicit
    # gateway cost header if supplied; never treat LiteLLM's estimate as a provider debit.
    amount = usd_amount(response.headers.get("x-vercel-ai-gateway-cost"))
    if amount is not None:
        event["vercel_header_cost"] = amount
    event["cost_month"] = datetime.now(UTC).date().replace(day=1)
    raw = await response.aread()
    try:
        # Observe before the SDK's schema validation: an HTTP 200 with an unusable
        # image (or a rejected ImageResponse schema) may already have been charged.
        clean_image_body(raw, response.headers.get("content-type", ""), event)
    except (ValueError, UnicodeError):
        pass


async def persist_image_cost(key, cost, month):
    import psycopg

    async with await psycopg.AsyncConnection.connect(
        os.environ["HOMELAB_DATABASE_URL"], connect_timeout=5
    ) as connection:
        await connection.execute(
            """INSERT INTO homelab_image_costs (client_key_hash, month, total_usd, charged_calls)
            VALUES (%s, %s, %s, 1)
            ON CONFLICT (client_key_hash, month) DO UPDATE
            SET total_usd = homelab_image_costs.total_usd + EXCLUDED.total_usd,
                charged_calls = homelab_image_costs.charged_calls + 1""",
            (hashlib.sha256(key.encode()).hexdigest(), month, cost),
        )


async def ensure_image_accounting():
    import psycopg

    async with await psycopg.AsyncConnection.connect(
        os.environ["HOMELAB_DATABASE_URL"], connect_timeout=5
    ) as connection:
        await connection.execute("SELECT 1 FROM homelab_image_costs LIMIT 0")


async def finish_image_response(response, event, request):
    computed = usd_amount(response.headers.get("x-litellm-response-cost"))
    status = response.status_code
    try:
        raw = (
            b"".join([part async for part in response.body_iterator])
            if hasattr(response, "body_iterator")
            else response.body
        )
    except Exception:
        raw, status = b"", 502
    headers = {
        name: value
        for name, value in response.headers.items()
        if name.lower() in {"content-type", "cache-control", "retry-after"}
    }
    if status < 400:
        try:
            raw = clean_image_body(raw, headers.get("content-type", ""), event)
            response = Response(raw, status_code=status, headers=headers)
        except (ValueError, UnicodeError):
            status = 502
    if status >= 400:
        response = (
            error_response(
                402,
                "Monthly image budget exhausted; resets on the 1st at 00:00 UTC",
                "quota_for_entity_exceeded",
            )
            if status == 402
            else error_response(status, "Image request failed")
        )
        for name in ("retry-after", "cache-control"):
            if name in headers:
                response.headers[name] = headers[name]
    cost = event.get("vercel_header_cost", event.get("vercel_metadata_cost"))
    if cost is None and status < 400 and computed is not None and computed > 0:
        cost = computed
    if cost is not None and (status < 400 or cost > 0):
        _, _, key = request.headers.get("authorization", "").partition(" ")
        try:
            await persist_image_cost(
                key, cost, event.get("cost_month", datetime.now(UTC).date().replace(day=1))
            )
        except Exception:
            # Return the confirmed charge even if persistence fails; never encourage a
            # silent retry of an already billed call or emit database diagnostics.
            logger.error("image_cost_record_failed cost_usd=%s", format(cost, "f"))
            response = error_response(503, "Image cost could not be recorded")
        response.headers["x-homelab-cost-usd"] = format(cost, "f")
    return response


def mac_load_failed(purpose, exception):
    # Only the native router's failed-load signal on configured Mac routes. Never infer
    # memory pressure, inspect model memory, or repeat an upstream diagnostic to a client.
    _, _, info = route(purpose)
    return "connect_timeout" in info and bool(
        re.search(r"model name=[A-Za-z0-9_.-]+ failed to load", str(exception))
    )


def route(purpose):
    """Only configured Mac patterns expand; no global wildcard or mutable route table."""
    if not isinstance(purpose, str):
        return None, {}, {}
    if purpose in model_specs and "*" not in purpose:
        return purpose, model_specs[purpose], model_info[purpose]
    for pattern, spec in model_specs.items():
        if not pattern.endswith("-*") or "connect_timeout" not in model_info[pattern]:
            continue
        prefix = pattern[:-1]
        if purpose.startswith(prefix) and re.fullmatch(
            r"[A-Za-z0-9][A-Za-z0-9_.-]{0,95}", purpose[len(prefix) :]
        ):
            return pattern, {**spec, "model": "openai/" + purpose}, model_info[pattern]
    return None, {}, {}


def input_type_error(data):
    if "input_type" not in data:
        return None
    role = data["input_type"]
    if not isinstance(role, str) or role not in {"query", "passage"}:
        return "input_type must be query or passage"
    text = data.get("input")
    if not isinstance(text, str) and not (
        isinstance(text, list) and text and all(isinstance(item, str) for item in text)
    ):
        return "input_type requires string or list-of-string input"
    return None


def error_response(status: int, message: str, error_type="invalid_request_error"):
    return JSONResponse(
        {
            "error": {
                "message": message,
                "type": error_type,
                "param": None,
                "code": None,
            }
        },
        status_code=status,
    )


def profile_fields(info):
    """Published fields only; dotted setting paths denote nested JSON objects."""
    return {
        *(item["name"] for item in info.get("advanced", [])),
        *(
            info[key]["field"]
            for key in ("aspect_ratios", "resolutions", "qualities")
            if key in info
        ),
    }


def nested_fields_allowed(value, paths):
    if not isinstance(value, dict):
        return False
    for key, item in value.items():
        if key in paths:
            if isinstance(item, (dict, list)):
                return False
            continue
        children = {path[len(key) + 1 :] for path in paths if path.startswith(key + ".")}
        if not children or not nested_fields_allowed(item, children):
            return False
    return True


class ModelEndpointMiddleware(BaseHTTPMiddleware):
    """Expose only the contract; observe the full response, including streams."""

    async def dispatch(self, request, call_next):
        path = request.url.path
        if path == "/health/liveliness" and request.method == "GET":
            return await call_next(request)

        started = monotonic()
        event = {
            "purpose": "unknown",
            "real_model": "none",
            "upstream": False,
            "image_edit": path == "/v1/images/edits",
        }
        token = audit.set(event)
        status = 500

        def record():
            logger.info(
                "model_call purpose=%s real_model=%s status=%d elapsed_seconds=%.3f "
                "upstream=%s profile_blocked=%s profile_paused=%s",
                event["purpose"],
                event["real_model"],
                status,
                monotonic() - started,
                str(event["upstream"]).lower(),
                str(event.get("profile_blocked", False)).lower(),
                str(event.get("profile_paused", False)).lower(),
            )

        try:
            fields = None
            if path == "/v1/models" and request.method == "GET":
                event["purpose"] = "models"
            elif path == "/v1/images/cost" and request.method == "GET":
                event["purpose"] = "image-cost"
            elif path == "/v1/model/info" and request.method == "GET":
                event["purpose"] = "model-info"
            elif path == "/v1/images/edits" and request.method == "POST":
                fields = set(ImageEditParamsBase.__annotations__) | {"image[]", "mask[]", "stream"}
            elif path == "/v1/images/generations" and request.method == "POST":
                fields = set(ImageGenerateParamsBase.__annotations__) | {"stream"}
            elif path == "/v1/chat/completions" and request.method == "POST":
                fields = set(CompletionCreateParamsBase.__annotations__) | {"stream"}
            elif path == "/v1/embeddings" and request.method == "POST":
                fields = set(EmbeddingCreateParams.__annotations__) | {"input_type"}
            else:
                response = error_response(404, "Endpoint not offered")
                status = response.status_code
                record()
                return response

            if request.query_params:
                response = error_response(400, "Query parameters are not offered")
            elif fields is not None:
                try:
                    if path == "/v1/images/edits":
                        # Cache the bytes before parsing: LiteLLM must receive the original
                        # multipart body, including every repeated image[] part.
                        await request.body()
                        async with request.form() as form:
                            body = dict(form)
                    else:
                        body = await request.json()
                except (ValueError, UnicodeError):
                    body = None
                if not isinstance(body, dict):
                    response = error_response(400, "Expected a JSON object")
                else:
                    purpose = body.get("model")
                    key, spec, info = route(purpose)
                    if key is not None:
                        # Audit purpose stays bounded even for refused arbitrary model ids.
                        event["purpose"] = key[:-2] if key.endswith("-*") else key
                        event["requested_model"] = purpose
                        if not key.endswith("-*"):
                            event["real_model"] = spec["model"].split("/", 1)[1]
                    image_entry = info.get("mode") == "image_generation"
                    published = profile_fields(info) if image_entry else set()
                    fields |= {field.split(".", 1)[0] for field in published}
                    if image_entry:
                        fields.discard("seed")
                    # SDK fields define the OpenAI shape. LiteLLM-specific provider/routing
                    # overrides must never reach the library, even before its pre-call hook.
                    if set(body) - set(fields):
                        response = error_response(400, "Only OpenAI request fields are offered")
                    elif "providerOptions" in body and not nested_fields_allowed(
                        body["providerOptions"],
                        {
                            field.removeprefix("providerOptions.")
                            for field in published
                            if field.startswith("providerOptions.")
                        },
                    ):
                        response = error_response(400, "Only published image settings are offered")
                    elif path.startswith("/v1/images/") and not image_entry:
                        response = error_response(400, "An image purpose is required")
                    elif path.startswith("/v1/images/") and info["endpoint"] != "images/edits":
                        response = error_response(400, "This image purpose uses chat/completions")
                    elif (
                        image_entry
                        and info["prompt_family"] == "openai"
                        and body.get("response_format", "b64_json") != "b64_json"
                    ):
                        response = error_response(400, "This image purpose returns b64_json")
                    elif path == "/v1/embeddings" and (error := input_type_error(body)):
                        # The proxy decodes some token arrays before its pre-call hook.
                        response = error_response(400, error)
                    elif image_entry and (
                        request.headers.get("authorization", "").split(" ", 1)[0].lower()
                        != "bearer"
                        or not request.headers.get("authorization", "").partition(" ")[2].strip()
                    ):
                        response = error_response(401, "Bearer client key required")
                    else:
                        # Client keys are accepted but never forwarded upstream. Drop custom
                        # headers, including LiteLLM routing and debug controls.
                        request.scope["headers"] = [
                            (name, value)
                            for name, value in request.scope["headers"]
                            if name in {b"authorization", b"content-type", b"accept", b"user-agent"}
                        ]
                        response = await call_next(request)
            else:
                if path == "/v1/images/cost":
                    async with httpx.AsyncClient(trust_env=False, timeout=10) as client:
                        result = await client.get(
                            os.environ["HOMELAB_IMAGE_COST_API_URL"],
                            headers={"authorization": request.headers.get("authorization", "")},
                        )
                    response = (
                        JSONResponse(result.json())
                        if result.status_code == 200
                        else error_response(result.status_code, "Image cost total unavailable")
                    )
                elif path == "/v1/model/info":
                    # Build the allow-list projection from the route table. Never serialize
                    # LiteLLM's expanded deployment entries (URLs, key references, metadata).
                    response = JSONResponse(
                        {
                            "data": [
                                {"model_name": name, "model_info": info}
                                for name, info in model_info.items()
                            ]
                        }
                    )
                else:
                    response = await call_next(request)

            status = response.status_code
            if event.get("profile_blocked"):
                # Only our own observed missing preset can produce this fixed refusal.
                if hasattr(response, "body_iterator"):
                    async for _ in response.body_iterator:
                        pass
                response = error_response(
                    503,
                    (
                        f"{event['purpose']} is paused because the Mac is on battery"
                        if event.get("profile_paused")
                        else f"{event['purpose']} is not available in this profile"
                    ),
                    "service_unavailable_error",
                )
                status = 503
            if event.get("model_load_failed"):
                if hasattr(response, "body_iterator"):
                    async for _ in response.body_iterator:
                        pass
                response = error_response(503, MAC_LOAD_FAILURE, "service_unavailable_error")
                status = 503
            if image_event(event):
                response = await finish_image_response(response, event, request)
                status = response.status_code
            # Provider URLs and deployment diagnostics are not part of the client contract.
            for name in list(response.headers):
                if name.lower() not in {
                    "content-type",
                    "content-length",
                    "cache-control",
                    "retry-after",
                    *(["x-homelab-cost-usd"] if image_event(event) else []),
                }:
                    del response.headers[name]

            if hasattr(response, "body_iterator"):
                iterator = response.body_iterator

                async def observed():
                    nonlocal status
                    try:
                        async for chunk in iterator:
                            yield chunk
                    except Exception:
                        status = 502
                        raise
                    finally:
                        record()

                response.body_iterator = observed()
            else:
                record()
            return response
        except Exception:
            if image_event(event):
                response = await finish_image_response(
                    error_response(500, "Image request failed"), event, request
                )
                status = response.status_code
                record()
                return response
            record()
            return error_response(500, "Model endpoint failed")
        finally:
            if event.get("image_client") is not None:
                await event["image_client"].close()
            audit.reset(token)


class PurposeHooks(CustomLogger):
    async def async_pre_call_hook(self, user_api_key_dict, cache, data, call_type):
        purpose = data.get("model")
        key, spec, info = route(purpose)
        if key is None:
            # Use LiteLLM's own default refusal, including for deployment names and ids.
            raise ProxyModelNotFoundError(
                route="/v1/embeddings" if call_type == "embeddings" else "/v1/chat/completions",
                model_name=purpose if isinstance(purpose, str) else "",
            )
        metadata = data.setdefault("metadata", {})
        embedding = info.get("mode") == "embedding"
        metadata[RETURN_RAW_MODEL_NAME_METADATA_KEY] = embedding or key in {"vision", "vision-*"}
        if call_type in {"embeddings", "aembedding"} and "input_type" in data:
            if not embedding:
                raise HTTPException(400, "input_type requires an embedding purpose")
            if error := input_type_error(data):
                raise HTTPException(400, error)
            role = data.pop("input_type")
            text = data.get("input")
            prefix = info["input_prefixes"][role]
            data["input"] = (
                prefix + text if isinstance(text, str) else [prefix + item for item in text]
            )
        if "reasoning_effort" in spec:
            data["reasoning_effort"] = spec["reasoning_effort"]
        # Token accounting can fetch image URLs. Data images require no remote fetch.
        for message in data.get("messages", []):
            content = message.get("content")
            if isinstance(content, list):
                for part in content:
                    if part.get("type") == "image_url":
                        url = part.get("image_url", {}).get("url", "")
                        if not url.startswith("data:"):
                            raise HTTPException(400, "Remote image URLs are not offered")
        return data

    async def async_pre_call_deployment_hook(self, kwargs, call_type):
        event = audit.get()
        if event is not None and "connect_timeout" in model_info.get(event["purpose"], {}):
            # The pinned proxy accepts numeric YAML timeouts; OpenAI's SDK needs an
            # httpx.Timeout for a separate connect limit on an on-demand Mac route.
            purpose = event.get("requested_model", event["purpose"])
            key, spec, info = route(purpose)
            url_variable = spec["api_base"].removeprefix("os.environ/")
            if not os.environ.get(url_variable):
                raise HTTPException(503, "Model call failed")
            # Native preset omission enforces policy at the Mac. Inspect only model ids;
            # never forward upstream diagnostics or accept policy supplied by a client.
            base = os.environ[url_variable].removesuffix("/v1").rstrip("/")
            timeout = httpx.Timeout(3.0, connect=info["connect_timeout"])
            async with httpx.AsyncClient(trust_env=False, timeout=timeout) as client:
                response = await client.get(base + "/models")
                response.raise_for_status()
                available = {
                    name
                    for row in response.json()["data"]
                    for name in [row["id"], *row.get("aliases", [])]
                }
                model = spec["model"].split("/", 1)[1]
                if model not in available:
                    # A native static file carries one boolean, never private profile contents.
                    try:
                        state = await client.get(base + "/homelab-profile.json")
                        if state.status_code == 200:
                            value = state.json()
                            event["profile_paused"] = (
                                isinstance(value, dict) and value.get("paused_on_battery") is True
                            )
                    except (httpx.RequestError, ValueError):
                        pass  # Old routers/missing metadata retain the fixed availability refusal.
            if model not in available:
                event["profile_blocked"] = True
                raise HTTPException(503, "Model call failed")
            kwargs["timeout"] = httpx.Timeout(spec["timeout"], connect=info["connect_timeout"])
        if image_event(event):
            # Use each native handler's supported client type. The libraries still
            # own authentication, multipart encoding and response parsing.
            if event.get("image_client") is None:
                try:
                    await ensure_image_accounting()
                except Exception:
                    raise HTTPException(503, "Image accounting unavailable") from None
                _, spec, info = route(event["purpose"])
                api_key = spec["api_key"]
                if api_key.startswith("os.environ/"):
                    api_key = os.environ[api_key.removeprefix("os.environ/")]
                if not event["image_edit"]:
                    event["image_client"] = AsyncOpenAI(
                        api_key=api_key,
                        base_url=spec["api_base"],
                        timeout=spec["timeout"],
                        max_retries=0,
                        http_client=httpx.AsyncClient(
                            trust_env=False, event_hooks={"response": [capture_image_response]}
                        ),
                    )
                else:
                    event["image_client"] = AsyncHTTPHandler(
                        timeout=spec["timeout"],
                        event_hooks={"response": [capture_image_response]},
                    )
            kwargs["client"] = event["image_client"]
        if event is not None:
            event["upstream"] = True
        return kwargs

    async def async_post_call_success_hook(self, data, user_api_key_dict, response):
        purpose = data.get("model")
        key, spec, info = route(purpose)
        if info.get("mode") == "embedding":
            served = getattr(response, "_hidden_params", {}).get(
                "provider_response_model"
            ) or getattr(response, "model", "")
            if key in {"embed-large-bulk", "embed-large-bulk-*"} and served.startswith(
                "embed-large-bulk-"
            ):
                response.model = served.removeprefix("embed-large-bulk-")
            elif key.endswith("-*"):
                response.model = purpose.removeprefix(key[:-1])
            else:
                response.model = spec["model"].split("/", 1)[1]
        elif key in {"vision", "vision-*"}:
            self.record_vision_model(response)
            response.model = purpose
        return response

    @staticmethod
    def record_vision_model(response):
        event = audit.get()
        # The pinned streaming wrapper puts the upstream name in hidden metadata.
        served = getattr(response, "_hidden_params", {}).get("provider_response_model") or getattr(
            response, "model", None
        )
        # LiteLLM can append a synthetic final chunk carrying the purpose alias.
        if event is not None and isinstance(served, str) and served and served != event["purpose"]:
            event["real_model"] = served

    async def async_post_call_streaming_iterator_hook(
        self, user_api_key_dict, response, request_data
    ):
        async for chunk in response:
            purpose = request_data.get("model")
            if route(purpose)[0] in {"vision", "vision-*"}:
                self.record_vision_model(chunk)
                chunk.model = purpose
            yield chunk

    async def async_post_call_failure_hook(
        self, request_data, original_exception, user_api_key_dict, traceback_str=None
    ):
        if isinstance(original_exception, ProxyModelNotFoundError):
            return None
        if mac_load_failed(request_data.get("model"), original_exception):
            event = audit.get()
            if event is not None:
                event["model_load_failed"] = True
            return HTTPException(503, MAC_LOAD_FAILURE)
        # Gateway exceptions can include provider names, bodies and credentials. Preserve
        # the numeric status, but let LiteLLM format a content-free OpenAI error.
        status = getattr(original_exception, "status_code", 502)
        if not isinstance(status, int) or not 400 <= status <= 599:
            status = 502
        return HTTPException(status, "Model call failed")


hooks = PurposeHooks()
