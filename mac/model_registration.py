"""Private HF downloads and isolated llama.cpp test loads; no live router policy writes."""

import hashlib
import json
import math
import os
import re
import shutil
import socket
import subprocess
import tempfile
import time
import urllib.error
import urllib.parse
import urllib.request
from pathlib import Path

MIN_FREE_BYTES = 20 * 1024**3


class ModelError(ValueError):
    """Fixed messages safe to show without URLs, paths or native diagnostic output."""


class NoRedirect(urllib.request.HTTPRedirectHandler):
    def redirect_request(self, req, fp, code, msg, headers, newurl):
        return None


class HTTPSRedirect(urllib.request.HTTPRedirectHandler):
    def redirect_request(self, req, fp, code, msg, headers, newurl):
        if urllib.parse.urlsplit(newurl).scheme != "https":
            raise ModelError("model download requires HTTPS")
        return super().redirect_request(req, fp, code, msg, headers, newurl)


def metadata(url):
    parsed = urllib.parse.urlsplit(url)
    if (
        parsed.scheme != "https"
        or parsed.netloc != "huggingface.co"
        or not re.fullmatch(r"/[^/]+/[^/]+/resolve/[^/]+/.+", parsed.path)
        or parsed.query
        or parsed.fragment
    ):
        raise ModelError("use a Hugging Face HTTPS resolve URL without query or fragment")
    opener = urllib.request.build_opener(urllib.request.ProxyHandler({}), NoRedirect())
    try:
        response = opener.open(urllib.request.Request(url, method="HEAD"), timeout=30)
    except urllib.error.HTTPError as exc:
        if exc.code not in (301, 302, 303, 307, 308):
            raise ModelError("Hugging Face metadata request failed") from None
        response = exc
    with response:
        headers = response.headers
        digest = headers.get("X-Linked-ETag", headers.get("ETag", "")).strip('"')
        size = headers.get("X-Linked-Size", headers.get("Content-Length", ""))
    if not re.fullmatch(r"[0-9a-fA-F]{64}", digest):
        raise ModelError("Hugging Face publishes no SHA-256 for this file; registration stopped")
    if not size.isdecimal() or int(size) <= 0:
        raise ModelError("Hugging Face publishes no download size; registration stopped")
    return {"url": url, "sha256": digest.lower(), "size": int(size)}


def download(info, target):
    opener = urllib.request.build_opener(urllib.request.ProxyHandler({}), HTTPSRedirect())
    partial = target.with_suffix(".partial")
    digest = hashlib.sha256()
    received = 0
    try:
        with opener.open(info["url"], timeout=60) as response, partial.open("xb") as output:
            while chunk := response.read(1024 * 1024):
                received += len(chunk)
                if received > info["size"]:
                    raise ModelError("model download exceeded its published size")
                output.write(chunk)
                digest.update(chunk)
        if received != info["size"] or digest.hexdigest() != info["sha256"]:
            raise ModelError("model download SHA-256 or size mismatch; partial file deleted")
        partial.replace(target)
    finally:
        partial.unlink(missing_ok=True)


def prepare(template, name, url, projector_url=None):
    """Own one new directory, so any failed registration can remove only its own files."""
    sources = [("model", metadata(url))]
    if "mmproj" in template["flags"]:
        if projector_url is None:
            raise ModelError("vision registration requires --projector-url")
        sources.append(("mmproj", metadata(projector_url)))
    elif projector_url is not None:
        raise ModelError("projector URL requires a vision template with mmproj")
    parent = Path(template["model"]).parent
    if shutil.disk_usage(parent).free - sum(info["size"] for _, info in sources) < MIN_FREE_BYTES:
        raise ModelError("download refused: less than 20 GiB would remain free")
    folder = parent / name
    folder.mkdir(mode=0o700)  # Never overwrite/reuse an existing download directory.
    entry = {
        "model": str(folder / "model.gguf"),
        "flags": dict(template["flags"]),
        "memory_mb": None,
    }
    try:
        for kind, info in sources:
            target = folder / ("model.gguf" if kind == "model" else "projector.gguf")
            download(info, target)
            if kind == "mmproj":
                entry["flags"]["mmproj"] = str(target)
        return entry, folder
    except BaseException:
        shutil.rmtree(folder)
        raise


def test_load(entry, router):
    """Load once on a temporary loopback port and measure Darwin physical footprint in MiB."""
    short = {"ngl", "c", "np", "b", "ub", "t"}
    args = [router["BINARY"], "--model", entry["model"]]
    for key, value in entry["flags"].items():
        if key == "embeddings":
            if value != "true":
                raise ModelError("test load requires embeddings=true")
            args.append("--embeddings")
        else:
            args.extend([("-" if key in short else "--") + key, value])
    with socket.socket() as probe:
        probe.bind(("127.0.0.1", 0))
        port = probe.getsockname()[1]
    args += ["--host", "127.0.0.1", "--port", str(port), "--log-disable", "--no-mmproj-auto"]
    env = {k: v for k, v in os.environ.items() if not k.startswith("LLAMA_ARG_")}
    opener = urllib.request.build_opener(urllib.request.ProxyHandler({}))
    with tempfile.TemporaryDirectory(prefix="homelab-model-test-") as cache:
        env["LLAMA_CACHE"] = cache
        process = subprocess.Popen(
            args,
            env=env,
            stdin=subprocess.DEVNULL,
            stdout=subprocess.DEVNULL,
            stderr=subprocess.DEVNULL,
        )
        try:
            deadline = time.monotonic() + 300
            while process.poll() is None and time.monotonic() < deadline:
                try:
                    with opener.open(f"http://127.0.0.1:{port}/health", timeout=2) as response:
                        ready = json.load(response).get("status") == "ok"
                    if ready:
                        samples = []
                        for _ in range(3):
                            samples.append(
                                router["memory"](process.pid)["family_physical_footprint_bytes"]
                            )
                            time.sleep(0.2)
                        if process.poll() is not None or max(samples) <= 0:
                            break
                        return math.ceil(max(samples) / 1024**2)
                except (OSError, ValueError, urllib.error.URLError):
                    pass
                time.sleep(0.2)
            raise ModelError("Mac could not test-load this model; it was not registered")
        finally:
            if process.poll() is None:
                process.terminate()
            try:
                process.wait(timeout=10)
            except subprocess.TimeoutExpired:
                process.kill()
                process.wait(timeout=10)
