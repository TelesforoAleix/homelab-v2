"""Run inside the pinned image with --network none; no real key, corpus or model calls."""

from __future__ import annotations

import contextlib
import io
import json
import logging
import os
import sys
import tempfile
import threading
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from secrets import token_hex

import yaml

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "config"))
compose = yaml.safe_load((ROOT / "compose.yaml").read_text())
os.environ.update(compose["services"]["litellm"]["environment"])
external_attempts = []


def socket_audit(event, args):
    if event == "socket.getaddrinfo" and args[0] not in {"localhost", "127.0.0.1", "::1"}:
        external_attempts.append(event)
        raise OSError("External DNS forbidden in smoke test")
    if event == "socket.connect" and isinstance(args[1], tuple):
        if args[1][0] not in {"127.0.0.1", "::1"}:
            external_attempts.append(event)
            raise OSError("External connections forbidden in smoke test")


sys.addaudithook(socket_audit)
requests = []
CONTENT = "private-fixture-content-9876"
KEY = token_hex(24)
REAL_CHAT = "deepseek/deepseek-v4.1-flash"
REAL_EMBED = "llama-nemotron-embed-1b-v2"


class Upstream(BaseHTTPRequestHandler):
    def log_message(self, *args):
        pass

    def do_POST(self):
        body = json.loads(self.rfile.read(int(self.headers["Content-Length"])))
        requests.append((self.path, body, self.headers.get("Authorization")))
        self.send_response(
            500 if body.get("messages", [{}])[0].get("content") == CONTENT + ":fail" else 200
        )
        self.send_header(
            "Content-Type", "text/event-stream" if body.get("stream") else "application/json"
        )
        self.send_header("x-provider-model", REAL_CHAT)
        self.end_headers()
        if body.get("messages", [{}])[0].get("content") == CONTENT + ":fail":
            self.wfile.write(json.dumps({"error": {"message": CONTENT + KEY + REAL_CHAT}}).encode())
        elif self.path == "/v1/embeddings":
            self.wfile.write(
                json.dumps(
                    {
                        "object": "list",
                        "model": REAL_EMBED,
                        "data": [
                            {"object": "embedding", "index": 0, "embedding": [0.125, -0.5, 0.25]}
                        ],
                        "usage": {"prompt_tokens": 3, "total_tokens": 3},
                    }
                ).encode()
            )
        else:
            response = {
                "id": "chatcmpl-fixture",
                "object": "chat.completion",
                "created": 1,
                "model": REAL_CHAT,
                "choices": [
                    {
                        "index": 0,
                        "message": {"role": "assistant", "content": CONTENT},
                        "finish_reason": "stop",
                    }
                ],
                "usage": {
                    "prompt_tokens": 5,
                    "completion_tokens": 4,
                    "total_tokens": 9,
                    "completion_tokens_details": {"reasoning_tokens": 2},
                },
            }
            if body.get("stream"):
                response["object"] = "chat.completion.chunk"
                response["choices"] = [
                    {
                        "index": 0,
                        "delta": {"role": "assistant", "content": CONTENT},
                        "finish_reason": "stop",
                    }
                ]
                self.wfile.write(f"data: {json.dumps(response)}\n\ndata: [DONE]\n\n".encode())
            else:
                self.wfile.write(json.dumps(response).encode())


def run():
    # Capture vendor output too: a content-safe audit logger alone is not enough.
    output = io.StringIO()
    with contextlib.redirect_stdout(output), contextlib.redirect_stderr(output):
        from fastapi.testclient import TestClient
        from litellm_entrypoint import create_app

        upstream = ThreadingHTTPServer(("127.0.0.1", 0), Upstream)
        threading.Thread(target=upstream.serve_forever, daemon=True).start()
        try:
            with tempfile.TemporaryDirectory() as directory:
                config = yaml.safe_load((ROOT / "config/litellm.yaml").read_text())
                for row in config["model_list"]:
                    row["litellm_params"]["api_base"] = (
                        f"http://127.0.0.1:{upstream.server_port}/v1"
                    )
                path = Path(directory) / "litellm.yaml"
                path.write_text(yaml.safe_dump(config))
                secret = Path(directory) / "gateway_api_key"
                secret.write_text(KEY)
                secret.chmod(0o440)
                app = create_app(str(path), str(secret))
                logger = logging.getLogger("homelab.models")
                logger.handlers = [logging.StreamHandler(output)]
                with TestClient(app) as client:
                    headers = {"Authorization": "Bearer arbitrary-client-key"}
                    response = client.get("/v1/models", headers=headers)
                    assert response.status_code == 200
                    assert sorted(row["id"] for row in response.json()["data"]) == [
                        "chat",
                        "chat:high",
                        "chat:xhigh",
                        "embed",
                    ]
                    assert REAL_CHAT not in response.text and REAL_EMBED not in response.text
                    calls = 1
                    for purpose, effort in [
                        ("chat", "low"),
                        ("chat:high", "medium"),
                        ("chat:xhigh", "high"),
                    ]:
                        response = client.post(
                            "/v1/chat/completions",
                            headers=headers,
                            json={
                                "model": purpose,
                                "messages": [{"role": "user", "content": CONTENT}],
                                "reasoning_effort": "high",
                            },
                        )
                        calls += 1
                        assert response.status_code == 200
                        assert response.json()["model"] == purpose
                        assert (
                            response.json()["usage"]["completion_tokens_details"][
                                "reasoning_tokens"
                            ]
                            == 2
                        )
                        assert requests[-1][1]["model"] == REAL_CHAT
                        assert requests[-1][1]["reasoning_effort"] == effort
                        assert requests[-1][2] == "Bearer " + KEY
                        assert "arbitrary-client-key" not in json.dumps(requests[-1][1])
                        assert not any(name.startswith("x-litellm") for name in response.headers)
                        assert "x-provider-model" not in response.headers

                    response = client.post(
                        "/v1/embeddings",
                        headers=headers,
                        json={
                            "model": "embed",
                            "input": [CONTENT],
                            "encoding_format": "float",
                        },
                    )
                    calls += 1
                    assert response.status_code == 200
                    assert response.json()["model"] == REAL_EMBED
                    assert response.json()["data"][0]["embedding"] == [0.125, -0.5, 0.25]
                    assert requests[-1][1]["model"] == REAL_EMBED
                    assert requests[-1][1]["input"] == [CONTENT]
                    assert requests[-1][2] == "Bearer none"

                    response = client.post(
                        "/v1/chat/completions",
                        headers=headers,
                        json={
                            "model": "chat",
                            "messages": [{"role": "user", "content": CONTENT}],
                            "stream": True,
                        },
                    )
                    calls += 1
                    assert response.status_code == 200
                    events = [
                        json.loads(line[6:])
                        for line in response.text.splitlines()
                        if line.startswith("data: ") and line != "data: [DONE]"
                    ]
                    assert events and all(event["model"] == "chat" for event in events)

                    before = len(requests)
                    for purpose in [
                        "openai/gpt-5.6-luna",
                        "embed:high",
                        "vision",
                        "openai/" + REAL_CHAT,
                        REAL_CHAT,
                        REAL_EMBED,
                        "chat,chat:high",
                    ]:
                        response = client.post(
                            "/v1/chat/completions",
                            headers=headers,
                            json={
                                "model": purpose,
                                "messages": [{"role": "user", "content": CONTENT}],
                            },
                        )
                        calls += 1
                        assert 400 <= response.status_code < 500
                        assert "error" in response.json()
                    assert len(requests) == before

                    for override in [
                        {"api_base": "https://example.com", "api_key": KEY},
                        {"user_config": {"model_list": []}},
                        {"router_settings": {"fallbacks": [{"chat": [REAL_CHAT]}]}},
                    ]:
                        response = client.post(
                            "/v1/chat/completions",
                            headers=headers,
                            json={
                                "model": "chat",
                                "messages": [{"role": "user", "content": CONTENT}],
                                **override,
                            },
                        )
                        calls += 1
                        assert response.status_code == 400
                    response = client.post(
                        "/v1/chat/completions",
                        headers=headers,
                        json={
                            "model": "chat",
                            "messages": [
                                {
                                    "role": "user",
                                    "content": [
                                        {
                                            "type": "image_url",
                                            "image_url": {"url": "https://example.com/image"},
                                        }
                                    ],
                                }
                            ],
                        },
                    )
                    calls += 1
                    assert response.status_code == 400
                    assert len(requests) == before

                    for path in [
                        "/model/info",
                        "/v2/model/info",
                        "/key/generate",
                        "/ui",
                        "/config/yaml",
                    ]:
                        assert client.get(path, headers=headers).status_code == 404
                        calls += 1
                    assert (
                        client.post(
                            "/v1/chat/completions", content="{", headers=headers
                        ).status_code
                        == 400
                    )
                    calls += 1

                    response = client.post(
                        "/v1/chat/completions",
                        headers=headers,
                        json={
                            "model": "chat",
                            "messages": [{"role": "user", "content": CONTENT + ":fail"}],
                        },
                    )
                    calls += 1
                    assert response.status_code == 500, f"failure_status={response.status_code}"
                    assert (
                        REAL_CHAT not in response.text
                        and KEY not in response.text
                        and CONTENT not in response.text
                    )
                    assert client.get("/health/liveliness").status_code == 200
                logs = output.getvalue()
                assert (
                    CONTENT not in logs and KEY not in logs and "arbitrary-client-key" not in logs
                )
                lines = [line for line in logs.splitlines() if line.startswith("model_call ")]
                assert len(lines) == calls
                assert all(
                    "elapsed_seconds=" in line and "real_model=" in line and "status=" in line
                    for line in lines
                )
                assert all("upstream=false" in line for line in lines if "purpose=unknown" in line)
                assert all(
                    "upstream=true" in line
                    for line in lines
                    if "status=200" in line and "purpose=models" not in line
                )
                assert not external_attempts
        finally:
            upstream.shutdown()
    print(
        f"litellm_smoke checks=passed http_calls={calls} "
        f"upstream_calls={len(requests)} external_attempts=0"
    )


if __name__ == "__main__":
    run()
