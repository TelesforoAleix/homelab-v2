"""Root node orchestrator: probe the task launcher from containers without Docker access.

Run from the merged checkout, after owner approval and installation:
    sudo python3 tests/launcher_task_probe.py
"""

import grp
import json
import os
import pwd
import re
import shutil
import subprocess
import uuid
from pathlib import Path

WORK_ROOT = Path("/srv/homelab/task-work")
STATE = Path("/run/homelab-launcher")
CLIENT = """import json, os, socket, sys
assert os.getuid() == int(sys.argv[1]) != 0
request = json.loads(sys.argv[2])
with socket.socket(socket.AF_UNIX) as connection:
    connection.settimeout(60)
    connection.connect('/run/homelab-launcher.sock')
    with open('/dev/null', 'wb') as output:
        socket.send_fds(connection, [(json.dumps(request) + '\\n').encode()],
                        [output.fileno(), output.fileno()])
        with connection.makefile('rb') as stream:
            response = json.loads(stream.readline(65537))
print(json.dumps(response))
"""


def docker(*arguments, **kwargs):
    return subprocess.run(
        ["docker", *arguments], capture_output=True, text=True, check=True, timeout=90, **kwargs
    ).stdout.strip()


def client(image, group, uid, request, name):
    return json.loads(
        docker(
            "run",
            "--rm",
            "--name",
            name,
            "--pull",
            "never",
            "--network",
            "none",
            "--user",
            f"{uid}:{uid}",
            "--group-add",
            str(group),
            "--cap-drop",
            "ALL",
            "--security-opt",
            "no-new-privileges",
            "--read-only",
            "--log-driver",
            "none",
            "--memory",
            "256m",
            "--memory-swap",
            "256m",
            "--cpus",
            "1",
            "--pids-limit",
            "64",
            "--mount",
            "type=bind,src=/run/homelab-launcher.sock,dst=/run/homelab-launcher.sock",
            "--entrypoint",
            "/usr/bin/python3",
            image,
            "-I",
            "-c",
            CLIENT,
            str(uid),
            json.dumps(request),
        )
    )


def exists(name):
    return bool(docker("ps", "-aq", "--filter", f"name=^/{re.escape(name)}$"))


def probe():
    if os.geteuid() != 0:
        raise RuntimeError("run the orchestrator as root; clients run as non-root")
    image = docker("image", "inspect", "--format", "{{.Id}}", "homelab-task:2")
    if not re.fullmatch(r"sha256:[0-9a-f]{64}", image):
        raise RuntimeError("invalid task image id")
    if image not in Path("/etc/homelab-launcher/images").read_text().splitlines():
        raise RuntimeError("approve the task image id before probing")
    group = grp.getgrnam("homelab-launch").gr_gid
    owner = pwd.getpwnam("homelab-task")
    if owner.pw_uid == 0:
        raise RuntimeError("task work owner must be non-root")
    token = "launcher-probe-" + uuid.uuid4().hex
    work = WORK_ROOT / token
    # Parents must already exist; never create a tree on a locked volume.
    if not os.path.ismount("/srv/homelab") or not WORK_ROOT.is_dir():
        raise RuntimeError("unlock the task work volume first")
    names, run_ids = [], []
    work.mkdir(mode=0o700)
    try:
        for path in (work, work / ".git", work / ".venv"):
            if path != work:
                path.mkdir(mode=0o700)
            os.chown(path, owner.pw_uid, owner.pw_gid)
        base = {
            "op": "run",
            "profile": "task",
            "work_id": token,
            "shape": "check",
            "env": {"UV_NO_SYNC": "1", "PYTHONPYCACHEPREFIX": "/tmp/pycache"},
            "arguments": [
                "set -eu; test -d .git; test -d .venv; test ! -w .git; test ! -w .venv; "
                'test -w .; test "$(id -u)" -ne 0',
                "check",
            ],
        }
        cases = [
            ("check starts as uid 21020", 21020, {}, {"ok": True, "exit_code": 0}),
            (
                "off-list environment refused",
                21020,
                {"env": {"LD_PRELOAD": "refused"}},
                {"ok": False, "error": "invalid_environment"},
            ),
            (
                "unknown shape refused",
                21020,
                {"shape": "unknown"},
                {"ok": False, "error": "unknown_shape"},
            ),
            (
                "path in identifier refused",
                21020,
                {"work_id": "../refused"},
                {"ok": False, "error": "invalid_identifier"},
            ),
            (
                "unknown profile refused",
                21020,
                {"profile": "unknown"},
                {"ok": False, "error": "unknown_profile"},
            ),
            ("uid without task refused", 21010, {}, {"ok": False, "error": "profile_denied"}),
        ]
        for index, (label, uid, updates, expected) in enumerate(cases):
            run_id = f"{token}-{index}"
            run_ids.append(run_id)
            container = "homelab-launcher-task-" + run_id
            caller = f"{token}-client-{index}"
            names.extend([container, caller])
            response = client(image, group, uid, {**base, "run_id": run_id, **updates}, caller)
            if response != expected:
                raise RuntimeError("unexpected response: " + label)
            if exists(container) or exists(caller):
                raise RuntimeError("container left after: " + label)
            print("PASS: " + label)
        print("PASS: no probe containers remain")
    finally:
        # Only this invocation's randomly named fixtures and records are removed.
        # Always attempt remaining cleanup even if one Docker operation fails.
        cleanup_failed = False
        try:
            for name in names:
                try:
                    if exists(name):
                        docker("rm", "-f", name)
                except (OSError, subprocess.SubprocessError):
                    cleanup_failed = True
                finally:
                    (STATE / (name + ".current")).unlink(missing_ok=True)
        finally:
            for run_id in run_ids:
                (STATE / (run_id + ".json")).unlink(missing_ok=True)
            shutil.rmtree(work)
        if cleanup_failed:
            raise RuntimeError("probe container cleanup failed")


if __name__ == "__main__":
    try:
        probe()
    except (OSError, ValueError, RuntimeError, subprocess.SubprocessError):
        # Never echo container output or exception text from a privileged probe.
        raise SystemExit(
            "FAIL: task launcher probe; inspect installation and Docker state"
        ) from None
