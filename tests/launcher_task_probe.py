"""Root node orchestrator: probe the task launcher from containers without Docker access.

Run from the merged checkout, after owner approval and installation:
    sudo python3 tests/launcher_task_probe.py
"""

import argparse
import base64
import grp
import importlib.machinery
import importlib.util
import json
import os
import pwd
import re
import shutil
import subprocess
import time
import urllib.request
import uuid
from pathlib import Path

WORK_ROOT = Path("/srv/homelab/task-work")
STATE = Path("/run/homelab-launcher")
CLIENT = """import json, os, socket, sys
assert os.getuid() == int(sys.argv[1]) != 0
request = json.loads(os.environ.pop('HOMELAB_PROBE_REQUEST') if sys.argv[2] == '-' else sys.argv[2])
with socket.socket(socket.AF_UNIX) as connection:
    connection.settimeout(60)
    connection.connect('/run/homelab-launcher.sock')
    with open('/dev/null', 'wb') as output:
        descriptors = [output.fileno(), output.fileno()] if request['op'] == 'run' else []
        if request.get('stdin'):
            source = os.memfd_create('probe-input', os.MFD_CLOEXEC)
            os.write(source, b'{"goal":"probe","assets":[]}')
            descriptors.append(source)
        socket.send_fds(connection, [(json.dumps(request) + '\\n').encode()], descriptors)
        with connection.makefile('rb') as stream:
            response = json.loads(stream.readline(65537))
print(json.dumps(response))
"""
RUN_CLIENT = """import json, os, socket, sys
assert os.getuid() == int(sys.argv[1]) != 0
request = json.loads(sys.argv[2])
with socket.socket(socket.AF_UNIX) as connection:
    connection.settimeout(60)
    connection.connect('/run/homelab-launcher.sock')
    with open('/dev/null', 'wb') as output:
        descriptors = [output.fileno(), output.fileno()]
        if request.get('stdin'):
            source = os.memfd_create('probe-input', os.MFD_CLOEXEC)
            os.write(source, b'{"goal":"probe","assets":[]}')
            descriptors.append(source)
        socket.send_fds(connection, [(json.dumps(request) + '\\n').encode()], descriptors)
        with connection.makefile('rb') as stream:
            response = json.loads(stream.readline(65537))
sys.exit(0 if response == {'ok': True, 'exit_code': 0} else 1)
"""


def docker(*arguments, **kwargs):
    return subprocess.run(
        ["docker", *arguments], capture_output=True, text=True, check=True, timeout=90, **kwargs
    ).stdout.strip()


def client_container(image, group, uid, request, name, script, *, private=False):
    return docker(
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
        *(["--env", "HOMELAB_PROBE_REQUEST"] if private else []),
        image,
        "-I",
        "-c",
        script,
        str(uid),
        "-" if private else json.dumps(request),
        **(
            {"env": {**os.environ, "HOMELAB_PROBE_REQUEST": json.dumps(request)}} if private else {}
        ),
    )


def client(image, group, uid, request, name, *, private=False):
    return json.loads(client_container(image, group, uid, request, name, CLIENT, private=private))


def run_client(image, group, uid, request, name):
    client_container(image, group, uid, request, name, RUN_CLIENT)


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


def ownership_probe():
    """Create and clone through the socket; every write is in a task-owned container."""
    image = docker("image", "inspect", "--format", "{{.Id}}", "homelab-task:2")
    group = grp.getgrnam("homelab-launch").gr_gid
    owner = pwd.getpwnam("homelab-task")
    if owner.pw_uid in (0, 21020):
        raise RuntimeError("invalid task owner")
    token = "ownership-probe-" + uuid.uuid4().hex
    work = WORK_ROOT / token
    ids = []
    try:
        response = client(
            image, group, 21020, {"op": "task-create", "work_id": token}, token + "-creator"
        )
        if response != {"ok": True} or work.stat().st_uid != owner.pw_uid:
            raise RuntimeError("launcher did not create task-owned folder")
        if work.stat().st_mode & 0o022:
            raise RuntimeError("runner could write task folder")
        print("PASS: launcher creates work owned by the existing task uid, never runs")
        clone_id = token + "-clone"
        ids.append(clone_id)
        response = client(
            image,
            group,
            21020,
            {
                "op": "run",
                "profile": "task",
                "shape": "clone",
                "run_id": clone_id,
                "work_id": token,
                "repository": "TelesforoAleix/homelab-v2",
                "branch": "main",
            },
            token + "-cloner",
        )
        if response != {"ok": True, "exit_code": 0} or not (work / ".git").is_dir():
            raise RuntimeError("fixed clone failed")
        if (work / ".git").stat().st_uid != owner.pw_uid:
            raise RuntimeError("clone wrote as wrong owner")
        print("PASS: fixed clone uses the proxy and writes as the task uid")
        for field, value, expected in [
            ("repository", "../refused", "invalid_repository"),
            ("branch", "bad..ref", "invalid_branch"),
            ("arguments", ["arbitrary"], "invalid_fields"),
        ]:
            run_id = token + "-" + field
            ids.append(run_id)
            response = client(
                image,
                group,
                21020,
                {
                    "op": "run",
                    "profile": "task",
                    "shape": "clone",
                    "run_id": run_id,
                    "work_id": token,
                    "repository": "TelesforoAleix/homelab-v2",
                    "branch": "main",
                    field: value,
                },
                token + "-bad-" + field,
            )
            if response != {"ok": False, "error": expected}:
                raise RuntimeError("clone accepted nonfixed input")
        print("PASS: clone refuses repository, ref and command injection")
    finally:
        # A client timeout may leave the fixed clone running. Stop only this
        # probe's containers before asking the launcher to remove their work.
        for run_id in ids:
            name = "homelab-launcher-task-" + run_id
            if exists(name):
                docker("rm", "-f", name)
        response = client(
            image, group, 21020, {"op": "task-remove", "work_id": token}, token + "-remover"
        )
        if response != {"ok": True} or work.exists():
            raise RuntimeError("launcher did not remove its work folder")
        for run_id in ids:
            (STATE / (run_id + ".json")).unlink(missing_ok=True)
            (STATE / ("homelab-launcher-task-" + run_id + ".current")).unlink(missing_ok=True)
    print("PASS: ownership probe leaves no work folder")


def check_cli_mounts(arguments, read_only):
    def mounts(destination):
        return [
            value
            for flag, value in zip(arguments, arguments[1:], strict=False)
            if flag == "--mount"
            and value.startswith("type=bind,")
            and f"dst={destination}" in value.split(",")
        ]

    work = mounts("/work")
    run = mounts("/run/factory")
    if (
        len(work) != 1
        or work[0].split(",")[-1] != ("readonly" if read_only else "dst=/work")
        or work[0].split(",").count("readonly") != int(read_only)
        or len(run) != 1
        or "readonly" in run[0].split(",")
    ):
        raise RuntimeError("CLI probe mount check failed")


def readonly_probe():
    loader = importlib.machinery.SourceFileLoader("launcher", "/usr/local/sbin/homelab-launcher")
    spec = importlib.util.spec_from_loader(loader.name, loader)
    launcher = importlib.util.module_from_spec(spec)
    loader.exec_module(launcher)
    profiles, _ = launcher.load_configuration()
    image = docker("image", "inspect", "--format", "{{.Id}}", "homelab-task:2")
    group = grp.getgrnam("homelab-launch").gr_gid
    for read_only in (False, True):
        work_id = "run-" + uuid.uuid4().hex
        run_id, caller = work_id + "-help", work_id + "-client"
        container = "homelab-fence-run-" + run_id
        created = False
        try:
            if client(
                image, group, 21020, {"op": "cli-create", "work_id": work_id}, caller + "-create"
            ) != {"ok": True}:
                raise RuntimeError("cannot create CLI probe folders")
            created = True
            request = {
                "op": "run",
                "profile": "cli-run",
                "run_id": run_id,
                "folders": {"work": work_id, "run": work_id},
                "tool": "codex",
                "exports": [],
                "command": ["codex", "exec", "--help"],
            }
            if read_only:
                request["read_only"] = True
                request["stdin"] = True
            run_client(image, group, 21020, request, caller)
            arguments, _ = launcher.run_arguments(request, profiles)
            check_cli_mounts(arguments, read_only)
            print(
                "PASS: read-only cli-run runs with a read-only /work mount; "
                "run folder stays writable"
                if read_only
                else "PASS: cli-run without read_only runs with a writable /work mount"
            )
            if read_only:
                print("PASS: cli-run accepts a bounded prompt descriptor without a model call")
        finally:
            for name in (container, caller):
                if exists(name):
                    docker("rm", "-f", name)
            # Wait for the launcher to release its overlap guard after the Docker exit.
            if created:
                for _ in range(30):
                    response = client(
                        image,
                        group,
                        21020,
                        {"op": "cli-remove", "work_id": work_id},
                        caller + "-remove",
                    )
                    if response == {"ok": True}:
                        break
                    time.sleep(0.1)
                else:
                    raise RuntimeError("CLI probe cleanup failed")
            (STATE / (run_id + ".json")).unlink(missing_ok=True)
            (STATE / (container + ".current")).unlink(missing_ok=True)


def token_absent(work, needles):
    """No-follow scan of regular files, after the clone container has exited."""
    import stat

    overlap = max(map(len, needles))
    for _, _, files, directory in os.fwalk(work, follow_symlinks=False):
        for name in files:
            try:
                fd = os.open(name, os.O_RDONLY | os.O_NOFOLLOW | os.O_NONBLOCK, dir_fd=directory)
            except OSError:
                continue
            with os.fdopen(fd, "rb") as stream:
                if not stat.S_ISREG(os.fstat(stream.fileno()).st_mode):
                    continue
                previous = b""
                while chunk := stream.read(65536):
                    data = previous + chunk
                    if any(needle in data for needle in needles):
                        return False
                    previous = data[-overlap:]
    return True


def private_clone_probe():
    repository = os.environ["HOMELAB_PROBE_REPOSITORY"]
    token = os.environ["HOMELAB_PROBE_TOKEN"]
    branch = os.environ["HOMELAB_PROBE_BRANCH"]
    if not re.fullmatch(r"[A-Za-z0-9][A-Za-z0-9_-]*/[A-Za-z0-9][A-Za-z0-9._-]*", repository):
        raise RuntimeError("invalid probe repository")
    request = urllib.request.Request(
        f"https://api.github.com/repos/{repository}",
        headers={"Authorization": "Bearer " + token, "Accept": "application/vnd.github+json"},
    )
    with urllib.request.urlopen(request, timeout=30) as response:
        if json.load(response).get("private") is not True:
            raise RuntimeError("probe requires a private repository")
    image = docker("image", "inspect", "--format", "{{.Id}}", "homelab-task:2")
    group = grp.getgrnam("homelab-launch").gr_gid
    work_id = "run-" + uuid.uuid4().hex
    run_id = work_id + "-clone"
    container = "homelab-launcher-task-" + run_id
    work = Path("/srv/factory/work") / work_id
    header = (
        "Authorization: Basic " + base64.b64encode(("x-access-token:" + token).encode()).decode()
    )
    created = False
    try:
        if client(
            image, group, 21020, {"op": "cli-create", "work_id": work_id}, work_id + "-create"
        ) != {"ok": True}:
            raise RuntimeError("private clone folder creation failed")
        created = True
        request = {
            "op": "run",
            "profile": "task",
            "shape": "clone",
            "workspace": "cli",
            "run_id": run_id,
            "work_id": work_id,
            "repository": repository,
            "branch": branch,
            "env": {"GIT_CONFIG_VALUE_0": header},
        }
        if client(image, group, 21020, request, work_id + "-client", private=True) != {
            "ok": True,
            "exit_code": 0,
        }:
            raise RuntimeError("private clone failed")
        if (work / ".git").stat().st_uid != 21000 or not token_absent(
            work, [token.encode(), header.encode(), header.split()[-1].encode()]
        ):
            raise RuntimeError("private clone ownership or credential check failed")
        print("PASS: private clone uses an environment-only header and leaves no token in its copy")
    finally:
        if exists(container):
            docker("rm", "-f", container)
        if created and client(
            image, group, 21020, {"op": "cli-remove", "work_id": work_id}, work_id + "-remove"
        ) != {"ok": True}:
            raise RuntimeError("private clone cleanup failed")
        (STATE / (run_id + ".json")).unlink(missing_ok=True)
        (STATE / (container + ".current")).unlink(missing_ok=True)


if __name__ == "__main__":
    parser = argparse.ArgumentParser()
    parser.add_argument("--private", action="store_true")
    options = parser.parse_args()
    try:
        probe()
        ownership_probe()
        readonly_probe()
        if options.private:
            private_clone_probe()
    except Exception:
        # Never echo container output or exception text from a privileged probe.
        raise SystemExit(
            "FAIL: task launcher probe; inspect installation and Docker state"
        ) from None
