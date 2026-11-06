"""The rule for the task network: a run's token, its purposes, one endpoint, nothing else."""

import hashlib
import json
import logging
from pathlib import Path
from time import time

import pytest
import yaml
from fastapi import FastAPI, Request
from fastapi.responses import JSONResponse, StreamingResponse
from fastapi.testclient import TestClient

from tests.test_image_cost_hook import load_hook

ROOT = Path(__file__).resolve().parents[1]
TASK_PEER = ("10.253.87.9", 50000)
OTHER_PEER = ("172.18.0.5", 50000)
TOKEN = "fixture-task-token-9876"
BEARER = {"authorization": f"Bearer {TOKEN}"}
CHAT = {"model": "task-worker", "messages": [{"role": "user", "content": "hello"}]}


@pytest.fixture
def hook(monkeypatch, tmp_path, tmp_path_factory):
    module = load_hook(monkeypatch)
    monkeypatch.setattr(module, "task_token_dir", tmp_path)
    monkeypatch.setattr(module, "task_cost_dir", tmp_path_factory.mktemp("costs"))
    return module


def grant(directory, token=TOKEN, purposes=("task-worker",), expires_at=None, **fields):
    path = directory / hashlib.sha256(token.encode()).hexdigest()
    path.write_text(
        json.dumps(
            {
                "purposes": list(purposes),
                "expires_at": time() + 60 if expires_at is None else expires_at,
                **fields,
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


def post(http):
    return http.post("/v1/chat/completions", headers=BEARER, json=CHAT)


def test_a_token_gets_its_budget_and_then_429(hook, tmp_path, caplog):
    caplog.set_level(logging.INFO, logger="homelab.models")
    grant(tmp_path, max_requests=2)
    http, reached = client(hook)
    assert [post(http).status_code for _ in range(4)] == [200, 200, 429, 429]
    assert len(reached) == 2
    assert "task_refused reason=budget status=429" in caplog.text


def test_the_refusal_says_the_words_the_runner_looks_for(hook, tmp_path):
    grant(tmp_path, max_requests=1)
    http, _ = client(hook)
    post(http)
    assert "request budget is spent" in post(http).json()["error"]["message"]


def test_the_default_budget_is_a_hundred(hook, tmp_path):
    grant(tmp_path)
    http, reached = client(hook)
    assert {post(http).status_code for _ in range(100)} == {200}
    assert post(http).status_code == 429
    assert len(reached) == 100


def test_each_token_has_its_own_count(hook, tmp_path):
    grant(tmp_path, max_requests=1)
    grant(tmp_path, token="another-token", max_requests=1)
    http, _ = client(hook)
    assert post(http).status_code == 200
    other = {"authorization": "Bearer another-token"}
    assert http.post("/v1/chat/completions", headers=other, json=CHAT).status_code == 200
    assert post(http).status_code == 429


def test_a_refused_request_does_not_spend_the_budget(hook, tmp_path):
    grant(tmp_path, max_requests=1)
    http, _ = client(hook)
    wrong = {**CHAT, "model": "chat"}
    assert http.post("/v1/chat/completions", headers=BEARER, json=wrong).status_code == 403
    assert http.get("/v1/models", headers=BEARER).status_code == 403
    assert post(http).status_code == 200


def test_the_count_keeps_no_token_and_forgets_an_expired_one(hook, tmp_path):
    grant(tmp_path, max_requests=5)
    http, _ = client(hook)
    post(http)
    assert TOKEN not in hook.task_requests
    (digest,) = hook.task_requests
    hook.task_requests[digest] = (1, time() - 1)
    post(http)
    assert hook.task_requests[digest][0] == 1


@pytest.mark.parametrize("value", [0, -1, True, "100", 1.5, None])
def test_a_malformed_budget_is_no_grant(hook, tmp_path, value):
    grant(tmp_path, max_requests=value)
    http, reached = client(hook)
    assert post(http).status_code == 401
    assert reached == []


def test_other_networks_have_no_budget(hook, tmp_path):
    grant(tmp_path, max_requests=1)
    http, _ = client(hook, peer=OTHER_PEER)
    assert hook.task_requests == {}
    http.get("/health/liveliness")
    assert hook.task_requests == {}


# The cost of each request, as the proxy reads it from the reply.


def costs_of(hook, token=TOKEN):
    path = hook.task_cost_dir / hashlib.sha256(token.encode()).hexdigest()
    return [json.loads(line) for line in path.read_text().splitlines()] if path.exists() else None


def replying(hook, make, peer=TASK_PEER):
    app = FastAPI()
    app.add_middleware(hook.ModelEndpointMiddleware)

    @app.api_route("/{path:path}", methods=["GET", "POST"])
    async def downstream(request: Request, path: str):
        return make()

    return TestClient(app, client=peer)


def sse(*events):
    return [f"data: {json.dumps(event)}\n\n".encode() for event in events] + [b"data: [DONE]\n\n"]


def streamed(chunks, status=200):
    return lambda: StreamingResponse(
        iter(chunks), status_code=status, media_type="text/event-stream"
    )


USAGE = {"choices": [], "usage": {"prompt_tokens": 3, "cost": 2.07e-05}}
DELTA = {"choices": [{"delta": {"content": "hi"}}], "usage": None}


def test_a_stream_is_billed_by_its_last_usage_and_arrives_unchanged(hook, tmp_path):
    grant(tmp_path)
    chunks = sse(DELTA, DELTA, USAGE)
    # The usage chunk is split across transport chunks, as a network may split it.
    whole = b"".join(chunks)
    cut = whole.index(b"cost") + 2
    http = replying(hook, streamed([whole[:cut], whole[cut:]]))
    response = http.post("/v1/chat/completions", headers=BEARER, json=CHAT)
    assert response.content == whole
    assert costs_of(hook) == [{"cost_usd": 2.07e-05}]


def test_each_request_adds_a_line(hook, tmp_path):
    grant(tmp_path)
    http = replying(hook, streamed(sse(USAGE)))
    post(http)
    post(http)
    assert costs_of(hook) == [{"cost_usd": 2.07e-05}] * 2


def test_a_reply_with_no_cost_is_null(hook, tmp_path):
    grant(tmp_path)
    http = replying(hook, streamed(sse(DELTA, {"usage": {"prompt_tokens": 3}})))
    post(http)
    http = replying(hook, streamed(sse(DELTA)))
    post(http)
    assert costs_of(hook) == [{"cost_usd": None}, {"cost_usd": None}]


@pytest.mark.parametrize("cost", [-1, "0.1", True, None, [1]])
def test_a_cost_that_is_not_a_non_negative_number_is_null(hook, tmp_path, cost):
    grant(tmp_path)
    post(replying(hook, streamed(sse({"usage": {"cost": cost}}))))
    assert costs_of(hook) == [{"cost_usd": None}]


def test_an_error_status_is_null_even_with_a_cost(hook, tmp_path):
    grant(tmp_path)
    http = replying(hook, streamed(sse(USAGE), status=500))
    assert post(http).status_code == 500
    assert costs_of(hook) == [{"cost_usd": None}]


def test_a_plain_json_reply_is_read_the_same_way(hook, tmp_path):
    grant(tmp_path)
    http = replying(hook, lambda: JSONResponse({"choices": [], "usage": {"cost": 0.5}}))
    assert post(http).json()["usage"]["cost"] == 0.5
    assert costs_of(hook) == [{"cost_usd": 0.5}]


def test_a_plain_reply_longer_than_the_tail_is_still_read(hook, tmp_path):
    grant(tmp_path)
    body = {"choices": [{"text": 'x"usage" ' * 5000}], "usage": {"cost": 0.25}}
    assert len(json.dumps(body)) > hook.TASK_COST_TAIL
    post(replying(hook, lambda: JSONResponse(body)))
    assert costs_of(hook) == [{"cost_usd": 0.25}]


def test_a_long_stream_keeps_only_its_tail_and_is_still_unchanged(hook, tmp_path):
    grant(tmp_path)
    filler = [sse({"choices": [{"delta": {"content": "y" * 1000}}]})[0]] * 100
    chunks = filler + sse(USAGE)
    response = post(replying(hook, streamed(chunks)))
    assert response.content == b"".join(chunks)
    assert costs_of(hook) == [{"cost_usd": 2.07e-05}]


def test_a_write_failure_leaves_the_reply_alone(hook, tmp_path, monkeypatch, caplog):
    grant(tmp_path)
    monkeypatch.setattr(hook, "task_cost_dir", tmp_path / "missing")
    chunks = sse(USAGE)
    with caplog.at_level(logging.INFO, logger="homelab.models"):
        response = post(replying(hook, streamed(chunks)))
    assert response.status_code == 200 and response.content == b"".join(chunks)
    assert caplog.text.count("task_cost_unrecorded") == 1
    assert "0.0000207" not in caplog.text and "2.07e-05" not in caplog.text


def test_the_cost_file_holds_nothing_else(hook, tmp_path):
    grant(tmp_path)
    post(replying(hook, streamed(sse(DELTA, USAGE))))
    (path,) = hook.task_cost_dir.iterdir()
    assert path.name == hashlib.sha256(TOKEN.encode()).hexdigest()
    assert path.read_text() == '{"cost_usd": 2.07e-05}\n'


def test_a_refused_request_writes_nothing(hook, tmp_path):
    grant(tmp_path)
    http = replying(hook, streamed(sse(USAGE)))
    http.post("/v1/chat/completions", headers=BEARER, json={**CHAT, "model": "chat"})
    http.post("/v1/chat/completions", headers={"authorization": "Bearer nope"}, json=CHAT)
    http.get("/v1/models", headers=BEARER)
    assert list(hook.task_cost_dir.iterdir()) == []


def test_another_network_writes_nothing(hook, tmp_path):
    grant(tmp_path)
    http = replying(hook, streamed(sse(USAGE)), peer=OTHER_PEER)
    assert post(http).status_code == 200
    assert list(hook.task_cost_dir.iterdir()) == []


def test_the_cost_folder_is_one_compose_mounts_writable():
    config = yaml.safe_load((ROOT / "config/litellm.yaml").read_text())
    compose = yaml.safe_load((ROOT / "compose.yaml").read_text())
    volumes = compose["services"]["litellm"]["volumes"]
    assert any(volume.endswith(":" + config["task_cost_dir"]) for volume in volumes)
    assert not any(
        config["task_cost_dir"] in volume
        for name, service in compose["services"].items()
        if name != "litellm"
        for volume in service.get("volumes", [])
    )
