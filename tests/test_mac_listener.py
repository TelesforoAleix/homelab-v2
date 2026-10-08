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
    listener.agent("install")
    settings = plistlib.loads(listener.PLIST.read_bytes())
    assert settings["ProgramArguments"] == [listener.sys.executable, str(SCRIPT), "serve"]
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
