"""Video contract with a fake Vercel gateway; no paid requests."""

import asyncio
import base64
import json
import logging
from datetime import UTC, datetime
from decimal import Decimal

import httpx
import pytest
import yaml
from fastapi import FastAPI
from fastapi.testclient import TestClient
from starlette.requests import Request

from homelab.api import video_jobs as video
from tests.test_image_cost_hook import ROOT, load_hook

MEDIA = "data:image/png;base64," + base64.b64encode(b"private-media").decode()


class MemoryStore:
    def __init__(self):
        self.rows = {}
        self.total = Decimal("0")
        self.calls = 0

    async def recover(self):
        return []

    async def create(self, job_id, key_hash, operation, model):
        if job_id in self.rows:
            return {**self.rows[job_id], "_new": False}
        self.rows[job_id] = dict(
            id=job_id,
            client_key_hash=key_hash,
            operation=operation,
            model=model,
            reference=None,
            status="queued",
            cost_usd=None,
            error=None,
            created_at=datetime.now(UTC),
            updated_at=datetime.now(UTC),
        )
        return self.rows[job_id].copy()

    async def get(self, job_id, key_hash):
        row = self.rows.get(job_id)
        return row.copy() if row and row["client_key_hash"] == key_hash else None

    async def update(self, job_id, *, status=None, reference=None, cost=None, failure=None):
        row = self.rows[job_id]
        if failure is not None:
            row["error"] = failure
        if status:
            row["status"] = status
        if reference is not None:
            row["reference"] = reference
        if cost is not None and row["cost_usd"] is None:
            row["cost_usd"] = cost
            self.total += cost
            self.calls += 1
        return row.copy()


@pytest.fixture
def gateway(monkeypatch):
    config = yaml.safe_load((ROOT / "config/litellm.yaml").read_text())
    store = MemoryStore()
    seen = []
    state = {
        "status": "completed",
        "providerMetadata": {"gateway": {"cost": "0.05"}},
        "videos": [
            {"type": "url", "url": "https://output.example/video.mp4", "mediaType": "video/mp4"}
        ],
    }

    async def upstream(request):
        seen.append(request)
        if request.url.host == "output.example":
            assert "authorization" not in request.headers
            return httpx.Response(
                200,
                content=b"finished-video",
                headers={
                    "content-type": "video/mp4",
                    "x-provider-secret": "private",
                    "cache-control": "private",
                },
            )
        assert request.headers["authorization"] == "Bearer fake-provider-key"
        assert "x-provider-url" not in request.headers
        if request.url.path.endswith("/start"):
            assert request.headers["idempotency-key"]
            return httpx.Response(
                200,
                json={
                    "operation": {"jobId": "vercel-job"},
                    "providerMetadata": {"gateway": {"asyncJob": {"jobId": "vercel-job"}}},
                },
            )
        if request.url.path.endswith("/status"):
            return httpx.Response(200, json=state)
        return httpx.Response(
            200,
            text="data: " + json.dumps({"type": "result", **state}) + "\n\n",
            headers={"content-type": "text/event-stream"},
        )

    instance = video.VideoGateway(
        config,
        store,
        "fake-provider-key",
        transport=httpx.MockTransport(upstream),
        poll_seconds=0.001,
    )
    hook = load_hook(monkeypatch)
    monkeypatch.setattr(hook, "video_gateway", instance)
    app = FastAPI()
    app.add_middleware(hook.ModelEndpointMiddleware)
    return instance, store, seen, state, TestClient(app)


def payload(**updates):
    return {
        "model": "video-grok-lite",
        "operation": "image_to_video",
        "prompt": "private-prompt",
        "image": MEDIA,
        "duration": 5,
        "resolution": "854x480",
        "generateAudio": False,
        **updates,
    }


async def settle(instance):
    await asyncio.gather(*list(instance.tasks.values()))


async def test_start_status_content_key_isolation_and_cost_once(gateway, caplog):
    instance, store, seen, _, _ = gateway
    app = FastAPI()

    @app.api_route("/{path:path}", methods=["GET", "POST"])
    async def dispatch(request: __import__("starlette.requests", fromlist=["Request"]).Request):
        return await instance.handle(request)

    async with httpx.AsyncClient(
        transport=httpx.ASGITransport(app),
        base_url="http://test",
        headers={"Authorization": "Bearer key-a"},
    ) as client:
        with caplog.at_level(logging.INFO):
            started = await client.post("/v1/videos", json=payload())
            assert started.status_code == 202
            job = started.json()
            assert job["status"] == "queued" and job["cost_usd"] is None
            await settle(instance)
            for _ in range(3):
                result = await client.get("/v1/videos/" + job["id"])
                assert result.json()["status"] == "completed"
                assert result.json()["cost_usd"] == "0.05"
            other = await client.get(
                "/v1/videos/" + job["id"], headers={"Authorization": "Bearer key-b"}
            )
            assert other.status_code == 404
            other_content = await client.get(
                "/v1/videos/" + job["id"] + "/content", headers={"Authorization": "Bearer key-b"}
            )
            assert other_content.status_code == 404
            content = await client.get("/v1/videos/" + job["id"] + "/content")
            assert content.content == b"finished-video"
            assert set(content.headers) <= {"content-type", "content-length", "x-homelab-cost-usd"}
            assert content.headers["x-homelab-cost-usd"] == "0.05"
    assert store.total == Decimal("0.05") and store.calls == 1
    assert len([r for r in seen if r.url.path.endswith("/start")]) == 1
    assert "private-prompt" not in caplog.text and "private-media" not in caplog.text
    assert all(
        "private-prompt" not in str(row) and MEDIA not in str(row) for row in store.rows.values()
    )


@pytest.mark.parametrize(
    "update",
    [
        {"image": "https://public.example/image.png"},
        {"image": "data:image/png;base64,bad"},
        {"model": "unknown"},
        {"duration": True},
        {"duration": 999},
        {"resolution": "garbage"},
        {"providerOptions": {"gateway": {"apiKey": "override"}}},
        {"endImage": MEDIA},
        {"operation": "edit_video"},
        {"generateAudio": "false"},
        {"prompt": []},
    ],
)
def test_bad_requests_never_call_vercel(gateway, update):
    _, store, seen, _, client = gateway
    result = client.post(
        "/v1/videos", json=payload(**update), headers={"Authorization": "Bearer a"}
    )
    assert result.status_code == 400
    assert seen == [] and store.rows == {}


def test_oversize_and_auth_before_upstream(gateway, monkeypatch):
    _, store, seen, _, client = gateway
    monkeypatch.setattr(video, "MAX_BODY_BYTES", 100)
    assert (
        client.post(
            "/v1/videos", content=b"x" * 101, headers={"Authorization": "Bearer a"}
        ).status_code
        == 413
    )
    assert client.post("/v1/videos", json=payload()).status_code == 401
    assert (
        client.get("/v1/videos/invalid", headers={"Authorization": "Bearer a"}).status_code == 404
    )
    assert not store.rows and not seen


async def test_large_inline_uses_single_request_and_charged_failure(gateway, monkeypatch):
    instance, store, seen, state, _ = gateway
    monkeypatch.setattr(video, "ASYNC_BODY_BYTES", 100)
    state.update(
        status="error",
        type="error",
        error="private provider error",
        message="private provider error",
        providerMetadata={"gateway": {"cost": "0.02"}},
    )
    app = FastAPI()
    from starlette.requests import Request

    @app.post("/v1/videos")
    async def start(request: Request):
        return await instance.handle(request)

    async with httpx.AsyncClient(
        transport=httpx.ASGITransport(app), base_url="http://test"
    ) as client:
        result = await client.post(
            "/v1/videos", json=payload(), headers={"Authorization": "Bearer a"}
        )
        await settle(instance)
    row = store.rows[result.json()["id"]]
    assert row["status"] == "failed" and row["cost_usd"] == Decimal("0.02")
    assert store.calls == 1
    assert seen[0].url.path == "/v4/ai/video-model"
    assert b"private-media" not in seen[0].content  # base64 on the wire
    assert json.loads(seen[0].content)["image"]["type"] == "file"


def test_operation_catalogue(gateway):
    *_, client = gateway
    response = client.get("/v1/model/info")
    assert response.status_code == 200
    operations = response.json()["operations"]
    assert set(operations) == {
        "generate_image",
        "edit_image",
        "upscale_image",
        "image_to_video",
        "upscale_video",
        "edit_video",
    }
    rows = {r["model_name"]: r["model_info"] for r in response.json()["data"]}
    for operation, names in operations.items():
        assert 0 <= len(names) <= 4
        assert sum(operation in rows[name]["default_for"] for name in names) == bool(names)
        for name in names:
            assert operation in rows[name]["operations"]
            assert "price" in rows[name] and "capabilities" in rows[name]
    assert operations["upscale_image"] == ["image-upscale-wonder"]


async def test_pending_then_completed_and_inline_content(gateway):
    instance, store, seen, state, _ = gateway
    state.update(status="pending")
    model, operation, wire = instance.validate(payload())
    row = await store.create("00000000-0000-0000-0000-000000000001", "a", operation, model)
    task = asyncio.create_task(instance.run(row["id"], model, wire=wire))
    while not any(r.url.path.endswith("/status") for r in seen):
        await asyncio.sleep(0)
    assert store.rows[row["id"]]["status"] == "running"
    state.update(
        status="completed",
        videos=[
            {
                "type": "base64",
                "data": base64.b64encode(b"video-bytes").decode(),
                "mediaType": "video/mp4",
            }
        ],
    )
    await task
    row = store.rows[row["id"]]
    response = await instance.content(row)
    assert b"".join([part async for part in response.body_iterator]) == b"video-bytes"
    assert store.calls == 1


async def test_recovery_resumes_reference_but_never_replays_inline(gateway, monkeypatch):
    instance, store, seen, _, _ = gateway
    first = await store.create(
        "00000000-0000-0000-0000-000000000001", "a", "image_to_video", "video-grok-lite"
    )
    second = await store.create(
        "00000000-0000-0000-0000-000000000002", "a", "image_to_video", "video-grok-lite"
    )
    await store.update(first["id"], reference={"jobId": "saved-reference"})

    async def recovery_rows():
        return list(store.rows.values())

    monkeypatch.setattr(store, "recover", recovery_rows)
    await instance.recover()
    await settle(instance)
    assert store.rows[first["id"]]["status"] == "completed"
    assert store.rows[second["id"]]["status"] == "failed"
    assert all(r.url.path.endswith("/status") for r in seen)
    assert store.calls == 1


async def test_database_failure_refuses_paid_start(gateway, monkeypatch):
    instance, store, seen, _, _ = gateway

    async def unavailable(*args):
        raise OSError("private database diagnostic")

    monkeypatch.setattr(store, "create", unavailable)
    from starlette.requests import Request

    app = FastAPI()

    @app.post("/v1/videos")
    async def start(request: Request):
        return await instance.handle(request)

    async with httpx.AsyncClient(
        transport=httpx.ASGITransport(app), base_url="http://test"
    ) as client:
        result = await client.post(
            "/v1/videos", json=payload(), headers={"Authorization": "Bearer a"}
        )
    assert result.status_code == 503 and not seen
    assert "private" not in result.text


@pytest.mark.parametrize("failed", [False, True])
async def test_postgres_debits_video_once_and_total_route(monkeypatch, failed):
    import os
    from uuid import uuid4

    import psycopg
    from psycopg import sql
    from psycopg.conninfo import make_conninfo

    from homelab.api import image_costs
    from homelab.api.app import app

    database_url = os.environ.get("HOMELAB_TEST_DATABASE_URL")
    if not database_url:
        pytest.skip("Set HOMELAB_TEST_DATABASE_URL to an isolated Postgres database")
    schema = "video_test_" + uuid4().hex
    with psycopg.connect(database_url) as connection:
        connection.execute(sql.SQL("CREATE SCHEMA {}").format(sql.Identifier(schema)))
    url = make_conninfo(database_url, options="-c search_path=" + schema)
    monkeypatch.setenv("HOMELAB_DATABASE_URL", url)
    try:
        image_costs.migrate()
        store = video.PostgresStore()
        job_id = str(uuid4())
        key_hash = __import__("hashlib").sha256(b"key-a").hexdigest()
        await store.create(job_id, key_hash, "image_to_video", "video-grok-lite")
        terminal = "failed" if failed else "completed"
        await asyncio.gather(
            *(store.update(job_id, status=terminal, cost=Decimal("0.02")) for _ in range(12))
        )
        row = await store.get(job_id, key_hash)
        assert row["status"] == terminal and row["cost_usd"] == Decimal("0.02")
        assert await store.get(job_id, "0" * 64) is None
        client = TestClient(app)
        result = client.get("/v1/images/cost", headers={"Authorization": "Bearer key-a"})
        assert result.json()["total_usd"] == "0.02"
        assert result.json()["charged_calls"] == 1
        assert (
            client.get("/v1/images/cost", headers={"Authorization": "Bearer key-b"}).json()[
                "total_usd"
            ]
            == "0"
        )
    finally:
        with psycopg.connect(database_url) as connection:
            connection.execute(sql.SQL("DROP SCHEMA {} CASCADE").format(sql.Identifier(schema)))


def test_image_upscale_native_transport_and_billed_cost(monkeypatch):
    # Multipart parsing itself is covered in the pinned-image smoke suite, whose
    # distribution supplies python-multipart. The application venv does not need it.
    from contextlib import asynccontextmanager
    from io import BytesIO

    from starlette.datastructures import FormData, Headers, UploadFile

    @asynccontextmanager
    async def form(_request):
        yield FormData(
            {
                "model": "image-upscale-wonder",
                "image": UploadFile(
                    BytesIO(b"fixture-image"),
                    filename="test.png",
                    headers=Headers({"content-type": "image/png"}),
                ),
            }
        )

    monkeypatch.setattr(Request, "form", form)
    hook = load_hook(monkeypatch)
    seen = []
    recorded = []

    async def accounting():
        pass

    async def persist(*args):
        recorded.append(args)

    def upstream(request):
        seen.append(request)
        assert request.url.path == "/v4/ai/image-model"
        assert request.headers["ai-model-id"] == "topaz/wonder-3.5"
        body = json.loads(request.content)
        assert body["files"][0]["type"] == "file"
        return httpx.Response(
            200, json={"images": ["aW1hZ2U="], "providerMetadata": {"gateway": {"cost": "0.08"}}}
        )

    config = yaml.safe_load((ROOT / "config/litellm.yaml").read_text())
    instance = video.VideoGateway(
        config, MemoryStore(), "fake-key", transport=httpx.MockTransport(upstream)
    )
    monkeypatch.setattr(hook, "video_gateway", instance)
    monkeypatch.setattr(hook, "ensure_image_accounting", accounting)
    monkeypatch.setattr(hook, "persist_image_cost", persist)
    app = FastAPI()
    app.add_middleware(hook.ModelEndpointMiddleware)
    client = TestClient(app)
    result = client.post(
        "/v1/images/edits",
        headers={"Authorization": "Bearer key-a"},
        data={"model": "image-upscale-wonder"},
        files={"image": ("test.png", b"fixture-image", "image/png")},
    )
    assert result.status_code == 200 and result.json() == {"data": [{"b64_json": "aW1hZ2U="}]}
    assert result.headers["x-homelab-cost-usd"] == "0.08"
    assert len(seen) == len(recorded) == 1
    assert set(result.headers) <= {"content-type", "content-length", "x-homelab-cost-usd"}


async def test_async_charged_failure_and_final_accounting_retry(gateway, monkeypatch):
    instance, store, seen, state, _ = gateway
    state.update(status="error", error="Provider rejected the request.")
    original = store.update
    failures = []

    async def update(job_id, **changes):
        if changes.get("status") == "failed" and not failures:
            failures.append(True)
            raise OSError("private-database-diagnostic")
        return await original(job_id, **changes)

    monkeypatch.setattr(store, "update", update)
    model, operation, wire = instance.validate(payload())
    job_id = "00000000-0000-0000-0000-000000000001"
    import hashlib

    await store.create(job_id, hashlib.sha256(b"key-a").hexdigest(), operation, model)
    await instance.run(job_id, model, wire=wire)
    assert store.calls == 0
    assert instance.pending_charge[job_id][:2] == ("failed", Decimal("0.05"))
    from starlette.requests import Request

    app = FastAPI()

    @app.get("/v1/videos/{job_id}")
    async def status(request: Request):
        return await instance.handle(request)

    async with httpx.AsyncClient(
        transport=httpx.ASGITransport(app), base_url="http://test"
    ) as client:
        for _ in range(3):
            result = await client.get(
                "/v1/videos/" + job_id, headers={"Authorization": "Bearer key-a"}
            )
            assert result.json()["status"] == "failed"
            assert result.json()["cost_usd"] == "0.05"
            assert result.json()["error"]["type"] == "provider"
    assert store.calls == 1 and store.total == Decimal("0.05")
    assert len(seen) == 2  # One start and one status, no paid retry.


def test_kling_rejects_end_frame_and_edit_video_inline_contract(gateway):
    instance, *_ = gateway
    with pytest.raises(ValueError):
        instance.validate(payload(model="video-kling-turbo", endImage=MEDIA, resolution="1280x720"))
    source = "data:video/mp4;base64," + base64.b64encode(b"fixture-video").decode()
    _, _, wire = instance.validate(
        {
            "model": "video-grok",
            "operation": "edit_video",
            "prompt": "synthetic change",
            "video": source,
        }
    )
    assert wire["providerOptions"]["spacexai"]["videoUrl"] == source
    with pytest.raises(ValueError):
        instance.validate(
            {
                "model": "video-grok",
                "operation": "edit_video",
                "prompt": "synthetic change",
                "video": source,
                "duration": 5,
            }
        )


async def test_client_idempotency_reuses_job_without_rebilling(gateway):
    instance, store, seen, _, _ = gateway
    from starlette.requests import Request

    app = FastAPI()

    @app.post("/v1/videos")
    async def start(request: Request):
        return await instance.handle(request)

    headers = {"Authorization": "Bearer key-a", "Idempotency-Key": "stable-client-attempt"}
    async with httpx.AsyncClient(
        transport=httpx.ASGITransport(app), base_url="http://test"
    ) as client:
        first = await client.post("/v1/videos", json=payload(), headers=headers)
        await settle(instance)
        again = await client.post("/v1/videos", json=payload(), headers=headers)
        await settle(instance)
        assert first.json()["id"] == again.json()["id"]
    assert len(store.rows) == 1 and store.calls == 1
    assert len([r for r in seen if r.url.path.endswith("/start")]) == 1


def test_kling_resolution_selects_provider_mode_and_edit_rejects_audio_control(gateway):
    instance, *_ = gateway
    _, _, wire = instance.validate(payload(model="video-kling-turbo", resolution="1920x1080"))
    assert wire["providerOptions"]["klingai"]["mode"] == "pro"
    assert "resolution" not in wire  # The provider ignores the generic field.
    with pytest.raises(ValueError):
        instance.validate(
            payload(model="video-kling-turbo", resolution="1280x720", aspectRatio="16:9")
        )
    with pytest.raises(ValueError):
        instance.validate(
            {
                "model": "video-grok",
                "operation": "edit_video",
                "prompt": "synthetic change",
                "video": "data:video/mp4;base64,dmlkZW8=",
                "generateAudio": False,
            }
        )


@pytest.mark.parametrize("kind", ["provider", "timeout", "gateway", "restart"])
def test_public_failure_reason(kind):
    row = {
        "id": "1",
        "operation": "image_to_video",
        "model": "video",
        "status": "failed",
        "cost_usd": None,
        "error": {"type": kind, "message": "Failed."},
    }
    assert video.public_job(row)["error"] == row["error"]
    row["status"] = "completed"
    assert video.public_job(row)["error"] is None


def test_provider_message_sanitization():
    message = "Rejected https://example.invalid/path " + "A" * 400
    clean = video.safe_message(message, {})
    assert len(clean) <= 300
    assert "https://" not in clean and "A" * 40 not in clean
    assert len(video.safe_message("word " * 100, {})) == 299


async def test_provider_failure_is_stored_without_message_in_log(gateway, caplog):
    instance, store, _, state, _ = gateway
    state.update(status="error", error={"message": "Provider rejected request", "code": "rejected"})
    model, operation, wire = instance.validate(payload())
    row = await store.create("00000000-0000-0000-0000-000000000009", "a", operation, model)
    with caplog.at_level(logging.INFO):
        await instance.run(row["id"], model, wire=wire)
    assert store.rows[row["id"]]["error"] == {
        "type": "provider",
        "message": "Provider rejected request",
    }
    assert "ProviderFailure" in caplog.text and "Provider rejected request" not in caplog.text


@pytest.mark.parametrize(
    "exception,kind", [(TimeoutError(), "timeout"), (RuntimeError(), "gateway")]
)
async def test_exception_failure_is_persisted(gateway, monkeypatch, exception, kind):
    instance, store, _, _, _ = gateway
    model, operation, wire = instance.validate(payload())
    row = await store.create("00000000-0000-0000-0000-000000000008", "a", operation, model)

    async def fail(*args, **kwargs):
        raise exception

    monkeypatch.setattr(instance, "single", fail)
    monkeypatch.setattr(video, "ASYNC_BODY_BYTES", 0)
    await instance.run(row["id"], model, wire=wire)
    assert video.public_job(store.rows[row["id"]])["error"]["type"] == kind


async def test_recovery_records_restart(gateway, monkeypatch):
    instance, store, _, _, _ = gateway
    row = await store.create(
        "00000000-0000-0000-0000-000000000007", "a", "image_to_video", "video-grok-lite"
    )

    async def recover():
        return [row]

    monkeypatch.setattr(store, "recover", recover)
    await instance.recover()
    assert video.public_job(store.rows[row["id"]])["error"]["type"] == "restart"
