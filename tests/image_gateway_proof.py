"""One paid call per image profile, on the node; print metadata only, never content or keys.

Run with the pinned LiteLLM image's Python (httpx and PyYAML are bundled).
Direct proofs require a read-only image secret mount. Proxy proofs use a client key.
"""

from __future__ import annotations

import argparse
import base64
import json
import struct
import zlib
from pathlib import Path
from time import monotonic

import httpx
import yaml


def reference_png(shape):
    """A 256px synthetic shape; deterministic fixtures, no private inputs."""
    side = 256
    pixels = bytearray()
    for y in range(side):
        pixels.append(0)
        for x in range(side):
            inside = (
                (x - 128) ** 2 + (y - 128) ** 2 < 72**2
                if shape == "circle"
                else 56 <= x < 200 and 56 <= y < 200
            )
            pixels.extend(
                (220, 30, 30)
                if inside and shape == "circle"
                else (30, 70, 220)
                if inside
                else (255, 255, 255)
            )

    def chunk(kind, data):
        return (
            struct.pack(">I", len(data)) + kind + data + struct.pack(">I", zlib.crc32(kind + data))
        )

    return (
        b"\x89PNG\r\n\x1a\n"
        + chunk(b"IHDR", struct.pack(">IIBBBBB", side, side, 8, 2, 0, 0, 0))
        + chunk(b"IDAT", zlib.compress(bytes(pixels)))
        + chunk(b"IEND", b"")
    )


def reported_cost(payload, headers):
    candidates = [headers.get("x-vercel-ai-gateway-cost"), headers.get("x-litellm-response-cost")]
    for section in [payload, payload.get("usage", {}), payload.get("provider_metadata", {})]:
        if isinstance(section, dict):
            candidates.extend(section.get(name) for name in ("cost", "total_cost", "response_cost"))
    for value in candidates:
        try:
            number = float(value)
            if 0 <= number < 100:
                return number
        except (TypeError, ValueError):
            pass
    return None


def prove(client, row, base_url, via_proxy):
    info = row["model_info"]
    model = (
        row["model_name"] if via_proxy else row["litellm_params"]["model"].removeprefix("openai/")
    )
    endpoint = info["endpoint"]
    references = [reference_png("circle"), reference_png("square")]
    prompt = "Combine the red circle and blue square from the two references on a white background."
    started = monotonic()
    result = {"model_name": row["model_name"], "endpoint": endpoint, "references": 2}
    try:
        if endpoint == "chat/completions":
            body = {
                "model": model,
                "modalities": ["text", "image"],
                "max_tokens": 4096,
                "messages": [
                    {
                        "role": "user",
                        "content": [
                            {"type": "text", "text": prompt},
                            *(
                                {
                                    "type": "image_url",
                                    "image_url": {
                                        "url": "data:image/png;base64,"
                                        + base64.b64encode(ref).decode()
                                    },
                                }
                                for ref in references
                            ),
                        ],
                    }
                ],
                "providerOptions": {
                    "google": {"imageConfig": {"aspectRatio": "1:1", "imageSize": "1K"}}
                },
            }
            response = client.post(base_url + "/" + endpoint, json=body)
        else:
            size = info.get("resolutions", {}).get("values", ["1024x1024"])[0]
            fields = {
                "model": model,
                "prompt": prompt,
                "n": "1",
                "size": size,
                "response_format": "b64_json",
            }
            if "qualities" in info:
                fields["quality"] = info["qualities"]["values"][0]
            response = client.post(
                base_url + "/" + endpoint,
                data=fields,
                files=[
                    ("image[]", (f"reference-{i}.png", ref, "image/png"))
                    for i, ref in enumerate(references)
                ],
            )
        result["status"] = response.status_code
        payload = response.json()
        result["cost_usd"] = reported_cost(payload, response.headers)
        if response.is_success:
            if endpoint == "chat/completions":
                images = payload.get("choices", [{}])[0].get("message", {}).get("images", [])
                successful = bool(images) and all(
                    image.get("image_url", {}).get("url", "").startswith("data:image/")
                    for image in images
                )
                form = "message_images" if successful else None
            else:
                images = payload.get("data", [])
                form = (
                    "b64_json"
                    if images and images[0].get("b64_json")
                    else "url"
                    if images and images[0].get("url")
                    else None
                )
                successful = form is not None
            result.update(success=successful, response=form, images=len(images))
        else:
            error_type = payload.get("error", {}).get("type")
            result.update(
                success=False,
                error_type=error_type
                if error_type
                in {
                    "quota_for_entity_exceeded",
                    "invalid_request_error",
                    "authentication_error",
                    "gateway_error",
                    "rate_limit_error",
                }
                else "other",
            )
    except Exception as exc:
        # Exceptions can include bodies or credentials. Only their class is safe metadata.
        result.update(success=False, exception=type(exc).__name__)
    result["seconds"] = round(monotonic() - started, 3)
    return result


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--config", default="/work/config/litellm.yaml")
    parser.add_argument("--secret", default="/run/secrets/image_gateway_api_key")
    parser.add_argument("--base-url", default="https://ai-gateway.vercel.sh/v1")
    parser.add_argument("--via-proxy", action="store_true")
    args = parser.parse_args()
    rows = yaml.safe_load(Path(args.config).read_text())["model_list"]
    key = "homelab" if args.via_proxy else Path(args.secret).read_text().strip()
    if not key:
        raise SystemExit("Image key is empty")
    with httpx.Client(
        headers={"Authorization": "Bearer " + key},
        timeout=300,
        follow_redirects=False,
        trust_env=False,
    ) as client:
        for row in rows:
            if row.get("model_info", {}).get("mode") == "image_generation":
                print(
                    json.dumps(prove(client, row, args.base_url.rstrip("/"), args.via_proxy)),
                    flush=True,
                )


if __name__ == "__main__":
    main()
