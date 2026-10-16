import hashlib
import importlib.machinery
import importlib.util
import io
from pathlib import Path
from types import SimpleNamespace

import pytest


def module(name, script):
    path = Path(__file__).resolve().parents[1] / "mac" / script
    loader = importlib.machinery.SourceFileLoader(name, str(path))
    spec = importlib.util.spec_from_loader(name, loader)
    result = importlib.util.module_from_spec(spec)
    loader.exec_module(result)
    return result


m = module("registration", "model_registration.py")
URL = "https://huggingface.co/example/repo/resolve/main/model.gguf"


@pytest.fixture
def template(tmp_path):
    return {"model": str(tmp_path / "old.gguf"), "flags": {"ngl": "99"}, "memory_mb": None}


def test_disk_refusal_before_downloading(template, monkeypatch):
    monkeypatch.setattr(m, "metadata", lambda url: {"size": 100})
    monkeypatch.setattr(
        m.shutil, "disk_usage", lambda path: SimpleNamespace(free=m.MIN_FREE_BYTES + 99)
    )
    monkeypatch.setattr(m, "download", lambda *args: pytest.fail("download attempted"))
    with pytest.raises(m.ModelError, match="20 GiB"):
        m.prepare(template, "new", URL)


def test_hash_mismatch_removes_partial_and_never_publishes(tmp_path, monkeypatch):
    monkeypatch.setattr(
        m.urllib.request,
        "build_opener",
        lambda *args: SimpleNamespace(open=lambda *args, **kwargs: io.BytesIO(b"wrong")),
    )
    target = tmp_path / "model.gguf"
    with pytest.raises(m.ModelError, match="mismatch"):
        m.download({"url": URL, "size": 5, "sha256": "0" * 64}, target)
    assert list(tmp_path.iterdir()) == []


def test_verified_download_and_projector_cleanup(template, monkeypatch):
    body = b"model"
    info = {"url": URL, "size": len(body), "sha256": hashlib.sha256(body).hexdigest()}
    monkeypatch.setattr(m, "metadata", lambda url: info)
    monkeypatch.setattr(m.shutil, "disk_usage", lambda path: SimpleNamespace(free=100 * 1024**3))
    monkeypatch.setattr(
        m.urllib.request,
        "build_opener",
        lambda *args: SimpleNamespace(open=lambda *args, **kwargs: io.BytesIO(body)),
    )
    entry, folder = m.prepare(template, "new", URL)
    assert (folder / "model.gguf").read_bytes() == body
    assert entry["flags"] == template["flags"]
    template["flags"]["mmproj"] = "/private/example/projector.gguf"
    count = []
    original = m.download

    def download(info, target):
        count.append(target)
        if len(count) == 2:
            raise OSError("network failure")
        original(info, target)

    monkeypatch.setattr(m, "download", download)
    with pytest.raises(OSError):
        m.prepare(template, "vision-new", URL, URL)
    assert not (folder.parent / "vision-new").exists()


@pytest.mark.parametrize(
    "digest,valid", [('"' + "a" * 64 + '"', True), ('W/"abc"', False), ("", False)]
)
def test_hf_published_metadata(monkeypatch, digest, valid):
    class Response(io.BytesIO):
        headers = {"X-Linked-ETag": digest, "X-Linked-Size": "123"}

    monkeypatch.setattr(
        m.urllib.request,
        "build_opener",
        lambda *args: SimpleNamespace(open=lambda *args, **kwargs: Response()),
    )
    if valid:
        assert m.metadata(URL) == {"url": URL, "size": 123, "sha256": "a" * 64}
    else:
        with pytest.raises(m.ModelError, match="no SHA-256"):
            m.metadata(URL)


@pytest.mark.parametrize("projector", [False, True])
def test_isolated_load_accounts_for_mapped_weights_and_stops_child(
    template, monkeypatch, projector
):
    with Path(template["model"]).open("wb") as handle:
        handle.truncate(2 * 1024**2 + 1)
    if projector:
        path = Path(template["model"]).with_name("projector.gguf")
        with path.open("wb") as handle:
            handle.truncate(3 * 1024**2 + 2)
        template["flags"]["mmproj"] = str(path)
    samples = iter([10 * 1024**2 + 1, 12 * 1024**2 + 1, 11 * 1024**2 + 1])
    calls = []

    class Process:
        pid = 123
        stopped = False

        def __init__(self, args, **kwargs):
            calls.append((args, kwargs))

        def poll(self):
            return 0 if self.stopped else None

        def terminate(self):
            self.stopped = True
            calls.append("terminate")

        def wait(self, **kwargs):
            calls.append("wait")

    monkeypatch.setattr(m.subprocess, "Popen", Process)
    monkeypatch.setattr(m.time, "sleep", lambda _: None)
    monkeypatch.setattr(
        m.urllib.request,
        "build_opener",
        lambda *args: SimpleNamespace(open=lambda *args, **kwargs: io.BytesIO(b'{"status":"ok"}')),
    )
    monkeypatch.setenv("LLAMA_ARG_HOST", "0.0.0.0")
    measured = m.test_load(
        template,
        {
            "BINARY": "/example/llama-server",
            "memory": lambda pid: {"family_physical_footprint_bytes": next(samples)},
        },
    )
    assert measured == (18 if projector else 15)  # Round the sum of bytes once, after sampling.
    args, kwargs = calls[0]
    assert args[args.index("--host") + 1] == "127.0.0.1"
    assert "--models-preset" not in args
    assert "--no-mmproj-auto" in args and "--log-disable" in args
    assert "LLAMA_ARG_HOST" not in kwargs["env"]
    assert calls[1:] == ["terminate", "wait"]


def test_memory_acceptance_node_program_compiles():
    acceptance = module("memory_acceptance", "../tests/mac_model_memory_acceptance.py")
    compile(acceptance.NODE_PROGRAM, "node-probe", "exec")


def test_darwin_allocation_fixture_refuses_only_child_model_maps(tmp_path):
    import os
    import shutil
    import subprocess
    import sys

    if sys.platform != "darwin" or not shutil.which("clang"):
        pytest.skip("Darwin dyld and clang required")
    acceptance = module("memory_acceptance_fixture", "../tests/mac_model_memory_acceptance.py")
    source = tmp_path / "fail.c"
    library = tmp_path / "fail.dylib"
    source.write_text(acceptance.SOURCE)
    subprocess.run(["clang", "-dynamiclib", "-o", str(library), str(source)], check=True)
    source.write_text("""
#include <errno.h>
#include <fcntl.h>
#include <sys/mman.h>
#include <unistd.h>
int main(int argc, char **argv) {
    int fd = open(argv[1], O_RDONLY);
    void * data = mmap(0, 257UL * 1024 * 1024, PROT_READ, MAP_PRIVATE, fd, 0);
    if (data == MAP_FAILED) return errno == ENOMEM ? 42 : 1;
    munmap(data, 257UL * 1024 * 1024);
    close(fd);
    return 0;
}
""")
    probe = tmp_path / "probe"
    subprocess.run(["clang", "-o", str(probe), str(source)], check=True)
    data = tmp_path / "sparse-data"
    with data.open("wb") as handle:
        handle.truncate(257 * 1024**2)  # Sparse; never fault in or allocate physical pages.
    marker = tmp_path / "ENOMEM"
    env = {k: v for k, v in os.environ.items() if k != "LLAMA_SERVER_ROUTER_PORT"}
    env.update(DYLD_INSERT_LIBRARIES=str(library), HOMELAB_TEST_ENOMEM_MARKER=str(marker))
    assert subprocess.run([str(probe), str(data)], env=env, timeout=10).returncode == 0
    assert not marker.exists()
    env["LLAMA_SERVER_ROUTER_PORT"] = "8090"
    assert subprocess.run([str(probe), str(data)], env=env, timeout=10).returncode == 42
    assert marker.read_text() == "ENOMEM\n"


def test_weight_charge_deduplicates_hard_linked_model_and_projector(template):
    path = Path(template["model"])
    path.write_bytes(b"weights")
    projector = path.with_name("projector.gguf")
    projector.hardlink_to(path)
    template["flags"]["mmproj"] = str(projector)
    assert m.model_file_bytes(template) == len(b"weights")
