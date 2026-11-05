"""The rule for the task network: a run's token, its purposes, one endpoint, nothing else."""

import hashlib
import json
import logging
from pathlib import Path
from time import time

import pytest
import yaml
from fastapi import FastAPI, Request
from fastapi.responses import JSONResponse
from fastapi.testclient import TestClient

from tests.test_image_cost_hook import load_hook

ROOT = Path(__file__).resolve().parents[1]
TASK_PEER = ("10.253.87.9", 50000)
OTHER_PEER = ("172.18.0.5", 50000)
TOKEN = "fixture-task-token-9876"
BEARER = {"authorization": f"Bearer {TOKEN}"}
CHAT = {"model": "task-worker", "messages": [{"role": "user", "content": "hello"}]}


@pytest.fixture
def hook(monkeypatch, tmp_path):
    module = load_hook(monkeypatch)
    monkeypatch.setattr(module, "task_token_dir", tmp_path)
    return module


def grant(directory, token=TOKEN, purposes=("task-worker",), expires_at=None):
    path = directory / hashlib.sha256(token.encode()).hexdigest()
    path.write_text(
        json.dumps(
            {
                "purposes": list(purposes),
                "expires_at": time() + 60 if expires_at is None else expires_at,
            }
        )
    )
    return path


def client(hook, peer=TASK_PEER):
    """Stand in for LiteLLM behind the real middleware; `reached` records what got through."""
    app = FastAPI()
    app.add_middleware(hook.ModelEndpointMiddleware)
    app.state.reached = []

    @app.api_route("/{path:path}", methods=["GET", "POST"])
    async def downstream(request: Request, path: str):
        app.state.reached.append((request.method, "/" + path, await request.body()))
        return JSONResponse({"ok": True})

    return TestClient(app, client=peer), app.state.reached


def test_valid_token_reaches_its_purpose(hook, tmp_path):
    grant(tmp_path)
    http, reached = client(hook)
    response = http.post("/v1/chat/completions", headers=BEARER, json=CHAT)
    assert response.status_code == 200
    assert [(method, path, json.loads(body)) for method, path, body in reached] == [
        ("POST", "/v1/chat/completions", CHAT)
    ]


@pytest.mark.parametrize(
    "purpose", ["chat", "image-nano-banana-2.1", "image-gpt-2.5-flare", "video-grok", None, ["x"]]
)
def test_other_purposes_are_refused(hook, tmp_path, purpose):
    grant(tmp_path)
    http, reached = client(hook)
    response = http.post("/v1/chat/completions", headers=BEARER, json={**CHAT, "model": purpose})
    assert response.status_code == 403
    assert reached == []


@pytest.mark.parametrize(
    "method,path",
    [
        ("GET", "/v1/models"),
        ("GET", "/v1/model/info"),
        ("GET", "/v1/images/cost"),
        ("GET", "/health/liveliness"),
        ("GET", "/v1/chat/completions"),
        ("POST", "/v1/embeddings"),
        ("POST", "/v1/images/generations"),
        ("POST", "/v1/images/edits"),
        ("POST", "/v1/videos"),
        ("GET", "/v1/videos/0d0d5c31-5b1e-4a8c-9d3f-7a3a2f1a9c11"),
        ("POST", "/chat/completions"),
        ("POST", "/v1/chat/completions/"),
    ],
)
def test_other_paths_are_refused(hook, tmp_path, monkeypatch, method, path):
    grant(tmp_path)
    monkeypatch.setattr(hook, "get_video_gateway", lambda: pytest.fail("reached video gateway"))
    http, reached = client(hook)
    response = http.request(
        method,
        path,
        headers=BEARER,
        **({"json": CHAT} if method == "POST" else {}),
        follow_redirects=False,
    )
    assert response.status_code == 403
    assert reached == []


def test_expired_token_is_refused(hook, tmp_path):
    grant(tmp_path, expires_at=time() - 1)
    http, reached = client(hook)
    assert http.post("/v1/chat/completions", headers=BEARER, json=CHAT).status_code == 401
    assert reached == []


def test_revoked_token_is_refused(hook, tmp_path):
    path = grant(tmp_path)
    http, reached = client(hook)
    assert http.post("/v1/chat/completions", headers=BEARER, json=CHAT).status_code == 200
    path.unlink()
    assert http.post("/v1/chat/completions", headers=BEARER, json=CHAT).status_code == 401
    assert len(reached) == 1


@pytest.mark.parametrize(
    "headers",
    [
        {},
        {"authorization": "Bearer "},
        {"authorization": "Basic " + TOKEN},
        {"authorization": "Bearer another-token"},
        {"authorization": "Bearer ../" + TOKEN},
    ],
)
@pytest.mark.parametrize(
    "method,path", [("POST", "/v1/chat/completions"), ("GET", "/health/liveliness")]
)
def test_no_valid_token_from_the_task_subnet_is_refused(hook, tmp_path, headers, method, path):
    grant(tmp_path)
    http, reached = client(hook)
    response = http.request(
        method, path, headers=headers, **({"json": CHAT} if method == "POST" else {})
    )
    assert response.status_code == 401
    assert reached == []


@pytest.mark.parametrize(
    "stored",
    [
        "not json",
        "[]",
        '{"purposes": ["task-worker"]}',
        '{"expires_at": 9999999999}',
        '{"purposes": "task-worker", "expires_at": 9999999999}',
        '{"purposes": ["task-worker"], "expires_at": "9999999999"}',
        '{"purposes": ["task-worker"], "expires_at": true}',
    ],
)
def test_a_malformed_grant_is_refused(hook, tmp_path, stored):
    grant(tmp_path).write_text(stored)
    http, reached = client(hook)
    assert http.post("/v1/chat/completions", headers=BEARER, json=CHAT).status_code == 401
    assert reached == []


@pytest.mark.parametrize(
    "body,content_type,status",
    [
        (b"not json", "application/json", 400),
        (b"[]", "application/json", 400),
        (json.dumps(CHAT).encode(), "application/x-www-form-urlencoded", 415),
        (json.dumps(CHAT).encode(), "multipart/form-data; boundary=x", 415),
    ],
)
def test_only_a_json_object_is_judged(hook, tmp_path, body, content_type, status):
    # LiteLLM parses forms by content type, so a body must mean one thing to both readers.
    grant(tmp_path)
    http, reached = client(hook)
    response = http.post(
        "/v1/chat/completions",
        headers={**BEARER, "content-type": content_type},
        content=body,
    )
    assert response.status_code == status
    assert reached == []


def test_a_forwarded_header_does_not_move_a_task_peer(hook, tmp_path):
    http, reached = client(hook)
    response = http.post(
        "/v1/chat/completions",
        headers={"x-forwarded-for": OTHER_PEER[0], "x-real-ip": OTHER_PEER[0]},
        json=CHAT,
    )
    assert response.status_code == 401
    assert reached == []


@pytest.mark.parametrize("peer", [OTHER_PEER, ("127.0.0.1", 50000), ("10.253.88.9", 50000)])
def test_other_networks_are_unchanged(hook, peer):
    http, reached = client(hook, peer)
    for purpose in ["chat", "task-worker", "translate"]:
        response = http.post("/v1/chat/completions", json={**CHAT, "model": purpose})
        assert response.status_code == 200
    assert http.get("/v1/models").status_code == 200
    assert http.get("/health/liveliness").status_code == 200
    assert len(reached) == 5


def test_an_unparsable_peer_is_not_a_task_peer(hook):
    http, reached = client(hook, ("testclient", 50000))
    assert http.post("/v1/chat/completions", json=CHAT).status_code == 200
    assert len(reached) == 1


def test_the_token_is_never_logged(hook, tmp_path, caplog):
    grant(tmp_path)
    http, _ = client(hook)
    with caplog.at_level(logging.INFO, logger="homelab.models"):
        http.post("/v1/chat/completions", headers=BEARER, json=CHAT)
        http.post("/v1/chat/completions", headers=BEARER, json={**CHAT, "model": "chat"})
        http.get("/v1/models", headers=BEARER)
        http.post(
            "/v1/chat/completions", headers={"authorization": "Bearer wrong-" + TOKEN}, json=CHAT
        )
    assert "task_refused reason=purpose status=403" in caplog.text
    assert "task_refused reason=path status=403" in caplog.text
    assert "task_refused reason=token status=401" in caplog.text
    assert TOKEN not in caplog.text
    assert hashlib.sha256(TOKEN.encode()).hexdigest() not in caplog.text


def test_the_subnet_is_the_one_compose_creates():
    config = yaml.safe_load((ROOT / "config/litellm.yaml").read_text())
    compose = yaml.safe_load((ROOT / "compose.yaml").read_text())
    tasks = compose["networks"]["tasks"]
    assert tasks["name"] == "homelab-tasks"
    assert tasks["internal"] is True
    assert tasks["ipam"]["config"] == [{"subnet": config["task_subnet"]}]
    members = [
        name
        for name, service in compose["services"].items()
        if "tasks" in service.get("networks", [])
    ]
    assert members == ["litellm"]
    assert any(
        volume.endswith(":" + config["task_token_dir"] + ":ro")
        for volume in compose["services"]["litellm"]["volumes"]
    )
