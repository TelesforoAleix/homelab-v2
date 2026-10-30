"""Exercise gateway policy without importing LiteLLM's optional proxy distribution."""

import ast
import logging
from datetime import UTC, datetime
from decimal import Decimal
from pathlib import Path
from types import ModuleType

import httpx
import pytest
from fastapi import FastAPI, Request
from fastapi.responses import JSONResponse, StreamingResponse
from fastapi.testclient import TestClient

ROOT = Path(__file__).resolve().parents[1]


@pytest.fixture
def hook(monkeypatch):
    return load_hook(monkeypatch)


def load_hook(monkeypatch):
    monkeypatch.setenv("CONFIG_FILE_PATH", str(ROOT / "config/litellm.yaml"))
    source = ast.parse((ROOT / "config/litellm_hooks.py").read_text())
    source.body = [
        node
        for node in source.body
        if not isinstance(node, ast.ImportFrom) or not node.module.startswith("litellm")
    ]
    module = ModuleType("image_cost_hook_test")
    module.CustomLogger = object
    module.RETURN_RAW_MODEL_NAME_METADATA_KEY = "return_raw_model"
    module.ProxyModelNotFoundError = type("ProxyModelNotFoundError", (Exception,), {})
    exec(compile(source, "config/litellm_hooks.py", "exec"), module.__dict__)
    return module


@pytest.mark.parametrize(
    "purpose,provider_header,metadata,computed,status,expected",
    [
        ("image-gpt-2.5-flare", "0.004", "0.00597", "0.008", 200, "0.004"),
        ("image-gpt-2.5-flare", None, "0.00597", "0.008", 200, "0.00597"),
        ("image-gpt-2.5-flare", None, None, "0.008", 200, "0.008"),
        ("image-gpt-2.5-flare", None, None, "0", 200, None),
        ("image-gpt-2.5-flare", None, None, None, 200, None),
        ("image-gpt-2.5-flare", None, "0.00597", "0", 500, "0.00597"),
        ("image-gpt-2.5-flare", None, None, "0.008", 400, None),
        ("image-gpt-2.5-flare", None, "0", "0.008", 200, "0"),
        ("chat", "0.004", "0.00597", "0.008", 200, None),
        ("embed", "0.004", "0.00597", "0.008", 200, None),
        ("image-nano-banana-2.1", None, "0.00597", "0.008", 200, "0.00597"),
    ],
)
def test_image_cost_policy(
    hook, monkeypatch, purpose, provider_header, metadata, computed, status, expected
):
    recorded = []

    async def persist(key, cost, month):
        recorded.append((key, cost, month))

    monkeypatch.setattr(hook, "persist_image_cost", persist)
    app = FastAPI()
    app.add_middleware(hook.ModelEndpointMiddleware)

    @app.post("/{path:path}")
    async def upstream(request: Request):
        raw_body = {
            "data": [],
            "providerMetadata": {
                "gateway": {"cost": metadata, "routing": {"provider": "private-provider"}}
            },
        }
        raw_headers = {"content-type": "application/json", "x-provider-url": "private"}
        if provider_header is not None:
            raw_headers["x-vercel-ai-gateway-cost"] = provider_header
        raw = httpx.Response(200, headers=raw_headers, json=raw_body)
        await hook.capture_image_response(raw)
        headers = {"x-provider-url": "private", "x-homelab-cost-usd": "999"}
        if computed is not None:
            headers["x-litellm-response-cost"] = computed
        return JSONResponse(raw_body, headers=headers, status_code=status)

    payload = {"model": purpose}
    path = "/v1/images/generations"
    if purpose in {"chat", "image-nano-banana-2.1"}:
        path = "/v1/chat/completions"
        payload["messages"] = [{"role": "user", "content": "synthetic"}]
    elif purpose == "embed":
        path = "/v1/embeddings"
        payload["input"] = "synthetic"
    else:
        payload["prompt"] = "synthetic"
    response = TestClient(app).post(
        path, headers={"Authorization": "Bearer client-a"}, json=payload
    )
    assert response.status_code == status
    assert response.headers.get("x-homelab-cost-usd") == expected
    assert "x-provider-url" not in response.headers
    assert "x-litellm-response-cost" not in response.headers
    if purpose.startswith("image-"):
        assert "providerMetadata" not in response.text
        assert "private-provider" not in response.text
    assert len(recorded) == int(expected is not None)
    if recorded:
        assert recorded[0][:2] == ("client-a", Decimal(expected))
        assert recorded[0][2] == datetime.now(UTC).date().replace(day=1)


def test_image_stream_strips_metadata_and_retains_cost(hook, monkeypatch):
    recorded = []

    async def persist(*args):
        recorded.append(args)

    monkeypatch.setattr(hook, "persist_image_cost", persist)
    app = FastAPI()
    app.add_middleware(hook.ModelEndpointMiddleware)

    @app.post("/v1/chat/completions")
    async def upstream():
        async def chunks():
            yield 'data: {"choices":[],"providerMetadata":{"gateway":{"cost":"0.01"}}}\n\n'
            yield "data: [DONE]\n\n"

        return StreamingResponse(chunks(), media_type="text/event-stream")

    response = TestClient(app).post(
        "/v1/chat/completions",
        json={"model": "image-nano-banana-2.1", "messages": [], "stream": True},
        headers={"Authorization": "Bearer client-a"},
    )
    assert response.headers["x-homelab-cost-usd"] == "0.01"
    assert "providerMetadata" not in response.text
    assert "[DONE]" in response.text
    assert len(recorded) == 1


@pytest.mark.parametrize("value", ["NaN", "Infinity", "-1", "garbage", {}, True])
def test_invalid_cost_is_not_money(hook, value):
    assert hook.usd_amount(value) is None


def test_cost_write_failure_keeps_confirmed_charge(hook, monkeypatch, caplog):
    async def fail(*args):
        raise OSError("private database diagnostic")

    monkeypatch.setattr(hook, "persist_image_cost", fail)
    app = FastAPI()
    app.add_middleware(hook.ModelEndpointMiddleware)

    @app.post("/v1/images/generations")
    async def upstream():
        hook.audit.get()["vercel_metadata_cost"] = Decimal("0.01")
        return JSONResponse({"data": []})

    with caplog.at_level(logging.INFO, logger="homelab.models"):
        response = TestClient(app).post(
            "/v1/images/generations",
            json={"model": "image-gpt-2.5-flare", "prompt": "private prompt"},
            headers={"Authorization": "Bearer private-client-key"},
        )
    assert response.status_code == 503
    assert response.headers["x-homelab-cost-usd"] == "0.01"
    assert "private" not in response.text
    assert "private" not in caplog.text


def test_gateway_cost_route_forwards_only_callers_authorization(hook, monkeypatch):
    seen = []

    def api(request):
        seen.append(request.headers["authorization"])
        assert request.url.path == "/v1/images/cost"
        assert "x-client-key" not in request.headers
        key = request.headers["authorization"].removeprefix("Bearer ")
        return httpx.Response(
            200,
            json={"month": "2026-10", "total_usd": "1" if key == "a" else "2", "charged_calls": 1},
        )

    original_client = httpx.AsyncClient
    monkeypatch.setattr(
        hook.httpx,
        "AsyncClient",
        lambda **kw: original_client(transport=httpx.MockTransport(api), **kw),
    )
    monkeypatch.setenv("HOMELAB_IMAGE_COST_API_URL", "http://api:8000/v1/images/cost")
    app = FastAPI()
    app.add_middleware(hook.ModelEndpointMiddleware)
    client = TestClient(app)
    for key, total in [("a", "1"), ("b", "2")]:
        response = client.get(
            "/v1/images/cost",
            headers={"Authorization": "Bearer " + key, "x-client-key": "other-key"},
        )
        assert response.json()["total_usd"] == total
        assert "x-homelab-cost-usd" not in response.headers
    assert seen == ["Bearer a", "Bearer b"]
    assert client.get("/v1/images/cost?key=b").status_code == 400


def test_image_requires_bearer_before_upstream(hook):
    app = FastAPI()
    app.add_middleware(hook.ModelEndpointMiddleware)
    response = TestClient(app).post(
        "/v1/images/generations", json={"model": "image-gpt-2.5-flare", "prompt": "synthetic"}
    )
    assert response.status_code == 401


async def test_accounting_unavailable_refuses_before_provider(hook, monkeypatch):
    async def unavailable():
        raise OSError("private database diagnostic")

    monkeypatch.setattr(hook, "ensure_image_accounting", unavailable)
    event = {"purpose": "image-gpt-2.5-flare", "upstream": False}
    token = hook.audit.set(event)
    try:
        with pytest.raises(hook.HTTPException) as error:
            await hook.hooks.async_pre_call_deployment_hook({}, "aimage_generation")
        assert error.value.status_code == 503
        assert "private" not in error.value.detail
        assert event["upstream"] is False
        assert "image_client" not in event
    finally:
        hook.audit.reset(token)
