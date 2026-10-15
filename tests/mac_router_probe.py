"""Direct-node acceptance probe; run inside the node's LiteLLM container via stdin.

Uses the existing private vision URL for both router models before deployment changes .env.
Prints synthetic-test results only; never emits URLs, upstream errors or vector contents.
"""

import base64
import json
import math
import os
import struct
import time
import urllib.request
import zlib

SENTENCES = [
    "The red bicycle stands beside the garden gate.",
    "A small boat crossed the quiet lake.",
    "Fresh bread cooled on the kitchen table.",
    "The library closes early on Sundays.",
    "Rain fell gently throughout the afternoon.",
    "A cat slept beside the warm window.",
    "The train arrived at the station on time.",
    "Children played football in the park.",
    "The mountain trail follows a narrow stream.",
    "She planted tomatoes in a sunny corner.",
    "A bright moon rose above the trees.",
    "The carpenter repaired the wooden chair.",
    "Our meeting begins at nine tomorrow.",
    "The museum displays ancient pottery.",
    "A friendly dog waited outside the shop.",
    "The baker measured flour into a bowl.",
    "Birds gathered around the fountain.",
    "The new bridge connects the two villages.",
    "He folded the clean towels carefully.",
    "The school concert finished before sunset.",
]
OPENER = urllib.request.build_opener(urllib.request.ProxyHandler({}))


def request(base, path, payload=None):
    data = json.dumps(payload).encode() if payload is not None else None
    req = urllib.request.Request(
        base + path, data=data, headers={"Content-Type": "application/json"}
    )
    with OPENER.open(req, timeout=300) as response:
        return json.load(response)


def image():
    def chunk(kind, data):
        return (
            struct.pack("!I", len(data)) + kind + data + struct.pack("!I", zlib.crc32(kind + data))
        )

    return (
        b"\x89PNG\r\n\x1a\n"
        + chunk(b"IHDR", struct.pack("!2I5B", 64, 64, 8, 2, 0, 0, 0))
        + chunk(b"IDAT", zlib.compress((b"\0" + b"\xff\0\0" * 64) * 64))
        + chunk(b"IEND", b"")
    )


def vectors(response):
    assert response["model"] == "Qwen3-Embedding-4B", "real embedding model differs"
    data = sorted(response["data"], key=lambda row: row["index"])
    assert len(data) == len(SENTENCES), "embedding count differs"
    assert [row["index"] for row in data] == list(range(len(SENTENCES))), "indices differ"
    values = [row["embedding"] for row in data]
    assert all(len(value) == 2560 for value in values), "embedding dimensions differ"
    return values


def main():
    base = os.environ["HOMELAB_MAC_VISION_URL"].removesuffix("/v1").rstrip("/")
    states = {
        row["id"]: row["status"]["value"]
        for row in request(base, "/models")["data"]
        if row["id"] in ("vision", "Qwen3-Embedding-4B")
    }
    assert set(states) == {"vision", "Qwen3-Embedding-4B"}, "router presets missing"
    assert all(state in ("unloaded", "sleeping") for state in states.values()), "models warm"
    print(json.dumps({"initial_model_states": states}), flush=True)
    started = time.perf_counter()
    response = request(
        base,
        "/v1/chat/completions",
        {
            "model": "vision",
            "max_tokens": 32,
            "temperature": 0,
            "messages": [
                {
                    "role": "user",
                    "content": [
                        {
                            "type": "text",
                            "text": "Name the solid colour in this image. Answer briefly.",
                        },
                        {
                            "type": "image_url",
                            "image_url": {
                                "url": "data:image/png;base64," + base64.b64encode(image()).decode()
                            },
                        },
                    ],
                }
            ],
        },
    )
    elapsed = time.perf_counter() - started
    answer = response["choices"][0]["message"]["content"]
    assert "red" in answer.lower(), "synthetic vision answer did not identify red"
    print(
        json.dumps({"vision_cold_seconds": round(elapsed, 3), "vision_identified_red": True}),
        flush=True,
    )
    started = time.perf_counter()
    mac = vectors(
        request(
            base,
            "/v1/embeddings",
            {
                "model": "Qwen3-Embedding-4B",
                "input": SENTENCES,
            },
        )
    )
    mac_seconds = time.perf_counter() - started
    node = vectors(
        request(
            "http://127.0.0.1:4000",
            "/v1/embeddings",
            {
                "model": "embed-large",
                "input": SENTENCES,
            },
        )
    )
    cosines = [
        sum(a * b for a, b in zip(left, right, strict=True))
        / math.sqrt(sum(a * a for a in left) * sum(b * b for b in right))
        for left, right in zip(mac, node, strict=True)
    ]
    minimum = min(cosines)
    print(
        json.dumps(
            {
                "embedding_sentences": len(SENTENCES),
                "embedding_dimensions": 2560,
                "mac_embedding_seconds": round(mac_seconds, 3),
                "lowest_cosine": minimum,
            }
        ),
        flush=True,
    )
    assert minimum >= 0.999, "vector-space acceptance threshold failed"


if __name__ == "__main__":
    try:
        main()
    except Exception as exc:
        print(json.dumps({"probe_failed": type(exc).__name__}), flush=True)
        raise SystemExit(1) from None
