"""Run inside the pinned image with --network none; no real key, corpus or model calls."""

from __future__ import annotations

import contextlib
import io
import json
import logging
import os
import subprocess
import sys
import tempfile
import threading
import time
from email import policy
from email.parser import BytesParser
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
IMAGE_KEY = token_hex(24)
IMAGE = (
    "data:image/png;base64,iVBORw0KGgoAAAANSUhEUgAAAAEAAAABCAQAAAC1HAwC"
    "AAAAC0lEQVR42mP8/x8AAwMCAO+aX1sAAAAASUVORK5CYII="
)
REAL_CHAT = "deepseek/deepseek-v4.1-flash"
REAL_EMBED = "llama-nemotron-embed-1b-v2"
REAL_LARGE = "Qwen3-Embedding-4B"


class Upstream(BaseHTTPRequestHandler):
    def log_message(self, *args):
        pass

    def do_GET(self):
        self.send_response(200)
        self.send_header("Content-Type", "application/json")
        self.end_headers()
        self.wfile.write(
            json.dumps(
                {
                    "data": [
                        {"id": model}
                        for model in getattr(self.server, "models", ["vision", REAL_LARGE])
                    ]
                }
            ).encode()
        )

    def do_POST(self):
        raw = self.rfile.read(int(self.headers["Content-Length"]))
        if self.headers["Content-Type"].startswith("multipart/form-data"):
            parts = BytesParser(policy=policy.default).parsebytes(
                ("Content-Type: " + self.headers["Content-Type"] + "\r\n\r\n").encode() + raw
            )
            body = {}
            for part in parts.iter_parts():
                name = part.get_param("name", header="content-disposition")
                value = part.get_payload(decode=True)
                if part.get_filename():
                    body.setdefault(name, []).append(len(value))
                else:
                    body[name] = value.decode()
        else:
            body = json.loads(raw)
        requests.append((self.path, body, self.headers.get("Authorization")))
        if body.get("prompt") == CONTENT + ":quota":
            self.send_response(402)
            self.send_header("Content-Type", "application/json")
            self.end_headers()
            self.wfile.write(
                json.dumps(
                    {
                        "error": {
                            "type": "quota_for_entity_exceeded",
                            "message": CONTENT + IMAGE_KEY + REAL_CHAT,
                        }
                    }
                ).encode()
            )
            return
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
        elif self.path.startswith("/v1/images/"):
            self.wfile.write(
                json.dumps({"created": 1, "data": [{"b64_json": IMAGE.split(",", 1)[1]}]}).encode()
            )
        elif self.path == "/v1/embeddings":
            self.wfile.write(
                json.dumps(
                    {
                        "object": "list",
                        "model": body["model"],
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
            if body.get("modalities") == ["text", "image"]:
                response["choices"][0]["message"]["images"] = [
                    {"type": "image_url", "image_url": {"url": IMAGE}}
                ]
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


def run(unset=False):
    # Capture vendor output too: a content-safe audit logger alone is not enough.
    output = io.StringIO()
    with contextlib.redirect_stdout(output), contextlib.redirect_stderr(output):
        import httpx
        import openai
        from fastapi.testclient import TestClient
        from litellm.utils import ImageResponse
        from litellm_entrypoint import create_app
        from pydantic import ValidationError

        # Both real FLUX responses contained an image but failed this pinned schema.
        # Keep the diagnostic reproducible without paid calls or response rewriting.
        for details in ({}, {"input_tokens_details": None}):
            usage = {"input_tokens": 0, "output_tokens": 0, "total_tokens": 0, **details}
            try:
                ImageResponse(created=1, data=[{"b64_json": IMAGE.split(",", 1)[1]}], usage=usage)
            except ValidationError as exc:
                assert any(
                    error["loc"] == ("usage", "input_tokens_details") for error in exc.errors()
                )
            else:
                raise AssertionError(
                    "Recheck excluded FLUX models: pinned parser now accepts usage"
                )
        ImageResponse(
            created=1,
            data=[{"b64_json": IMAGE.split(",", 1)[1]}],
            usage={
                "input_tokens": 0,
                "output_tokens": 0,
                "total_tokens": 0,
                "input_tokens_details": {"text_tokens": 0, "image_tokens": 0},
            },
        )

        sdk_timeouts = []
        original_init = openai.AsyncOpenAI.__init__

        def observed_init(self, *args, **kwargs):
            sdk_timeouts.append(kwargs.get("timeout"))
            original_init(self, *args, **kwargs)

        openai.AsyncOpenAI.__init__ = observed_init
        vision_upstream = ThreadingHTTPServer(("127.0.0.1", 0), Upstream)
        threading.Thread(target=vision_upstream.serve_forever, daemon=True).start()
        upstream = ThreadingHTTPServer(("127.0.0.1", 0), Upstream)
        threading.Thread(target=upstream.serve_forever, daemon=True).start()
        try:
            with tempfile.TemporaryDirectory() as directory:
                config = yaml.safe_load((ROOT / "config/litellm.yaml").read_text())
                stub_url = f"http://127.0.0.1:{upstream.server_port}/v1"
                os.environ["HOMELAB_MAC_VISION_URL"] = (
                    f"http://127.0.0.1:{vision_upstream.server_port}/v1"
                )
                os.environ["HOMELAB_MAC_EMBED_URL"] = os.environ["HOMELAB_MAC_VISION_URL"]
                if unset:
                    os.environ.pop("HOMELAB_MAC_VISION_URL")
                    os.environ.pop("HOMELAB_MAC_EMBED_URL")
                for row in config["model_list"]:
                    if row["model_name"] not in {"vision", "embed-large-bulk"}:
                        row["litellm_params"]["api_base"] = stub_url
                path = Path(directory) / "litellm.yaml"
                path.write_text(yaml.safe_dump(config))
                secret = Path(directory) / "gateway_api_key"
                secret.write_text(KEY)
                secret.chmod(0o440)
                image_secret = Path(directory) / "image_gateway_api_key"
                image_secret.write_text(IMAGE_KEY)
                image_secret.chmod(0o440)
                app = create_app(str(path), str(secret), str(image_secret))
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
                        "embed-large",
                        "embed-large-bulk",
                        "grade",
                        "image-gpt-2.5-flare",
                        "image-gpt-2.5-sunburst",
                        "image-nano-banana-2.1",
                        "translate",
                        "vision",
                        "vision:xhigh",
                    ]
                    assert REAL_CHAT not in response.text and REAL_EMBED not in response.text
                    calls = 1
                    response = client.get("/v1/model/info", headers=headers)
                    calls += 1
                    assert response.status_code == 200
                    assert response.json() == {
                        "data": [
                            {
                                "model_name": row["model_name"],
                                "model_info": row.get("model_info", {}),
                            }
                            for row in config["model_list"]
                        ]
                    }
                    assert "litellm_params" not in response.text and "api_base" not in response.text
                    assert "127.0.0.1" not in response.text
                    assert KEY not in response.text and IMAGE_KEY not in response.text
                    for row in config["model_list"]:
                        info = row.get("model_info", {})
                        if info.get("mode") != "image_generation":
                            continue
                        purpose = row["model_name"]
                        if info["endpoint"] == "chat/completions":
                            response = client.post(
                                "/v1/chat/completions",
                                headers=headers,
                                json={
                                    "model": purpose,
                                    "messages": [
                                        {
                                            "role": "user",
                                            "content": [
                                                {"type": "text", "text": CONTENT},
                                                {"type": "image_url", "image_url": {"url": IMAGE}},
                                                {"type": "image_url", "image_url": {"url": IMAGE}},
                                            ],
                                        }
                                    ],
                                    "modalities": ["text", "image"],
                                    "reasoning_effort": "minimal",
                                    "providerOptions": {
                                        "google": {
                                            "imageConfig": {"aspectRatio": "1:1", "imageSize": "1K"}
                                        }
                                    },
                                },
                            )
                            calls += 1
                            assert response.status_code == 200, response.text
                            assert (
                                response.json()["choices"][0]["message"]["images"][0]["image_url"][
                                    "url"
                                ]
                                == IMAGE
                            )
                            assert (
                                requests[-1][1]["providerOptions"]["google"]["imageConfig"][
                                    "imageSize"
                                ]
                                == "1K"
                            )
                        else:
                            response = client.post(
                                "/v1/images/edits",
                                headers=headers,
                                data={
                                    "model": purpose,
                                    "prompt": CONTENT,
                                    "response_format": "b64_json",
                                    **(
                                        {
                                            "quality": "low",
                                            "output_format": "png",
                                            "output_compression": "100",
                                            "background": "opaque",
                                        }
                                        if info["prompt_family"] == "openai"
                                        else {}
                                    ),
                                },
                                files=[
                                    ("image[]", ("a.png", b"synthetic-image-a", "image/png")),
                                    ("image[]", ("b.png", b"synthetic-image-b", "image/png")),
                                ],
                            )
                            calls += 1
                            assert response.status_code == 200, response.text
                            assert response.json()["data"][0]["b64_json"] == IMAGE.split(",", 1)[1]
                            assert requests[-1][1]["image[]"] == [17, 17]
                            if info["prompt_family"] == "openai":
                                assert requests[-1][1]["output_format"] == "png"
                                assert requests[-1][1]["output_compression"] == "100"
                                assert "response_format" not in requests[-1][1]
                        assert requests[-1][1]["model"] == row["litellm_params"]["model"][7:]
                        assert requests[-1][2] == "Bearer " + IMAGE_KEY
                    response = client.post(
                        "/v1/images/generations",
                        headers=headers,
                        json={
                            "model": "image-gpt-2.5-flare",
                            "prompt": CONTENT,
                            "response_format": "b64_json",
                            "quality": "low",
                            "output_format": "png",
                        },
                    )
                    calls += 1
                    assert response.status_code == 200, response.text
                    assert requests[-1][2] == "Bearer " + IMAGE_KEY
                    assert requests[-1][1]["output_format"] == "png"
                    response = client.post(
                        "/v1/images/generations",
                        headers=headers,
                        json={"model": "image-gpt-2.5-flare", "prompt": CONTENT + ":quota"},
                    )
                    calls += 1
                    assert response.status_code == 402
                    assert response.json()["error"]["type"] == "quota_for_entity_exceeded"
                    assert "Monthly image budget exhausted" in response.text
                    assert CONTENT not in response.text and IMAGE_KEY not in response.text
                    response = client.post(
                        "/v1/images/edits",
                        headers=headers,
                        data={"model": "image-gpt-2.5-sunburst", "prompt": CONTENT + ":quota"},
                        files={"image": ("a.png", b"synthetic", "image/png")},
                    )
                    calls += 1
                    assert response.status_code == 402
                    assert response.json()["error"]["type"] == "quota_for_entity_exceeded"
                    assert CONTENT not in response.text and IMAGE_KEY not in response.text
                    before_invalid_image = len(requests)
                    for image_path in ["/v1/images/edits", "/v1/images/generations"]:
                        for override in [
                            {"api_base": "https://example.com"},
                            {"seed": 1},
                            {"extra_body": {"api_key": KEY}},
                            {"wrong_field": True},
                            {"response_format": "url"},
                        ]:
                            body = {"model": "image-gpt-2.5-flare", "prompt": CONTENT, **override}
                            response = (
                                client.post(
                                    image_path,
                                    headers=headers,
                                    data={
                                        key: json.dumps(value) if isinstance(value, dict) else value
                                        for key, value in body.items()
                                    },
                                    files={"image": ("a.png", b"synthetic", "image/png")},
                                )
                                if image_path.endswith("edits")
                                else client.post(image_path, headers=headers, json=body)
                            )
                            calls += 1
                            assert response.status_code == 400
                    for options in [
                        {"gateway": {"byok": {"apiKey": KEY}}},
                        {"google": {"imageConfig": {"api_base": "https://example.com"}}},
                    ]:
                        response = client.post(
                            "/v1/chat/completions",
                            headers=headers,
                            json={
                                "model": "image-nano-banana-2.1",
                                "messages": [{"role": "user", "content": CONTENT}],
                                "providerOptions": options,
                            },
                        )
                        calls += 1
                        assert response.status_code == 400
                    assert len(requests) == before_invalid_image
                    for endpoint in ["/v1/models", "/v1/model/info"]:
                        assert (
                            client.get(endpoint + "?model=vision", headers=headers).status_code
                            == 400
                        )
                        assert client.post(endpoint, headers=headers).status_code == 404
                        calls += 2
                    for purpose, effort in [
                        ("chat", "low"),
                        ("chat:high", "medium"),
                        ("chat:xhigh", "high"),
                        ("grade", "low"),
                        ("translate", "low"),
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
                        assert response.json()["choices"][0]["message"]["content"] == CONTENT
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

                    for purpose, real, prefixes in [
                        ("embed", REAL_EMBED, {"query": "query: ", "passage": "passage: "}),
                        (
                            "embed-large",
                            REAL_LARGE,
                            {
                                "query": (
                                    "Instruct: Given a question, retrieve passages that answer "
                                    "the question\nQuery:"
                                ),
                                "passage": "",
                            },
                        ),
                        (
                            "embed-large-bulk",
                            REAL_LARGE,
                            {
                                "query": (
                                    "Instruct: Given a question, retrieve passages that answer "
                                    "the question\nQuery:"
                                ),
                                "passage": "",
                            },
                        ),
                    ]:
                        if unset and purpose == "embed-large-bulk":
                            before_bulk = len(requests)
                            started = time.monotonic()
                            response = client.post(
                                "/v1/embeddings",
                                headers=headers,
                                json={"model": purpose, "input": CONTENT},
                            )
                            calls += 1
                            assert 400 <= response.status_code <= 599
                            assert "error" in response.json()
                            assert CONTENT not in response.text and KEY not in response.text
                            assert len(requests) == before_bulk
                            assert time.monotonic() - started < 4
                            continue
                        timeout_count = len(sdk_timeouts)
                        for text in [CONTENT, [CONTENT, CONTENT + " second"]]:
                            for role in [None, "query", "passage"]:
                                body = {"model": purpose, "input": text, "encoding_format": "float"}
                                if role is not None:
                                    body["input_type"] = role
                                response = client.post("/v1/embeddings", headers=headers, json=body)
                                calls += 1
                                assert response.status_code == 200, response.text
                                assert response.json()["model"] == real
                                forwarded = requests[-1][1]
                                prefix = prefixes[role] if role is not None else ""
                                assert forwarded["input"] == (
                                    prefix + text
                                    if isinstance(text, str)
                                    else [prefix + item for item in text]
                                )
                                assert forwarded["model"] == real
                                assert "input_type" not in forwarded
                        for tokens in [[1, 2]]:
                            response = client.post(
                                "/v1/embeddings",
                                headers=headers,
                                json={"model": purpose, "input": tokens},
                            )
                            calls += 1
                            assert response.status_code == 200
                            assert requests[-1][1]["input"] == tokens
                        if purpose == "embed-large-bulk":
                            assert any(
                                isinstance(timeout, httpx.Timeout)
                                and timeout.connect == 3
                                and timeout.read == 300
                                for timeout in sdk_timeouts[timeout_count:]
                            ), "SDK did not receive the bulk connect timeout"
                        before_invalid = len(requests)
                        for role, text in [
                            ("document", CONTENT),
                            (None, CONTENT),
                            ([], CONTENT),
                            ("query", [1, 2]),
                            ("passage", [[1, 2], [3]]),
                            ("query", [CONTENT, 1]),
                            ("passage", []),
                        ]:
                            response = client.post(
                                "/v1/embeddings",
                                headers=headers,
                                json={"model": purpose, "input": text, "input_type": role},
                            )
                            calls += 1
                            assert response.status_code == 400
                        assert len(requests) == before_invalid

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

                    image = (
                        "data:image/png;base64,iVBORw0KGgoAAAANSUhEUgAAAAEAAAABCAQAAAC1HAwC"
                        "AAAAC0lEQVR42mP8/x8AAwMCAO+aX1sAAAAASUVORK5CYII="
                    )
                    for purpose in ["vision", "vision:xhigh"]:
                        before_image = len(requests)
                        started = time.monotonic()
                        response = client.post(
                            "/v1/chat/completions",
                            headers=headers,
                            json={
                                "model": purpose,
                                "messages": [
                                    {
                                        "role": "user",
                                        "content": [
                                            {"type": "text", "text": CONTENT},
                                            {"type": "image_url", "image_url": {"url": image}},
                                        ],
                                    }
                                ],
                            },
                        )
                        calls += 1
                        if unset and purpose == "vision":
                            assert 400 <= response.status_code <= 599
                            assert "error" in response.json()
                            assert CONTENT not in response.text and KEY not in response.text
                            assert len(requests) == before_image
                            assert time.monotonic() - started < 4
                        else:
                            assert response.status_code == 200, response.status_code
                            assert response.json()["model"] == purpose
                            assert response.json()["choices"][0]["message"]["content"] == CONTENT
                            assert requests[-1][1]["model"] == (
                                "vision" if purpose == "vision" else REAL_CHAT
                            )
                            assert (
                                requests[-1][1]["messages"][0]["content"][1]["image_url"]["url"]
                                == image
                            )
                            assert "reasoning_effort" not in requests[-1][1]
                            assert requests[-1][2] == "Bearer " + (
                                "none" if purpose == "vision" else KEY
                            )

                    if not unset:
                        response = client.post(
                            "/v1/chat/completions",
                            headers=headers,
                            json={
                                "model": "vision",
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
                        assert events and all(event["model"] == "vision" for event in events)
                        assert any(
                            isinstance(timeout, httpx.Timeout)
                            and timeout.connect == 3
                            and timeout.read == 300
                            for timeout in sdk_timeouts
                        ), "SDK did not receive the separate vision connect timeout"
                        for purpose, endpoint, payload in (
                            ("vision", "/v1/chat/completions", {"messages": []}),
                            ("embed-large-bulk", "/v1/embeddings", {"input": CONTENT}),
                        ):
                            vision_upstream.models = []
                            before_refusal = len(requests)
                            response = client.post(
                                endpoint, headers=headers, json={"model": purpose, **payload}
                            )
                            calls += 1
                            assert response.status_code == 503, response.text
                            assert response.json()["error"]["message"] == (
                                f"{purpose} is not available until Inference"
                            )
                            assert len(requests) == before_refusal
                            assert any(
                                f"purpose={purpose} " in line
                                and "profile_blocked=true" in line
                                and "upstream=false" in line
                                for line in output.getvalue().splitlines()
                            )
                        vision_upstream.models = ["vision", REAL_LARGE]
                        vision_upstream.shutdown()
                        vision_upstream.server_close()
                        before_stopped = len(requests)
                        started = time.monotonic()
                        response = client.post(
                            "/v1/chat/completions",
                            headers=headers,
                            json={
                                "model": "vision",
                                "messages": [{"role": "user", "content": CONTENT}],
                            },
                        )
                        calls += 1
                        stopped_seconds = time.monotonic() - started
                        assert stopped_seconds < 3
                        assert 400 <= response.status_code <= 599
                        assert "error" in response.json()
                        assert CONTENT not in response.text and KEY not in response.text
                        assert REAL_CHAT not in response.text and "127.0.0.1" not in response.text
                        assert len(requests) == before_stopped
                        started = time.monotonic()
                        response = client.post(
                            "/v1/embeddings",
                            headers=headers,
                            json={"model": "embed-large-bulk", "input": CONTENT},
                        )
                        calls += 1
                        assert time.monotonic() - started < 3
                        assert 400 <= response.status_code <= 599
                        assert "error" in response.json()
                        assert CONTENT not in response.text and KEY not in response.text
                        assert "127.0.0.1" not in response.text
                        assert len(requests) == before_stopped

                    before = len(requests)
                    for purpose in [
                        "image-nano-banana-2",
                        "openai/gpt-5.6-luna",
                        "embed:high",
                        "embed-large:high",
                        "embed-large-bulk:high",
                        "vision:high",
                        "openai/" + REAL_CHAT,
                        REAL_CHAT,
                        REAL_EMBED,
                        REAL_LARGE,
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
                    CONTENT not in logs
                    and KEY not in logs
                    and IMAGE_KEY not in logs
                    and image not in logs
                    and "arbitrary-client-key" not in logs
                )
                lines = [line for line in logs.splitlines() if line.startswith("model_call ")]
                assert len(lines) == calls
                assert all(
                    "elapsed_seconds=" in line and "real_model=" in line and "status=" in line
                    for line in lines
                )
                assert any(
                    f"purpose=embed-large real_model={REAL_LARGE} status=200" in line
                    for line in lines
                )
                for purpose in ["grade", "translate"]:
                    assert (
                        sum(
                            f"purpose={purpose} real_model={REAL_CHAT} status=200" in line
                            for line in lines
                        )
                        == 1
                    )
                vision_lines = [
                    line for line in lines if "purpose=vision " in line and "status=200" in line
                ]
                if not unset:
                    assert vision_lines and all(
                        f"real_model={REAL_CHAT}" in line for line in vision_lines
                    ), vision_lines
                assert all("upstream=false" in line for line in lines if "purpose=unknown" in line)
                assert all(
                    "upstream=true" in line
                    for line in lines
                    if "status=200" in line
                    and "purpose=models" not in line
                    and "purpose=model-info" not in line
                )
                assert not external_attempts
        finally:
            upstream.shutdown()
            upstream.server_close()
            vision_upstream.shutdown()
            vision_upstream.server_close()
            openai.AsyncOpenAI.__init__ = original_init
    print(
        f"litellm_smoke checks=passed http_calls={calls} "
        f"upstream_calls={len(requests)} external_attempts=0 unset={unset} "
        f"stopped_seconds={stopped_seconds:.3f}"
        if not unset
        else f"litellm_smoke checks=passed http_calls={calls} unset=True external_attempts=0"
    )


if __name__ == "__main__":
    run(unset="--unset" in sys.argv)
    if "--unset" not in sys.argv:
        subprocess.run([sys.executable, __file__, "--unset"], check=True)
