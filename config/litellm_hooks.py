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


def error_response(status: int, message: str):
    return JSONResponse(
        {
            "error": {
                "message": message,
                "type": "invalid_request_error",
                "param": None,
                "code": None,
            }
        },
        status_code=status,
    )


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
                "model_call purpose=%s real_model=%s status=%d elapsed_seconds=%.3f upstream=%s",
                event["purpose"],
                event["real_model"],
                status,
                monotonic() - started,
                str(event["upstream"]).lower(),
            )

        try:
            fields = None
            if path == "/v1/models" and request.method == "GET":
                event["purpose"] = "models"
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
                    # SDK fields define the OpenAI shape. LiteLLM-specific provider/routing
                    # overrides must never reach the library, even before its pre-call hook.
                    if set(body) - set(fields):
                        response = error_response(400, "Only OpenAI request fields are offered")
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
                response = await call_next(request)

            status = response.status_code
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
            # httpx.Timeout for a separate connect limit on this on-demand image route.
            purpose = event["purpose"]
            if not os.environ.get("HOMELAB_MAC_VISION_URL"):
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
