"""Run through the orchestrator on an idle Mac after med-ask is clear.

Rollback first: uv run python tests/mac_model_memory_acceptance.py restore
The cycle temporarily replaces the login router with the same native router and presets,
with child-only ENOMEM injection. It never changes profiles, model files or GPU settings.
"""

import argparse
import contextlib
import json
import os
import runpy
import shlex
import signal
import subprocess
import time
from pathlib import Path

TOOLS = Path.home() / "Library/Application Support/homelab/mac"
STATE = Path.home() / ".local/state/homelab-model-memory-acceptance"
RECOVERY = STATE / "recovery.json"
EXPECTED = "The Mac could not load this model now (often for lack of memory); try later"
# This is fault injection in a disposable test process, not a production runtime component.
# The native model loader receives a genuine mmap ENOMEM without any memory pressure.
SOURCE = r"""
#include <errno.h>
#include <fcntl.h>
#include <stdlib.h>
#include <sys/mman.h>
#include <unistd.h>

static void * refuse_model_map(void * addr, size_t size, int prot,
                                int flags, int fd, off_t offset) {
    if (getenv("LLAMA_SERVER_ROUTER_PORT") && fd >= 0 && size >= 256UL * 1024 * 1024) {
        const char * marker = getenv("HOMELAB_TEST_ENOMEM_MARKER");
        if (marker) {
            int out = open(marker, O_WRONLY | O_CREAT | O_APPEND, 0600);
            if (out >= 0) { (void)write(out, "ENOMEM\n", 7); close(out); }
        }
        errno = ENOMEM;
        return MAP_FAILED;
    }
    return mmap(addr, size, prot, flags, fd, offset);
}
__attribute__((used, section("__DATA,__interpose")))
static const struct { const void * replacement; const void * original; } bindings[] = {
    { (const void *)refuse_model_map, (const void *)mmap }
};
"""
NODE_PROGRAM = """
import json, urllib.request, urllib.error
request = urllib.request.Request('http://127.0.0.1:4000/v1/embeddings',
    data=json.dumps({'model':'embed-large-bulk','input':'allocation-test'}).encode(),
    headers={'Content-Type':'application/json', 'Authorization':'Bearer unused'})
try:
    with urllib.request.urlopen(request, timeout=90) as response:
        print(json.dumps({'status':response.status, 'message':'unexpected successful load'}))
except urllib.error.HTTPError as error:
    body = json.load(error)
    print(json.dumps({'status':error.code, 'message':body.get('error',{}).get('message')}))
"""


def command(name, *args):
    return runpy.run_path(str(TOOLS / "installed_tools.py"))["command"](name, *args)


def restore():
    if not RECOVERY.exists():
        print("no memory-test recovery pending")
        return
    lib = runpy.run_path(str(TOOLS / "homelab-router"))
    saved = json.loads(RECOVERY.read_text())
    if saved.get("identity") and lib["identity"](saved["pid"]) == saved["identity"]:
        os.killpg(saved["pid"], signal.SIGTERM)
        deadline = time.monotonic() + 15
        while lib["running"](saved) and time.monotonic() < deadline:
            time.sleep(0.2)
        if lib["running"](saved):
            os.killpg(saved["pid"], signal.SIGKILL)
    subprocess.run(command("homelab-router", "start"), check=True)
    RECOVERY.unlink()
    print("normal login router restored; profiles, registry and GPU settings unchanged")


def cycle():
    if RECOVERY.exists():
        raise RuntimeError("run restore before another cycle")
    lib = runpy.run_path(str(TOOLS / "homelab-router"))
    profile = json.loads(subprocess.check_output(command("homelab-profile", "status"), text=True))
    if profile["profile"] != "working" or profile["running_jobs"]:
        raise RuntimeError("requires exact Working with no running jobs; check med-ask first")
    saved = lib["record"]()
    if not lib["healthy"](saved):
        raise RuntimeError("normal router must be healthy")
    rows = lib["api"](saved["address"], "/models")["data"]
    if any(row["status"]["value"] not in {"unloaded", "sleeping"} for row in rows):
        raise RuntimeError("wait for native models to be unloaded/sleeping before testing")
    registry = json.loads((Path.home() / ".config/homelab/models.json").read_text())
    default = profile["services"]["embed-large-bulk"]["default"]
    entry = registry["services"]["embed-large-bulk"][default]
    if Path(entry["model"]).stat().st_size < 256 * 1024**2:
        raise RuntimeError("embedding default is too small for this allocation test")
    STATE.mkdir(parents=True, exist_ok=True, mode=0o700)
    source, library, marker = (STATE / name for name in ("fail-map.c", "fail-map.dylib", "ENOMEM"))
    source.write_text(SOURCE)
    subprocess.run(["/usr/bin/clang", "-dynamiclib", "-o", str(library), str(source)], check=True)
    marker.unlink(missing_ok=True)
    RECOVERY.write_text("{}\n")  # Arm rollback before stopping the real agent.
    process = None
    try:
        subprocess.run(command("homelab-router", "stop"), check=True)
        env = {
            **os.environ,
            "DYLD_INSERT_LIBRARIES": str(library),
            "HOMELAB_TEST_ENOMEM_MARKER": str(marker),
        }
        process = subprocess.Popen(
            command("homelab-router", "serve"),
            env=env,
            stdout=subprocess.DEVNULL,
            stderr=subprocess.DEVNULL,
            start_new_session=True,
        )
        RECOVERY.write_text(
            json.dumps({"pid": process.pid, "identity": lib["identity"](process.pid)})
        )
        deadline = time.monotonic() + 60
        while not lib["healthy"](lib["record"]()):
            if process.poll() is not None or time.monotonic() >= deadline:
                raise RuntimeError("test router startup failed; restoring normal router")
            time.sleep(0.2)
        remote = (
            "sudo docker compose -f /srv/homelab/homelab-v2/compose.yaml exec -T "
            "litellm python -c " + shlex.quote(NODE_PROGRAM)
        )
        result = subprocess.run(
            [
                "/usr/bin/ssh",
                "-o",
                "BatchMode=yes",
                "-o",
                "ConnectTimeout=10",
                "homelab-agent",
                remote,
            ],
            capture_output=True,
            text=True,
            check=True,
            timeout=110,
        )
        answer = json.loads(result.stdout)
        print(json.dumps(answer))
        assert marker.exists(), "native child did not encounter injected mmap ENOMEM"
        assert answer == {"status": 503, "message": EXPECTED}, "unexpected node answer"
        print("verified native child mmap ENOMEM and gateway translation")
    finally:
        if process is not None and process.poll() is None:
            with contextlib.suppress(ProcessLookupError):
                os.killpg(process.pid, signal.SIGTERM)
            try:
                process.wait(timeout=15)
            except subprocess.TimeoutExpired:
                os.killpg(process.pid, signal.SIGKILL)
                process.wait(timeout=10)
        restore()


if __name__ == "__main__":
    os.umask(0o077)
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("action", choices=("cycle", "restore"))
    args = parser.parse_args()
    if args.action == "restore":
        restore()
    else:
        cycle()
