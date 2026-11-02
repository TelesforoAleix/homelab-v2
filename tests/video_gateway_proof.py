"""Explicit paid acceptance: at most two videos and one image upscale, metadata only.

Run on the node with the pinned gateway image's Python, after deployment. The default
video model has no end-frame capability, so that conditional call is skipped. Inputs,
client key and the streamed source video stay only in this process's memory. No provider
key is read. This script never retries a paid POST or prints provider bodies or URLs.
"""

from __future__ import annotations

import argparse
import base64
import hashlib
import json
import random
import secrets
import struct
import time
import zlib

import httpx


def reference_png():
    width, height = 640, 560
    rng = random.Random(41)
    raw = b"".join(b"\0" + rng.randbytes(width * 3) for _ in range(height))

    def chunk(kind, data):
        return (
            struct.pack(">I", len(data)) + kind + data + struct.pack(">I", zlib.crc32(kind + data))
        )

    return (
        b"\x89PNG\r\n\x1a\n"
        + chunk(b"IHDR", struct.pack(">IIBBBBB", width, height, 8, 2, 0, 0, 0))
        + chunk(b"IDAT", zlib.compress(raw))
        + chunk(b"IEND", b"")
    )


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("--base-url", default="http://127.0.0.1:4000")
    args = parser.parse_args()
    with httpx.Client(
        base_url=args.base_url,
        timeout=120,
        trust_env=False,
        headers={"Authorization": "Bearer " + secrets.token_hex(32)},
    ) as client:
        catalogue = client.get("/v1/model/info").json()
        profiles = {row["model_name"]: row["model_info"] for row in catalogue["data"]}
        image = reference_png()
        print(json.dumps({"reference_bytes": len(image)}), flush=True)

        def total():
            result = client.get("/v1/images/cost")
            assert result.status_code == 200
            return result.json()

        def run(body):
            started = time.monotonic()
            response = client.post("/v1/videos", json=body)  # Exactly one attempt.
            print(
                json.dumps({"start_http": response.status_code, "model": body["model"]}), flush=True
            )
            assert response.status_code == 202, "Video start refused; no paid retry"
            job_id = response.json()["id"]
            while time.monotonic() - started < 3600:
                status = client.get("/v1/videos/" + job_id)
                assert status.status_code == 200
                job = status.json()
                if job["status"] in ("completed", "failed"):
                    break
                time.sleep(5)
            else:
                raise RuntimeError("Video acceptance exceeded one hour; do not replay")
            elapsed = time.monotonic() - started
            print(
                json.dumps({"job": job, "seconds": round(elapsed, 3), "month_total": total()}),
                flush=True,
            )
            assert job["cost_usd"] is not None, "Escalate: no billed cost readable"
            assert job["status"] == "completed", "Video job failed; no paid retry"
            before = total()
            for _ in range(3):
                assert client.get("/v1/videos/" + job_id).json()["cost_usd"] == job["cost_usd"]
            chunks = []
            with client.stream("GET", "/v1/videos/" + job_id + "/content") as result:
                assert result.status_code == 200
                assert set(result.headers) <= {
                    "content-type",
                    "content-length",
                    "x-homelab-cost-usd",
                    "date",
                    "server",
                }
                assert result.headers["x-homelab-cost-usd"] == job["cost_usd"]
                for part in result.iter_bytes():
                    chunks.append(part)
            content = b"".join(chunks)
            assert len(content) > 100 and b"ftyp" in content[:64]
            assert total() == before
            print(
                json.dumps(
                    {
                        "job_id": job_id,
                        "streamed_bytes": len(content),
                        "sha256": hashlib.sha256(content).hexdigest(),
                        "cost_once": True,
                    }
                ),
                flush=True,
            )
            return content

        defaults = {
            op: next(name for name in names if op in profiles[name]["default_for"])
            for op, names in catalogue["operations"].items()
            if names
        }
        first_model = defaults["image_to_video"]
        caps = profiles[first_model]["capabilities"]
        first = run(
            {
                "model": first_model,
                "operation": "image_to_video",
                "prompt": "Slow gentle motion through an abstract colorful texture. No text.",
                "image": "data:image/png;base64," + base64.b64encode(image).decode(),
                "duration": min(caps["durations"]),
                "resolution": caps["resolutions"][0],
                "generateAudio": False,
            }
        )
        print(
            json.dumps(
                {
                    "end_frame_job": "skipped",
                    "reason": "cheapest selected model has no end-frame support",
                }
            ),
            flush=True,
        )
        upscale = defaults.get("upscale_video")
        if upscale:
            run(
                {
                    "model": upscale,
                    "operation": "upscale_video",
                    "video": "data:video/mp4;base64," + base64.b64encode(first).decode(),
                    "resolution": profiles[upscale]["capabilities"]["resolutions"][0],
                }
            )
        started = time.monotonic()
        result = client.post(
            "/v1/images/edits",
            data={"model": defaults["upscale_image"], "size": "1280x1120"},
            files={"image": ("synthetic.png", image, "image/png")},
        )
        cost = result.headers.get("x-homelab-cost-usd")
        print(
            json.dumps(
                {
                    "image_upscale_http": result.status_code,
                    "cost_usd": cost,
                    "seconds": round(time.monotonic() - started, 3),
                    "month_total": total(),
                }
            ),
            flush=True,
        )
        assert result.status_code == 200 and cost is not None
        assert result.json()["data"][0]["b64_json"]
        assert total()["charged_calls"] == 3
        print(
            json.dumps(
                {
                    "acceptance": "passed",
                    "video_jobs": 2,
                    "image_upscales": 1,
                    "month_total": total(),
                }
            ),
            flush=True,
        )


if __name__ == "__main__":
    main()
