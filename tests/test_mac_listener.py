import importlib.machinery
import importlib.util
import json
import plistlib
import subprocess
import threading
import urllib.error
import urllib.request
from pathlib import Path

import pytest

SCRIPT = Path(__file__).resolve().parents[1] / "mac/homelab-listener"
loader = importlib.machinery.SourceFileLoader("mac_listener", str(SCRIPT))
spec = importlib.util.spec_from_loader(loader.name, loader)
listener = importlib.util.module_from_spec(spec)
loader.exec_module(listener)


@pytest.mark.parametrize("address", ["0.0.0.0", "127.0.0.1", "192.168.1.2", "::1", "", "bad"])
def test_binding_refuses_other_addresses(address, monkeypatch):
    monkeypatch.setattr(listener, "Server", lambda *a: pytest.fail("bound unsafe address"))
    with pytest.raises(ValueError):
        listener.make_server(address)


def test_binding_only_uses_tailnet_address(monkeypatch):
    address = str(listener.ipaddress.IPv4Address(0x64400001))
    calls = []

    class Server:
        def __init__(self, target, handler):
            calls.append((target, handler))

    monkeypatch.setattr(listener, "Server", Server)
    listener.make_server(address)
    assert calls == [((address, 8091), listener.Handler)]


@pytest.mark.parametrize("path", ["/infer", "/backup?path=/tmp", "/backup/", "/", "/BACKUP"])
def test_action_allowlist(path):
    assert listener.Actions().dispatch(path) == (404, {"status": "unknown"})


def test_no_card_never_launches(monkeypatch):
    monkeypatch.setattr(listener.os.path, "ismount", lambda path: False)
    monkeypatch.setattr(listener.subprocess, "Popen", lambda *a, **k: pytest.fail("ran copy"))
    assert listener.Actions().dispatch("/backup") == (200, {"status": "no-card"})


@pytest.mark.parametrize(
    "code,report,reply",
    [
        (
            0,
            b"folder: 4096 bytes\n/Volumes/SD Card/homelab-originals-2026-10-08.tar.age: "
            b"307200 encrypted bytes; confirmed on node\n",
            {"status": "done", "file": "homelab-originals-2026-10-08.tar.age", "bytes": 307200},
        ),
        (
            1,
            b"private failure",
            {"status": "failed", "reason": "check card and SSH; existing copies remain"},
        ),
        (0, b"invalid", {"status": "failed", "reason": "invalid copy report"}),
        (
            0,
            b"/Volumes/SD Card/homelab-originals-2026-10-08.tar.age: "
            b"0 encrypted bytes; confirmed on node",
            {"status": "failed", "reason": "invalid copy report"},
        ),
    ],
)
def test_copy_reports_only_confirmed_metadata(monkeypatch, code, report, reply):
    calls = []
    monkeypatch.setattr(listener.os.path, "ismount", lambda path: path == Path("/Volumes/SD Card"))

    class Process:
        returncode = code

        def communicate(self, timeout):
            assert timeout == 180
            return report, None

        def poll(self):
            return self.returncode

        def wait(self):
            return self.returncode

    def launch(args, **kwargs):
        calls.append((args, kwargs))
        return Process()

    monkeypatch.setattr(listener.subprocess, "Popen", launch)
    assert listener.Actions().dispatch("/backup") == (200, reply)
    args, kwargs = calls[0]
    assert args == [listener.sys.executable, str(SCRIPT.with_name("homelab-copy")), "SD Card"]
    assert kwargs["start_new_session"] is True
    assert kwargs["stderr"] == subprocess.DEVNULL


def test_timeout_kills_copy_and_ssh_group_before_unlock(monkeypatch):
    killed = []
    action = listener.Actions()
    monkeypatch.setattr(listener.os.path, "ismount", lambda path: True)

    class Process:
        pid = 42
        returncode = None

        def communicate(self, timeout=None):
            if timeout:
                raise subprocess.TimeoutExpired("copy", timeout)
            assert killed == [(42, listener.signal.SIGKILL)]
            self.returncode = -9
            return b"", None

        def poll(self):
            return self.returncode

        def wait(self):
            return self.returncode

    monkeypatch.setattr(listener.subprocess, "Popen", lambda *a, **kw: Process())
    monkeypatch.setattr(listener.os, "killpg", lambda *args: killed.append(args))
    assert action.dispatch("/backup")[1] == {"status": "failed", "reason": "copy timed out"}
    assert not action.lock.locked()
    assert action.process is None


def test_launch_failure_unlocks(monkeypatch):
    action = listener.Actions()
    monkeypatch.setattr(listener.os.path, "ismount", lambda path: True)

    def fail(*args, **kwargs):
        raise OSError("private exception")

    monkeypatch.setattr(listener.subprocess, "Popen", fail)
    assert action.dispatch("/backup")[1] == {"status": "failed", "reason": "could not run copy"}
    assert not action.lock.locked()


@pytest.fixture
def http_listener():
    # Test transport directly; production make_server rejects loopback.
    server = listener.Server(("127.0.0.1", 0), listener.Handler)
    server.actions = listener.Actions()
    thread = threading.Thread(target=server.serve_forever)
    thread.start()
    yield server, f"http://127.0.0.1:{server.server_port}"
    server.shutdown()
    server.server_close()
    thread.join()


def request(url, path, data=b"", method="POST"):
    opener = urllib.request.build_opener(urllib.request.ProxyHandler({}))
    req = urllib.request.Request(url + path, data=data, method=method)
    try:
        response = opener.open(req, timeout=2)
    except urllib.error.HTTPError as exc:
        response = exc
    with response:
        return response.code, json.load(response)


@pytest.mark.parametrize(
    "path,body,method",
    [
        ("/unknown", b"", "POST"),
        ("/backup", b'{"args":[]}', "POST"),
        ("/backup?args=x", b"", "POST"),
        ("/backup", None, "GET"),
    ],
)
def test_http_refuses_arguments_and_other_actions(http_listener, path, body, method):
    server, url = http_listener
    assert request(url, path, body, method)[0] == 404


def test_second_http_request_is_busy_while_copy_runs(http_listener, monkeypatch):
    server, url = http_listener
    entered, release = threading.Event(), threading.Event()

    def backup():
        entered.set()
        assert release.wait(3)
        return {"status": "no-card"}

    monkeypatch.setattr(server.actions, "backup", backup)
    replies = []
    thread = threading.Thread(target=lambda: replies.append(request(url, "/backup")))
    thread.start()
    try:
        assert entered.wait(2)
        assert request(url, "/backup") == (409, {"status": "busy"})
    finally:
        release.set()
        thread.join()
    assert replies == [(200, {"status": "no-card"})]
    assert request(url, "/backup") == (200, {"status": "no-card"})


def test_launchd_lifecycle_and_private_plist(monkeypatch, tmp_path):
    monkeypatch.setattr(listener, "PLIST", tmp_path / "LaunchAgents/listener.plist")
    monkeypatch.setattr(listener, "STATE", tmp_path / "state")
    calls = []

    def launchctl(*args, **kwargs):
        calls.append((args, kwargs))
        return subprocess.CompletedProcess(args, 0, b"state = running")

    monkeypatch.setattr(listener, "launchctl", launchctl)
    installed = tmp_path / "installed/homelab-listener"
    monkeypatch.setattr(
        listener, "installed_command", lambda name, *args: ["/fixed/python3", str(installed), *args]
    )
    listener.agent("install")
    settings = plistlib.loads(listener.PLIST.read_bytes())
    assert settings["ProgramArguments"] == ["/fixed/python3", str(installed), "serve"]
    assert settings["KeepAlive"] is True
    assert settings["RunAtLoad"] is True
    assert settings["Umask"] == 0o077
    assert listener.PLIST.stat().st_mode & 0o777 == 0o600
    assert settings["StandardOutPath"] == str(listener.STATE / "listener.log")
    assert listener.STATE.stat().st_mode & 0o777 == 0o700
    assert calls[-1][0] == ("bootstrap", f"gui/{listener.os.getuid()}", str(listener.PLIST))
    listener.agent("stop")
    assert calls[-1][0][0] == "bootout"
    listener.agent("start")
    assert calls[-1][0][0] == "bootstrap"
    listener.agent("uninstall")
    assert not listener.PLIST.exists()
    assert listener.STATE.exists()


def test_uninstall_retains_plist_if_unload_failed(monkeypatch, tmp_path):
    plist = tmp_path / "listener.plist"
    plist.write_text("installed")
    monkeypatch.setattr(listener, "PLIST", plist)
    monkeypatch.setattr(
        listener,
        "launchctl",
        lambda *args, **kwargs: subprocess.CompletedProcess(args, int(args[0] == "bootout")),
    )
    with pytest.raises(ValueError, match="retained"):
        listener.agent("uninstall")
    assert plist.exists()


def test_stop_terminates_active_copy_and_refuses_new_copy(monkeypatch):
    action = listener.Actions()
    killed = []

    class Process:
        pid = 42

        def poll(self):
            return None

    action.process = Process()
    monkeypatch.setattr(listener.os, "killpg", lambda *args: killed.append(args))
    monkeypatch.setattr(listener.os.path, "ismount", lambda path: True)
    action.stop()
    assert killed == [(42, listener.signal.SIGKILL)]
    assert action.dispatch("/backup")[1] == {"status": "failed", "reason": "copy stopped"}


@pytest.fixture
def minute_copy(monkeypatch):
    """Load the CLI without invoking main, and freeze its local wall clock."""
    import datetime
    from types import SimpleNamespace

    copy_loader = importlib.machinery.SourceFileLoader(
        "minute_copy", str(SCRIPT.with_name("homelab-copy"))
    )
    copy_spec = importlib.util.spec_from_loader(copy_loader.name, copy_loader)
    module = importlib.util.module_from_spec(copy_spec)
    copy_loader.exec_module(module)
    clock = SimpleNamespace(value=datetime.datetime(2026, 10, 8, 23, 7, 59))

    class LocalDatetime(datetime.datetime):
        @classmethod
        def now(cls, tz=None):
            assert tz is None, "copy names use local wall time"
            return clock.value

        @classmethod
        def utcnow(cls):
            pytest.fail("copy names must not use UTC")

    class LocalDate(datetime.date):
        @classmethod
        def today(cls):
            return clock.value.date()

    monkeypatch.setattr(module, "datetime", SimpleNamespace(datetime=LocalDatetime, date=LocalDate))
    return module, clock


def _minute_stream(monkeypatch, module, payload=b"encrypted", reported=9, code=0):
    calls = []
    real_fsync = module.os.fsync

    class Process:
        returncode = code

        def __init__(self, args, stdout, stderr):
            assert args == ["ssh", "homelab-agent", "sudo", "homelab-copy", "stream"]
            assert stderr == subprocess.PIPE
            calls.append("stream")
            stdout.write(payload)

        def communicate(self):
            report = {
                "bytes": reported,
                "manifest": [{"folder": "alpha-data/originals", "bytes": 8}],
            }
            return None, b"SSH warning\n" + json.dumps(report).encode()

        def poll(self):
            return self.returncode

    def fsync(fd):
        real_fsync(fd)
        calls.append("fsync")

    def confirm(args, **kwargs):
        assert args == [
            "ssh",
            "homelab-agent",
            "sudo",
            "homelab-copy",
            "confirm",
            str(len(payload)),
        ]
        assert kwargs == {"check": True}
        assert calls[-1] == "fsync"
        calls.append("confirm")

    monkeypatch.setattr(module.subprocess, "Popen", Process)
    monkeypatch.setattr(module.subprocess, "run", confirm)
    monkeypatch.setattr(module.os, "fsync", fsync)
    return calls


def test_minute_copy_local_name_and_multiple_same_day(tmp_path, monkeypatch, minute_copy, capsys):
    module, clock = minute_copy
    calls = _minute_stream(monkeypatch, module)
    legacy = tmp_path / "homelab-originals-2026-10-08.tar.age"
    legacy.write_bytes(b"legacy")
    first = module.copy(tmp_path)
    assert first == tmp_path / "homelab-originals-2026-10-08-2307.tar.age"
    assert first.read_bytes() == b"encrypted"
    assert capsys.readouterr().out == (
        f"alpha-data/originals: 8 bytes\n{first}: 9 encrypted bytes; confirmed on node\n"
    )
    clock.value = clock.value.replace(minute=8, second=0)
    second = module.copy(tmp_path)
    assert second == tmp_path / "homelab-originals-2026-10-08-2308.tar.age"
    assert second.read_bytes() == first.read_bytes() == b"encrypted"
    assert legacy.read_bytes() == b"legacy"
    assert calls == ["stream", "fsync", "confirm"] * 2


@pytest.mark.parametrize("symlink", [False, True])
def test_minute_copy_exclusive_collision(tmp_path, monkeypatch, minute_copy, symlink):
    module, _ = minute_copy
    calls = _minute_stream(monkeypatch, module)
    destination = tmp_path / "homelab-originals-2026-10-08-2307.tar.age"
    retained = tmp_path / "retained"
    retained.write_bytes(b"keep")
    if symlink:
        destination.symlink_to(retained)
    else:
        destination.write_bytes(b"keep")
    with pytest.raises(FileExistsError):
        module.copy(tmp_path)
    assert destination.read_bytes() == retained.read_bytes() == b"keep"
    assert destination.is_symlink() is symlink
    assert calls == []


@pytest.mark.parametrize(
    "payload,reported,code",
    [(b"encrypted", 10, 0), (b"encrypted", True, 0), (b"", 0, 0), (b"encrypted", 9, 1)],
)
def test_minute_copy_failure_keeps_partial_without_confirm(
    tmp_path, monkeypatch, minute_copy, payload, reported, code, capsys
):
    module, _ = minute_copy
    calls = _minute_stream(monkeypatch, module, payload, reported, code)
    with pytest.raises(ValueError):
        module.copy(tmp_path)
    assert (tmp_path / "homelab-originals-2026-10-08-2307.tar.age").read_bytes() == payload
    assert calls == ["stream", "fsync"]
    assert capsys.readouterr().out == ""


def test_minute_copy_runbook_names_and_collision_guidance():
    text = (SCRIPT.parents[1] / "docs/operations.md").read_text()
    weekly = text.split("## Weekly originals copy\n", 1)[1].split("## Restoring originals", 1)[0]
    restore = text.split("## Restoring originals from a copy\n", 1)[1].split("\n## ", 1)[0]
    assert "homelab-originals-YYYY-MM-DD-HHMM.tar.age" in weekly
    assert "local time" in weekly
    assert "same minute" in weekly
    assert "before another copy that day" not in weekly
    assert "'/Volumes/<COPY-VOLUME>/homelab-originals-YYYY-MM-DD-HHMM.tar.age'" in restore
    assert "tar -xpf - -C '/Volumes/<RESTORE-VOLUME>/restored-originals'" in restore
    assert "read -r -s -p 'age private identity: ' COPY_IDENTITY" in restore


@pytest.mark.parametrize(
    "filename,accepted",
    [
        ("homelab-originals-2026-10-08.tar.age", True),
        ("homelab-originals-2026-10-08-0000.tar.age", True),
        ("homelab-originals-2026-10-08-2307.tar.age", True),
        ("homelab-originals-2026-10-08-123.tar.age", False),
        ("homelab-originals-2026-10-08-12345.tar.age", False),
        ("homelab-originals-2026-10-08-12ab.tar.age", False),
        ("homelab-originals-2026-10-08-.tar.age", False),
        ("homelab-originals-2026-10-08-12:34.tar.age", False),
        ("homelab-originals-2026-10-08-1234.tar.age.extra", False),
        ("other-2026-10-08-1234.tar.age", False),
        ("../homelab-originals-2026-10-08-1234.tar.age", False),
    ],
)
def test_listener_copy_name_compatibility(monkeypatch, filename, accepted):
    monkeypatch.setattr(listener.os.path, "ismount", lambda path: True)

    class Process:
        returncode = 0

        def communicate(self, timeout):
            return (
                f"folder: 8 bytes\n/Volumes/SD Card/{filename}: "
                "307200 encrypted bytes; confirmed on node\n"
            ).encode(), None

        def poll(self):
            return 0

        def wait(self):
            return 0

    monkeypatch.setattr(listener.subprocess, "Popen", lambda *a, **kw: Process())
    expected = (
        {"status": "done", "file": filename, "bytes": 307200}
        if accepted
        else {"status": "failed", "reason": "invalid copy report"}
    )
    assert listener.Actions().dispatch("/backup") == (200, expected)


def test_minute_copy_preserves_encryption_and_workflow_files():
    import hashlib

    root = SCRIPT.parents[1]
    expected = {
        "node/usr/local/sbin/homelab-copy": (
            "5d3a6478dc1ceb4023db6aabf89db1c760548bbcd3af9407091167b1943e4929"
        ),
        ".github/workflows/ci.yml": (
            "caea700399131263c27fd4f2c6af1c363a3ede4707198ab063c4367de015cd8e"
        ),
    }
    assert sorted(
        p.relative_to(root).as_posix() for p in (root / ".github/workflows").iterdir()
    ) == [".github/workflows/ci.yml"]
    for path, digest in expected.items():
        assert hashlib.sha256((root / path).read_bytes()).hexdigest() == digest, path


def test_dockerfile_keeps_approved_image_configuration():
    expected = (
        (
            "# Two stages: resolve and install dependencies with uv, "
            "then copy the ready venv into a slim\n"
        )
        + """\
# runtime image that runs as an unprivileged user. Rebuilds reuse uv's cache.

FROM ghcr.io/astral-sh/uv:python3.13-bookworm-slim AS builder
ENV UV_COMPILE_BYTECODE=1 UV_LINK_MODE=copy UV_PYTHON_DOWNLOADS=0
WORKDIR /app
COPY pyproject.toml uv.lock README.md ./
RUN --mount=type=cache,target=/root/.cache/uv \\
    uv sync --locked --no-dev --no-install-project
COPY src ./src
COPY config ./config
RUN --mount=type=cache,target=/root/.cache/uv \\
    uv sync --locked --no-dev

FROM python:3.13-slim-bookworm
RUN apt-get update \\
    && apt-get install -y --no-install-recommends libpq5 \\
    && rm -rf /var/lib/apt/lists/*
RUN useradd --create-home --uid 10001 --shell /usr/sbin/nologin homelab
WORKDIR /app
COPY --from=builder --chown=homelab:homelab /app /app
COPY configurations/ /app/configurations/
ENV PATH="/app/.venv/bin:$PATH" \\
    PYTHONUNBUFFERED=1
USER homelab
EXPOSE 8000
CMD ["uvicorn", "homelab.api.app:app", "--host", "0.0.0.0", "--port", "8000"]
"""
    )
    assert (SCRIPT.parents[1] / "Dockerfile").read_text() == expected


@pytest.mark.parametrize(
    "state,health,expected_code",
    [
        ("running", "healthy", 0),
        ("running", "none", 0),
        ("running", "unhealthy", 1),
        ("running", "starting", 1),
        ("restarting", "healthy", 1),
    ],
)
def test_runbook_health_loop_running_containers(tmp_path, state, health, expected_code):
    import os
    import re
    import sys

    text = (SCRIPT.parents[1] / "docs/operations.md").read_text()
    loops = re.findall(r"^for container in \$\(docker ps [^\n]+\n.*?^done$", text, re.M | re.S)
    assert len(loops) == 1, "retain the documented container status/health loop"
    docker = tmp_path / "docker"
    docker.write_text(
        f"#!{sys.executable}\n"
        + """import json, os, sys
args = sys.argv[1:]
with open(os.environ["HEALTH_TEST_CALLS"], "a") as handle:
    handle.write(json.dumps(args) + "\\n")
if args == ["ps", "-q"]:
    print("service")
elif args == ["ps", "-aq"]:
    print("service\\nexited-one-off")
elif args[:2] == ["inspect", "-f"]:
    if args[-1] == "exited-one-off":
        print("exited" if args[2] == "{{.State.Status}}" else "healthy")
    elif args[2] == "{{.State.Status}}":
        print(os.environ["HEALTH_TEST_STATE"])
    elif args[2] == "{{if .State.Health}}{{.State.Health.Status}}{{else}}healthy{{end}}":
        health = os.environ["HEALTH_TEST_HEALTH"]
        print("healthy" if health == "none" else health)
    else:
        sys.exit(2)
else:
    sys.exit(2)
"""
    )
    docker.chmod(0o755)
    calls_file = tmp_path / "calls.jsonl"
    env = dict(
        os.environ,
        PATH=f"{tmp_path}:{os.environ['PATH']}",
        HEALTH_TEST_CALLS=str(calls_file),
        HEALTH_TEST_STATE=state,
        HEALTH_TEST_HEALTH=health,
    )
    result = subprocess.run(["bash", "-e", "-c", loops[0]], env=env, capture_output=True, text=True)
    assert result.returncode == expected_code, result.stderr
    calls = [json.loads(line) for line in calls_file.read_text().splitlines()]
    assert calls[0] == ["ps", "-q"]
    assert all("exited-one-off" not in call for call in calls)
    assert ["inspect", "-f", "{{.State.Status}}", "service"] in calls
    if state == "running":
        assert [
            "inspect",
            "-f",
            "{{if .State.Health}}{{.State.Health.Status}}{{else}}healthy{{end}}",
            "service",
        ] in calls
