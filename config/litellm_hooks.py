"""Proxy policy and content-free HTTP audit; routing stays in litellm.yaml."""

from __future__ import annotations

import logging
import os
import sys
from contextvars import ContextVar
from pathlib import Path
from time import monotonic

import httpx
import yaml
from fastapi import HTTPException
from litellm.constants import RETURN_RAW_MODEL_NAME_METADATA_KEY
from litellm.integrations.custom_logger import CustomLogger
from litellm.proxy.route_llm_request import ProxyModelNotFoundError
from openai.types.chat.completion_create_params import CompletionCreateParamsBase
from openai.types.embedding_create_params import EmbeddingCreateParams
from openai.types.image_edit_params import ImageEditParamsBase
from openai.types.image_generate_params import ImageGenerateParamsBase
from starlette.middleware.base import BaseHTTPMiddleware
from starlette.responses import JSONResponse

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
        event = {"purpose": "unknown", "real_model": "none", "upstream": False}
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
                    if isinstance(purpose, str) and purpose in model_specs:
                        event["purpose"] = purpose
                        event["real_model"] = model_specs[purpose]["model"].split("/", 1)[1]
                    info = model_info.get(purpose, {}) if isinstance(purpose, str) else {}
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
                if path == "/v1/model/info":
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
                        else f"{event['purpose']} is not available until Inference"
                    ),
                    "service_unavailable_error",
                )
                status = 503
            if (
                event["purpose"] in model_info
                and model_info[event["purpose"]].get("mode") == "image_generation"
                and status >= 400
            ):
                # Image handlers do not all use the failure hook's returned exception.
                # Discard their response body too, so provider diagnostics cannot escape.
                if hasattr(response, "body_iterator"):
                    async for _ in response.body_iterator:
                        pass
                response = (
                    error_response(
                        402,
                        "Monthly image budget exhausted; resets on the 1st at 00:00 UTC",
                        "quota_for_entity_exceeded",
                    )
                    if status == 402
                    else error_response(status, "Image request failed")
                )
            # Provider URLs and deployment diagnostics are not part of the client contract.
            for name in list(response.headers):
                if name.lower() not in {
                    "content-type",
                    "content-length",
                    "cache-control",
                    "retry-after",
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
            record()
            return error_response(500, "Model endpoint failed")
        finally:
            audit.reset(token)


class PurposeHooks(CustomLogger):
    async def async_pre_call_hook(self, user_api_key_dict, cache, data, call_type):
        purpose = data.get("model")
        if not isinstance(purpose, str) or purpose not in model_specs:
            # Use LiteLLM's own default refusal, including for deployment names and ids.
            raise ProxyModelNotFoundError(
                route="/v1/embeddings" if call_type == "embeddings" else "/v1/chat/completions",
                model_name=purpose if isinstance(purpose, str) else "",
            )
        spec = model_specs[purpose]
        metadata = data.setdefault("metadata", {})
        embedding = model_info[purpose].get("mode") == "embedding"
        metadata[RETURN_RAW_MODEL_NAME_METADATA_KEY] = embedding or purpose == "vision"
        if call_type in {"embeddings", "aembedding"} and "input_type" in data:
            if not embedding:
                raise HTTPException(400, "input_type requires an embedding purpose")
            if error := input_type_error(data):
                raise HTTPException(400, error)
            role = data.pop("input_type")
            text = data.get("input")
            prefix = model_info[purpose]["input_prefixes"][role]
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
            purpose = event["purpose"]
            url_variable = model_specs[purpose]["api_base"].removeprefix("os.environ/")
            if not os.environ.get(url_variable):
                raise HTTPException(503, "Model call failed")
            # Native preset omission enforces policy at the Mac. Inspect only model ids;
            # never forward upstream diagnostics or accept policy supplied by a client.
            base = os.environ[url_variable].removesuffix("/v1").rstrip("/")
            timeout = httpx.Timeout(3.0, connect=model_info[purpose]["connect_timeout"])
            async with httpx.AsyncClient(trust_env=False, timeout=timeout) as client:
                response = await client.get(base + "/models")
                response.raise_for_status()
                available = {row["id"] for row in response.json()["data"]}
                model = model_specs[purpose]["model"].split("/", 1)[1]
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
            kwargs["timeout"] = httpx.Timeout(
                model_specs[purpose]["timeout"], connect=model_info[purpose]["connect_timeout"]
            )
        if event is not None:
            event["upstream"] = True
        return kwargs

    async def async_post_call_success_hook(self, data, user_api_key_dict, response):
        purpose = data.get("model")
        if model_info.get(purpose, {}).get("mode") == "embedding":
            response.model = model_specs[purpose]["model"].split("/", 1)[1]
        elif purpose == "vision":
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
            if request_data.get("model") == "vision":
                self.record_vision_model(chunk)
                chunk.model = "vision"
            yield chunk

    async def async_post_call_failure_hook(
        self, request_data, original_exception, user_api_key_dict, traceback_str=None
    ):
        if isinstance(original_exception, ProxyModelNotFoundError):
            return None
        # Gateway exceptions can include provider names, bodies and credentials. Preserve
        # the numeric status, but let LiteLLM format a content-free OpenAI error.
        status = getattr(original_exception, "status_code", 502)
        if not isinstance(status, int) or not 400 <= status <= 599:
            status = 502
        return HTTPException(status, "Model call failed")


hooks = PurposeHooks()
