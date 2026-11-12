"""Launcher boundary tests: no daemon, login contents or external service needed."""

import concurrent.futures
import contextlib
import copy
import importlib.machinery
import importlib.util
import json
import logging
import os
import socket
import subprocess
from pathlib import Path
from types import SimpleNamespace

import pytest

ROOT = Path(__file__).resolve().parents[1]
loader = importlib.machinery.SourceFileLoader(
    "homelab_launcher", str(ROOT / "node/usr/local/sbin/homelab-launcher")
)
spec = importlib.util.spec_from_loader(loader.name, loader)
launcher = importlib.util.module_from_spec(spec)
loader.exec_module(launcher)
fence = launcher.fence


@contextlib.contextmanager
def socket_pair():
    client, server = socket.socketpair()
    with client, server:
        yield client, server


PROFILES = json.loads((ROOT / "node/etc/homelab-launcher/profiles.json").read_text())
IMAGE = "sha256:" + "1" * 64


@pytest.fixture
def env(tmp_path, monkeypatch):
    for name in ("WORK_ROOT", "RUN_ROOT", "LOGIN_ROOT"):
        path = tmp_path / name
        path.mkdir()
        monkeypatch.setattr(fence, name, path)
    for tool in fence.TOOLS:
        (fence.LOGIN_ROOT / tool).mkdir(mode=0o700)
    work, run = fence.WORK_ROOT / "work", fence.RUN_ROOT / "folder"
    (work / "repos" / "project").mkdir(parents=True)
    run.mkdir()
    original_stat = Path.stat

    def login_metadata(path, *args, **kwargs):
        if path.parent == fence.LOGIN_ROOT and kwargs.get("follow_symlinks", True):
            return SimpleNamespace(st_mode=0o40700, st_uid=21000, st_gid=21000)
        return original_stat(path, *args, **kwargs)

    monkeypatch.setattr(Path, "stat", login_metadata)
    monkeypatch.setattr(launcher, "approved_image", lambda tag: IMAGE)
    state = tmp_path / "state"
    state.mkdir(mode=0o700)
    service = launcher.Launcher(PROFILES, {str(os.getuid()): list(PROFILES)}, state)
    # CI need not be root. Preserve real no-follow reads, substitute only ownership.
    original_read = launcher.protected_read

    def read(path, mode):
        if path.parent == state:
            return path.read_text()
        return original_read(path, mode)

    monkeypatch.setattr(launcher, "protected_read", read)
    request = {
        "op": "run",
        "profile": "cli-run",
        "run_id": "turn",
        "folders": {"work": "work", "run": "folder"},
        "tool": "codex",
        "command": ["codex", "exec", "--", "PRIVATE PROMPT"],
        "exports": ["project"],
    }
    return SimpleNamespace(service=service, request=request, work=work, run=run, state=state)


@pytest.fixture(params=["folder", "scope/folder", "scope/folder/io"])
def run_folder(env, request):
    env.run = fence.RUN_ROOT
    for part in request.param.split("/"):
        env.run /= part
        env.run.mkdir(mode=0o700, exist_ok=True)
    env.request["folders"]["run"] = request.param


@pytest.fixture(params=["work", "session-1/planning"])
def work_folder(env, request):
    if "/" in request.param:
        session = fence.WORK_ROOT / "session-1"
        session.mkdir(mode=0o700)
        env.work = session / "planning"
        (env.work / "repos" / "project").mkdir(parents=True)
    env.request["folders"]["work"] = request.param


@pytest.mark.parametrize("profile", ["cli-run", "chat"])
@pytest.mark.parametrize(
    "tool,command",
    [
        ("codex", ["codex", "exec", "--", "ticket"]),
        ("codex", ["codex", "debug", "models"]),
        ("claude", ["claude", "-p", "--", "ticket"]),
        ("claude", ["claude", "--print", "--", "ticket"]),
    ],
)
def test_exact_cli_argument_equality(env, run_folder, work_folder, profile, tool, command):
    request = {**env.request, "profile": profile, "tool": tool, "command": command}
    if profile == "chat":
        request["turn"] = 7
    args, name = launcher.run_arguments(request, PROFILES)
    expected = fence.docker_arguments(
        tool,
        repo=env.work,
        run=env.run,
        command=command,
        read_only_paths=[env.work / "repos/project"],
        container_name="factory-session-turn-7" if profile == "chat" else None,
    )
    image_index = expected.index(fence.IMAGE)
    expected[image_index] = IMAGE
    if profile != "chat":
        expected[image_index:image_index] = ["--name", "homelab-fence-run-turn"]
    assert args == expected
    assert name == ("factory-session-turn-7" if profile == "chat" else "homelab-fence-run-turn")
    assert request["command"] == command  # The builder never mutates caller data.


@pytest.mark.parametrize("branch", ["main", "slice/change-1"])
def test_exact_bundle_argument_equality(env, run_folder, work_folder, branch):
    request = {
        "op": "run",
        "profile": "bundle",
        "run_id": "bundle",
        "folders": env.request["folders"],
        "branch": branch,
    }
    args, name = launcher.run_arguments(request, PROFILES)
    expected = fence.bundle_arguments(repo=env.work, run=env.run, branch=branch)
    index = expected.index(fence.IMAGE)
    expected[index : index + 1] = ["--name", "homelab-fence-run-bundle", IMAGE]
    assert args == expected
    assert name == "homelab-fence-run-bundle"


def test_usage_uses_same_seal_with_only_read_only_login(env):
    args, name = launcher.run_arguments(
        {"op": "run", "profile": "usage", "run_id": "usage"}, PROFILES
    )
    assert args == [
        *fence.sealed_arguments("none"),
        "--mount",
        f"type=bind,src={fence.LOGIN_ROOT / 'codex'},dst=/login,readonly",
        "--workdir",
        "/home/agent",
        "--env",
        "HOME=/home/agent",
        "--name",
        "homelab-fence-run-usage",
        IMAGE,
        *launcher.USAGE_COMMAND,
    ]
    assert name == "homelab-fence-run-usage"
    assert PROFILES["chat"]["timeout"] == 1200


@pytest.mark.parametrize(
    "value",
    [
        "",
        ".",
        "..",
        "../x",
        "/etc",
        "a/b",
        "a,b",
        "a\nb",
        "a\rb",
        "a\0b",
        "-a",
        "_a",
        "x" * 129,
        None,
        1,
        True,
        [],
    ],
)
def test_identifier_refusals(value):
    with pytest.raises(launcher.Refused, match="invalid_identifier"):
        launcher.identifier(value)


@pytest.mark.parametrize("value", ["A", "a.b_-", "a" * 128])
def test_identifier_acceptance(value):
    assert launcher.identifier(value) == value


@pytest.mark.parametrize("profile", ["cli-run", "chat", "bundle", "usage"])
def test_every_run_field_required_and_unknown_refused(env, profile):
    requests = {
        "cli-run": env.request,
        "chat": {**env.request, "profile": "chat", "turn": 1},
        "bundle": {
            "op": "run",
            "profile": "bundle",
            "run_id": "r",
            "folders": env.request["folders"],
            "branch": "main",
        },
        "usage": {"op": "run", "profile": "usage", "run_id": "r"},
    }
    request = requests[profile]
    for key in request:
        bad = {k: v for k, v in request.items() if k != key}
        with pytest.raises(launcher.Refused):
            launcher.run_arguments(bad, PROFILES)
    for key in ("uid", "path", "image", "network", "docker_options", "unknown"):
        with pytest.raises(launcher.Refused):
            launcher.run_arguments({**request, key: "arbitrary"}, PROFILES)


@pytest.mark.parametrize(
    "key,value",
    [
        ("profile", "login"),
        ("profile", []),
        ("profile", None),
        ("tool", "sh"),
        ("tool", []),
        ("command", "codex exec"),
        ("command", []),
        ("command", [1]),
        ("command", ["codex", "exec", "\0"]),
        ("command", ["sh", "-c", "id"]),
        ("command", ["codex", "login"]),
        ("command", ["claude"]),
        ("exports", "project"),
        ("exports", [1]),
        ("exports", ["project", "project"]),
        ("exports", ["../project"]),
        ("folders", []),
        ("folders", {}),
        ("folders", {"work": "work", "run": "folder", "other": "x"}),
        ("folders", {"work": "/etc", "run": "folder"}),
        ("run_id", False),
    ],
)
def test_run_wrong_types_and_values(env, key, value):
    with pytest.raises((launcher.Refused, ValueError)):
        launcher.run_arguments({**env.request, key: value}, PROFILES)


@pytest.mark.parametrize("turn", [None, True, "1", 0, -1, 2**63, []])
def test_chat_turn_refused(env, turn):
    with pytest.raises(launcher.Refused, match="invalid_turn"):
        launcher.run_arguments({**env.request, "profile": "chat", "turn": turn}, PROFILES)


@pytest.mark.parametrize("branch", [None, 1, [], "../x", "-option", "a//b", "a..b"])
def test_bundle_branch_refused(env, branch):
    with pytest.raises((launcher.Refused, ValueError)):
        launcher.run_arguments(
            {
                "op": "run",
                "profile": "bundle",
                "run_id": "r",
                "folders": env.request["folders"],
                "branch": branch,
            },
            PROFILES,
        )


def test_paths_links_at_every_level_and_non_directories(env):
    outside = env.state / "outside"
    outside.mkdir()
    top = fence.WORK_ROOT / "link"
    top.symlink_to(outside, target_is_directory=True)
    with pytest.raises(OSError):
        launcher.folder("work", top.name)
    for target in (env.work, env.work / "repos", env.work / "repos/project", fence.WORK_ROOT):
        saved = target.with_name(target.name + "-saved")
        target.rename(saved)
        target.symlink_to(saved, target_is_directory=True)
        try:
            with pytest.raises(OSError):
                launcher.run_arguments(env.request, PROFILES)
        finally:
            target.unlink()
            saved.rename(target)
    file = fence.WORK_ROOT / "file"
    file.touch()
    for name in ("missing", "file"):
        with pytest.raises(OSError):
            launcher.folder("work", name)
    with pytest.raises(launcher.Refused, match="invalid_role"):
        launcher.folder("login", "codex")


@pytest.mark.parametrize("role", ["work", "run"])
@pytest.mark.parametrize(
    "value",
    [
        "a/b/c/d",
        "",
        "/folder",
        "scope/",
        "scope//folder",
        ".",
        "..",
        "./folder",
        "../folder",
        "scope/.",
        "scope/..",
        "scope\\folder",
        "scope/fo\\lder",
        "scope/folder/",
        "scope//io",
        "scope/folder/.",
        "scope/folder/..",
        "scope/folder/i\\o",
        *[f"{part}/folder" for part in ("-a", "_a", "a,b", "a\nb", "a\rb", "a\0b", "x" * 129)],
        *[f"scope/{part}" for part in ("-a", "_a", "a,b", "a\nb", "a\rb", "a\0b", "x" * 129)],
        *[
            f"scope/folder/{part}"
            for part in ("-a", "_a", "a,b", "a\nb", "a\rb", "a\0b", "x" * 129)
        ],
        None,
        1,
        True,
        [],
    ],
)
def test_folder_refusals(env, role, value):
    env.request["folders"][role] = value
    with pytest.raises(launcher.Refused, match="invalid_(folder|identifier)"):
        launcher.run_arguments(env.request, PROFILES)


@pytest.mark.parametrize(
    "value,component",
    [
        ("scope/run", "scope"),
        ("scope/run", "run"),
        ("scope/run/io", "scope"),
        ("scope/run/io", "run"),
        ("scope/run/io", "io"),
    ],
)
@pytest.mark.parametrize("kind", ["link", "missing", "file"])
def test_nested_run_requires_real_directories(env, value, component, kind):
    scope = fence.RUN_ROOT / "scope"
    scope.mkdir(mode=0o700)
    run = scope / "run"
    run.mkdir(mode=0o700)
    io = run / "io"
    io.mkdir()
    target = {"scope": scope, "run": run, "io": io}[component]
    saved = target.with_name(target.name + "-saved")
    target.rename(saved)
    if kind == "link":
        target.symlink_to(saved, target_is_directory=True)
    elif kind == "file":
        target.touch()
    env.request["folders"]["run"] = value
    with pytest.raises(OSError):
        launcher.run_arguments(env.request, PROFILES)
    if kind == "missing":
        assert not target.exists()


@pytest.mark.parametrize("operation", ["run", "own", "remove"])
def test_work_folder_refuses_three_parts(env, operation):
    (env.work / "nested" / "third").mkdir(parents=True)
    with pytest.raises(launcher.Refused, match="invalid_folder"):
        if operation == "run":
            env.request["folders"]["work"] = "work/nested/third"
            launcher.run_arguments(env.request, PROFILES)
        else:
            env.service.execute(
                os.getuid(),
                {"op": operation, "role": "work", "folder_id": "work/nested/third"},
                [],
            )


@pytest.mark.parametrize("component", ["session", "planning"])
@pytest.mark.parametrize("kind", ["link", "missing", "file"])
@pytest.mark.parametrize("operation", ["run", "own", "remove"])
def test_nested_work_requires_real_directories(env, component, kind, operation):
    session = fence.WORK_ROOT / "session-2"
    session.mkdir(mode=0o700)
    planning = session / "planning"
    planning.mkdir()
    target = session if component == "session" else planning
    saved = target.with_name(target.name + "-saved")
    target.rename(saved)
    if kind == "link":
        target.symlink_to(saved, target_is_directory=True)
    elif kind == "file":
        target.touch()
    if operation == "run":
        env.request["folders"]["work"] = "session-2/planning"
        with pytest.raises(OSError):
            launcher.run_arguments(env.request, PROFILES)
    else:
        with pytest.raises(OSError):
            env.service.execute(
                os.getuid(),
                {"op": operation, "role": "work", "folder_id": "session-2/planning"},
                [],
            )
    if kind == "missing":
        assert not target.exists()


@pytest.mark.parametrize("operation", ["own", "remove"])
def test_work_folder_operations(env, work_folder, monkeypatch, operation):
    changed = []
    monkeypatch.setattr(os, "chown", lambda *args, **kwargs: None)
    monkeypatch.setattr(
        os,
        "fchown",
        lambda fd, uid, gid: changed.append((Path(os.readlink(f"/proc/self/fd/{fd}")), uid, gid)),
    )
    parent = env.work.parent
    assert env.service.execute(
        os.getuid(),
        {"op": operation, "role": "work", "folder_id": env.request["folders"]["work"]},
        [],
    ) == {"ok": True}
    assert parent.is_dir()
    if operation == "own":
        assert changed == [(env.work, fence.UID, fence.GID)]
        assert (env.work / "repos/project").is_dir()
    else:
        assert not env.work.exists()


@pytest.mark.parametrize("operation", ["own", "remove", "status", "stop"])
def test_non_run_schemas(env, operation):
    base = (
        {"op": operation, "role": "work", "folder_id": "work"}
        if operation in ("own", "remove")
        else {"op": operation, "run_id": "turn"}
    )
    for key in base:
        request = {k: v for k, v in base.items() if k != key}
        with pytest.raises(launcher.Refused):
            env.service.execute(os.getuid(), request, [])
    with pytest.raises(launcher.Refused, match="invalid_fields"):
        env.service.execute(os.getuid(), {**base, "path": "/etc"}, [])
    for key in base.keys() - {"op"}:
        with pytest.raises(launcher.Refused):
            env.service.execute(os.getuid(), {**base, key: []}, [])
    with pytest.raises(launcher.Refused, match="invalid_descriptors"):
        env.service.execute(os.getuid(), base, [1])


def test_authorization_and_unknown_operations(env):
    for request in (None, [], {}, {"op": []}, {"op": 1}):
        with pytest.raises(launcher.Refused):
            env.service.execute(os.getuid(), request, [])
    for operation in ("login", "session-cleanup", "exec", ""):
        with pytest.raises(launcher.Refused, match="unknown_operation"):
            env.service.execute(os.getuid(), {"op": operation}, [])
    for operation in ("run", "own", "remove", "status", "stop"):
        with pytest.raises(launcher.Refused, match="unknown_uid"):
            env.service.execute(999999, {"op": operation}, [])
    env.service.callers[str(os.getuid())] = ["usage"]
    with pytest.raises(launcher.Refused, match="profile_denied"):
        env.service.execute(os.getuid(), env.request, [1, 2])


def test_run_descriptors(env):
    for fds in ([], [1], [1, 2, 3]):
        with pytest.raises(launcher.Refused, match="invalid_descriptors"):
            env.service.execute(os.getuid(), env.request, fds)
    with open(__file__, "rb") as read_only:
        with pytest.raises(launcher.Refused, match="invalid_descriptors"):
            env.service.execute(os.getuid(), env.request, [read_only.fileno(), 2])


def test_own_walk_no_link_following_and_top_last(env, monkeypatch):
    (env.work / "file").touch()
    outside = env.state / "outside"
    outside.mkdir()
    (outside / "untouched").touch()
    (env.work / "linked-dir").symlink_to(outside, target_is_directory=True)
    (env.work / "linked-file").symlink_to(outside / "untouched")
    calls = []

    def chown(name, uid, gid, *, dir_fd, follow_symlinks):
        assert (uid, gid) == (21000, 21000)
        assert follow_symlinks is False
        calls.append((Path(os.readlink(f"/proc/self/fd/{dir_fd}")), name))

    def fchown(fd, uid, gid):
        assert (uid, gid) == (21000, 21000)
        calls.append((Path(os.readlink(f"/proc/self/fd/{fd}")), "TOP"))

    monkeypatch.setattr(os, "chown", chown)
    monkeypatch.setattr(os, "fchown", fchown)
    launcher.own(env.work)
    assert (env.work, "linked-dir") in calls
    assert (env.work, "linked-file") in calls
    assert all(path != outside and name != "untouched" for path, name in calls)
    assert calls[-1] == (env.work, "TOP")


def test_remove_cannot_follow_links(env):
    outside = env.state / "outside"
    outside.mkdir()
    sentinel = outside / "untouched"
    sentinel.touch()
    (env.work / "linked").symlink_to(outside, target_is_directory=True)
    launcher.remove(env.work)
    assert not env.work.exists()
    assert sentinel.exists()
    linked = fence.WORK_ROOT / "top-link"
    linked.symlink_to(outside, target_is_directory=True)
    with pytest.raises(OSError):
        launcher.remove(linked)
    assert sentinel.exists()


@pytest.mark.parametrize(
    "raw,reason",
    [
        (b"{}", "incomplete_request"),
        (b"{}\n{}\n", "invalid_frame"),
        (b"{}\nmore", "invalid_frame"),
        (b"x" * 65537, "line_too_long"),
        (b"{broken}\n", "invalid_json"),
        (b"\xff\n", "invalid_json"),
        (b'{"op":"own","op":"remove"}\n', "invalid_json"),
        (b'{"x":NaN}\n', "invalid_json"),
        (b"[" * 2000 + b"\n", "invalid_json"),
    ],
    ids=[
        "incomplete",
        "two-lines",
        "suffix",
        "oversize",
        "bad-json",
        "bad-utf8",
        "duplicate",
        "nan",
        "deep",
    ],
)
def test_wire_refusals(raw, reason):
    with socket_pair() as (client, server):
        client.sendall(raw)
        client.shutdown(socket.SHUT_WR)
        with pytest.raises(launcher.Refused, match=reason):
            launcher.receive(server, [])


def test_exact_line_limit_and_fragmentation():
    raw = b'"' + b"a" * (65536 - 3) + b'"\n'
    assert len(raw) == 65536
    with socket_pair() as (client, server):
        client.sendall(raw[:10])
        client.sendall(raw[10:])
        assert launcher.receive(server, []) == "a" * (65536 - 3)


def test_excess_ancillary_descriptors_are_closed(env):
    with socket_pair() as (client, server), open("/dev/null", "wb") as output:
        socket.send_fds(client, [b"{}\n"], [output.fileno()] * 3)
        received = []
        try:
            with pytest.raises(launcher.Refused, match="invalid_descriptors"):
                launcher.receive(server, received)
        finally:
            for fd in received:
                os.close(fd)


def test_real_peer_credentials_and_fd_output(
    env, run_folder, work_folder, tmp_path, monkeypatch, caplog
):
    # A real bound Unix socket pair, accepted in the temporary folder. Do not stub
    # SO_PEERCRED, recv_fds or descriptor writes: these are the privilege boundary.
    path = tmp_path / "launcher.sock"
    calls = []

    def docker(args, **kwargs):
        calls.append((args, kwargs))
        if args[:2] == ["docker", "ps"]:
            return SimpleNamespace(stdout=b"", returncode=0)
        assert kwargs["stdin"] == subprocess.DEVNULL
        assert not kwargs.get("shell")
        os.write(kwargs["stdout"], b"PRIVATE STDOUT\n")
        os.write(kwargs["stderr"], b"PRIVATE STDERR\n")
        return SimpleNamespace(returncode=17)

    monkeypatch.setattr(subprocess, "run", docker)
    caplog.set_level(logging.INFO, logger="homelab-launcher")
    out_path, err_path = tmp_path / "out", tmp_path / "err"
    with (
        socket.socket(socket.AF_UNIX) as listener,
        socket.socket(socket.AF_UNIX) as client,
        out_path.open("wb") as out,
        err_path.open("wb") as err,
        concurrent.futures.ThreadPoolExecutor() as pool,
    ):
        listener.bind(str(path))
        listener.listen()
        client.connect(str(path))
        server, _ = listener.accept()
        with server:
            assert launcher.peer_uid(server) == os.getuid()
            future = pool.submit(env.service.handle, server)
            wire = (json.dumps(env.request) + "\n").encode()
            socket.send_fds(client, [wire[:20]], [out.fileno(), err.fileno()])
            client.sendall(wire[20:])
            response = json.loads(client.recv(65536))
            future.result(timeout=5)
    assert response == {"ok": True, "exit_code": 17}
    assert f"type=bind,src={env.run},dst=/run/factory" in calls[-1][0]
    assert out_path.read_bytes() == b"PRIVATE STDOUT\n"
    assert err_path.read_bytes() == b"PRIVATE STDERR\n"
    assert not any(text in caplog.text for text in ("PRIVATE", "ticket", "command", "/srv/"))
    assert json.loads(caplog.records[-1].message)["uid"] == os.getuid()
    for fd in (calls[-1][1]["stdout"], calls[-1][1]["stderr"]):
        with pytest.raises(OSError):
            os.fstat(fd)


def exchange(service, request, fds=()):
    with socket_pair() as (client, server):
        wire = (json.dumps(request) + "\n").encode()
        if fds:
            socket.send_fds(client, [wire], fds)
        else:
            client.sendall(wire)
        service.handle(server)
        return json.loads(client.recv(65536))


def test_content_free_errors_and_untrusted_audit(env, monkeypatch, caplog):
    caplog.set_level(logging.INFO, logger="homelab-launcher")
    for request in (
        {"op": ["PRIVATE"]},
        {"op": "PRIVATE", "profile": ["PRIVATE"]},
        {"op": "run", "run_id": "/PRIVATE", "profile": "PRIVATE"},
    ):
        assert exchange(env.service, request)["ok"] is False
    assert "PRIVATE" not in caplog.text
    monkeypatch.setattr(
        env.service,
        "execute",
        lambda *a: (_ for _ in ()).throw(OSError("PRIVATE CONTENT AND SECRETS")),
    )
    assert exchange(env.service, {}) == {"ok": False, "error": "unavailable"}
    assert "PRIVATE" not in caplog.text


def test_unknown_peer_uid_is_refused_on_socket(env):
    env.service.callers = {"999999": list(PROFILES)}
    assert exchange(env.service, {"op": "status", "run_id": "x"}) == {
        "ok": False,
        "error": "unknown_uid",
    }


def test_run_lifecycle_status_stop_and_duplicate(env, monkeypatch):
    calls = []

    def docker(args, **kwargs):
        calls.append(args)
        return SimpleNamespace(stdout=b"", returncode=0)

    monkeypatch.setattr(subprocess, "run", docker)
    with open("/dev/null", "wb") as output:
        fds = [output.fileno()] * 2
        assert env.service.execute(os.getuid(), env.request, fds) == {"ok": True, "exit_code": 0}
        with pytest.raises(launcher.Refused, match="run_exists"):
            env.service.execute(os.getuid(), env.request, fds)
    # A new instance must still know the run, including the chat name mapping.
    service = launcher.Launcher(PROFILES, env.service.callers, env.state)
    assert service.execute(os.getuid(), {"op": "status", "run_id": "turn"}, []) == {
        "ok": True,
        "running": False,
        "exit_code": 0,
    }
    monkeypatch.setattr(service, "container_exists", lambda name: True)
    assert service.execute(os.getuid(), {"op": "stop", "run_id": "turn"}, []) == {"ok": True}
    assert calls[-1] == ["docker", "rm", "-f", "homelab-fence-run-turn"]
    with pytest.raises(launcher.Refused, match="unknown_run"):
        service.execute(os.getuid(), {"op": "status", "run_id": "missing"}, [])


@pytest.mark.parametrize("cleanup_fails,code", [(False, 124), (True, 125)])
def test_chat_timeout_and_forced_cleanup(env, monkeypatch, cleanup_fails, code):
    calls = []

    def docker(args, **kwargs):
        calls.append(args)
        if args[1] == "run":
            assert kwargs["timeout"] == 1200
            raise subprocess.TimeoutExpired(args, 1200)
        if args[1] == "rm" and cleanup_fails:
            raise subprocess.CalledProcessError(1, args, stderr=b"PRIVATE")
        return SimpleNamespace(stdout=b"id" if len(calls) > 1 else b"", returncode=0)

    monkeypatch.setattr(subprocess, "run", docker)
    request = {**env.request, "profile": "chat", "turn": 7}
    with open("/dev/null", "wb") as output:
        response = env.service.execute(os.getuid(), request, [output.fileno()] * 2)
    assert response == {"ok": True, "exit_code": code}
    assert calls[-1] == ["docker", "rm", "-f", "factory-session-turn-7"]
    assert json.loads((env.state / "turn.json").read_text())["exit_code"] == code


def test_existing_container_refused(env, monkeypatch):
    monkeypatch.setattr(env.service, "container_exists", lambda name: True)
    with pytest.raises(launcher.Refused, match="run_exists"):
        env.service.execute(os.getuid(), env.request, [1, 2])


def test_status_and_stop_cannot_cross_caller_or_profile(env):
    env.service.write_record(
        "turn",
        {"profile": "chat", "name": "factory-session-turn-7", "uid": 2001, "exit_code": None},
    )
    env.service.callers["2002"] = ["chat"]
    with pytest.raises(launcher.Refused, match="run_denied"):
        env.service.execute(2002, {"op": "stop", "run_id": "turn"}, [])
    env.service.callers["2001"] = ["usage"]
    with pytest.raises(launcher.Refused, match="profile_denied"):
        env.service.execute(2001, {"op": "status", "run_id": "turn"}, [])


@pytest.mark.parametrize(
    "contents,output,error",
    [
        (IMAGE + "\n", IMAGE + "\n", None),
        ("sha256:" + "2" * 64 + "\n", IMAGE + "\n", "image_not_approved"),
        ("", IMAGE, "invalid_images"),
        ("homelab-fence:1\n", IMAGE, "invalid_images"),
        ("sha256:short\n", IMAGE, "invalid_images"),
        (IMAGE + "\n\n", IMAGE, "invalid_images"),
        (IMAGE, "not-an-id", "image_not_approved"),
    ],
)
def test_approved_images(monkeypatch, contents, output, error):
    monkeypatch.setattr(launcher, "protected_read", lambda path, mode: contents)
    calls = []

    def inspect(args, **kwargs):
        calls.append(args)
        return SimpleNamespace(stdout=output.encode())

    monkeypatch.setattr(subprocess, "run", inspect)
    if error:
        with pytest.raises(launcher.Refused, match=error):
            launcher.approved_image("homelab-fence:1")
    else:
        assert launcher.approved_image("homelab-fence:1") == IMAGE
    if calls:
        assert calls == [["docker", "image", "inspect", "--format", "{{.Id}}", "homelab-fence:1"]]


@pytest.mark.parametrize("kind", ["missing", "link", "mode", "owner", "group", "directory"])
def test_images_file_security(tmp_path, monkeypatch, kind):
    path = tmp_path / "images"
    if kind != "missing":
        path.write_text(IMAGE + "\n")
        path.chmod(0o600)
    if kind == "link":
        target = path.with_name("target")
        path.rename(target)
        path.symlink_to(target)
    elif kind == "mode":
        path.chmod(0o644)
    elif kind == "directory":
        path.unlink()
        path.mkdir()
    original_fstat = os.fstat

    def metadata(fd):
        value = original_fstat(fd)
        return SimpleNamespace(
            st_mode=value.st_mode,
            st_uid=1 if kind == "owner" else 0,
            st_gid=1 if kind == "group" else 0,
        )

    monkeypatch.setattr(os, "fstat", metadata)
    with pytest.raises((launcher.Refused, OSError)):
        launcher.protected_read(path, 0o600)


def test_configuration_and_initial_callers(monkeypatch):
    callers = {"0": list(PROFILES)}
    monkeypatch.setattr(
        launcher,
        "protected_read",
        lambda path, mode: json.dumps(PROFILES if path.name == "profiles.json" else callers),
    )
    assert launcher.load_configuration() == (PROFILES, callers)
    installed = json.loads((ROOT / "node/etc/homelab-launcher/callers.json").read_text())
    assert installed == callers
    for bad in (
        {"root": ["usage"]},
        {"1": []},
        {"1": ["unknown"]},
        {"1": "usage"},
        {"1": [None]},
        {"1": ["usage", "usage"]},
        {"01": ["usage"]},
        [],
        {str(2**32): ["usage"]},
    ):
        callers = bad
        with pytest.raises(launcher.Refused, match="invalid_configuration"):
            launcher.load_configuration()


def test_profile_metadata_cannot_widen_fixed_boundary(monkeypatch):
    for field, value in (
        ("network", "host"),
        ("image", "other:1"),
        ("limits", {}),
        ("mounts", {"host": "/:rw"}),
        ("timeout", 1),
    ):
        profiles = copy.deepcopy(PROFILES)
        profiles["cli-run"][field] = value
        monkeypatch.setattr(
            launcher, "protected_read", lambda path, mode, profiles=profiles: json.dumps(profiles)
        )
        with pytest.raises(launcher.Refused, match="invalid_configuration"):
            launcher.load_configuration()


def test_usage_returns_only_newest_valid_limits(tmp_path):
    sessions = tmp_path / "sessions"
    sessions.mkdir()
    older = sessions / "old.jsonl"
    newer = sessions / "new.jsonl"
    limits = {"primary": {"used_percent": 12}, "secondary": None}
    older.write_text(json.dumps({"payload": {"rate_limits": {"secondary": {"used_percent": 1}}}}))
    newer.write_text(
        "\n".join(
            [
                "malformed PRIVATE CONTENT",
                json.dumps({"payload": {"rate_limits": {"primary": {"used_percent": 9}}}}),
                json.dumps({"payload": {"rate_limits": limits}, "PRIVATE": "NEVER OUTPUT"}),
                json.dumps({"payload": {"rate_limits": {"primary": None, "secondary": None}}}),
                json.dumps({"payload": "PRIVATE"}),
            ]
        )
    )
    os.utime(older, ns=(1, 1))
    os.utime(newer, ns=(2, 2))
    script = launcher.USAGE_SCRIPT.replace("Path('/login/sessions')", f"Path({str(sessions)!r})")
    result = subprocess.run(["python3", "-I", "-c", script], capture_output=True, check=True)
    assert json.loads(result.stdout) == limits
    assert result.stderr == b""
    newer.unlink()
    older.unlink()
    assert subprocess.check_output(["python3", "-I", "-c", script]) == b"null\n"


def test_units_socket_permissions_and_root_service():
    socket_unit = (ROOT / "node/etc/systemd/system/homelab-launcher.socket").read_text()
    service = (ROOT / "node/etc/systemd/system/homelab-launcher.service").read_text()
    assert "ListenStream=/run/homelab-launcher.sock" in socket_unit
    assert "SocketMode=0660" in socket_unit
    assert "SocketGroup=homelab-launch" in socket_unit
    assert "User=root" in service and "Group=root" in service
    assert "RuntimeDirectoryMode=0700" in service


def test_stop_waits_for_pending_container_creation(env, monkeypatch):
    name = "homelab-fence-run-turn"
    env.service.write_record(
        "turn", {"profile": "cli-run", "name": name, "uid": os.getuid(), "exit_code": None}
    )
    env.service.active.add(name)
    exists = iter((False, True))
    calls = []
    monkeypatch.setattr(env.service, "container_exists", lambda _: next(exists))
    monkeypatch.setattr(env.service, "docker", lambda *args: calls.append(args))
    monkeypatch.setattr(launcher.time, "sleep", lambda _: None)
    assert env.service.execute(os.getuid(), {"op": "stop", "run_id": "turn"}, []) == {"ok": True}
    assert calls == [("rm", "-f", name)]


def test_stop_does_not_acknowledge_an_unstopped_launch(env, monkeypatch):
    name = "factory-session-turn-7"
    env.service.write_record(
        "turn", {"profile": "chat", "name": name, "uid": os.getuid(), "exit_code": None}
    )
    env.service.active.add(name)
    monkeypatch.setattr(env.service, "container_exists", lambda _: False)
    ticks = iter((0, 31))
    monkeypatch.setattr(launcher.time, "monotonic", lambda: next(ticks))
    with pytest.raises(launcher.Refused, match="stop_failed"):
        env.service.execute(os.getuid(), {"op": "stop", "run_id": "turn"}, [])


def test_run_name_filter_is_literal(env, monkeypatch):
    calls = []
    monkeypatch.setattr(
        env.service, "docker", lambda *args: calls.append(args) or SimpleNamespace(stdout=b"")
    )
    assert env.service.container_exists("homelab-fence-run-a.b") is False
    assert calls == [("ps", "-aq", "--filter", r"name=^/homelab\-fence\-run\-a\.b$")]


def test_descriptors_closed_on_bad_json(env):
    with socket_pair() as (client, server), open("/dev/null", "wb") as output:
        before = set(os.listdir("/proc/self/fd"))
        socket.send_fds(client, [b"{bad}\n"], [output.fileno()] * 2)
        env.service.handle(server)
        assert json.loads(client.recv(65536)) == {"ok": False, "error": "invalid_json"}
        assert set(os.listdir("/proc/self/fd")) == before


def test_usage_skips_non_utf8_and_deep_records(tmp_path):
    sessions = tmp_path / "sessions"
    sessions.mkdir()
    valid = sessions / "valid.jsonl"
    valid.write_text(json.dumps({"payload": {"rate_limits": {"primary": {"used_percent": 5}}}}))
    invalid = sessions / "invalid.jsonl"
    invalid.write_bytes(b"\xff")
    os.utime(valid, ns=(1, 1))
    os.utime(invalid, ns=(2, 2))
    script = launcher.USAGE_SCRIPT.replace("Path('/login/sessions')", f"Path({str(sessions)!r})")
    result = subprocess.run(["python3", "-I", "-c", script], capture_output=True, check=True)
    assert json.loads(result.stdout) == {"primary": {"used_percent": 5}}
    assert result.stderr == b""
    invalid.write_text("[" * 2000)
    result = subprocess.run(["python3", "-I", "-c", script], capture_output=True, check=True)
    assert json.loads(result.stdout) == {"primary": {"used_percent": 5}}
    assert result.stderr == b""


def load_probe():
    loader = importlib.machinery.SourceFileLoader(
        "homelab_check_fence", str(ROOT / "node/usr/local/sbin/homelab-check-fence")
    )
    spec = importlib.util.spec_from_loader(loader.name, loader)
    module = importlib.util.module_from_spec(spec)
    loader.exec_module(module)
    return module


def test_original_probe_pass_lines_unchanged(monkeypatch, capsys):
    probe = load_probe()
    results = iter(
        (
            SimpleNamespace(returncode=0),
            SimpleNamespace(returncode=0),
            SimpleNamespace(returncode=1, stderr="403"),
            SimpleNamespace(returncode=1),
        )
    )
    monkeypatch.setattr(probe, "curl", lambda *args: next(results))
    probe.check_fence()
    assert capsys.readouterr().out.splitlines() == [
        "PASS: api.github.com:443 answers through proxy",
        "PASS: pypi.org:443 answers through proxy",
        "PASS: example.com:443 refused with 403",
        "PASS: direct HTTPS without proxy fails",
    ]


def test_probe_container_has_only_socket_and_nonroot_uid(monkeypatch):
    probe = load_probe()
    monkeypatch.setattr(probe.grp, "getgrnam", lambda _: SimpleNamespace(gr_gid=23456))
    calls = []
    monkeypatch.setattr(
        probe.subprocess,
        "run",
        lambda args, **kwargs: calls.append((args, kwargs)) or SimpleNamespace(returncode=0),
    )
    assert probe.launcher_client(21998, {"op": "status", "run_id": "probe"}, {"ok": True})
    args, kwargs = calls[0]
    assert args[args.index("--user") + 1] == "21998:21998"
    assert args[args.index("--group-add") + 1] == "23456"
    assert args[args.index("--network") + 1] == "none"
    assert args.count("--mount") == 1
    assert args[args.index("--mount") + 1] == (
        "type=bind,src=/run/homelab-launcher.sock,dst=/run/homelab-launcher.sock"
    )
    assert kwargs["stdout"] == kwargs["stderr"] == subprocess.DEVNULL


@pytest.mark.parametrize("failed", [False, True])
def test_probe_configuration_restored_even_on_failure(tmp_path, monkeypatch, capsys, failed):
    probe = load_probe()
    for name in ("CONFIG", "WORK", "RUNS", "STATE"):
        path = tmp_path / name
        path.mkdir()
        monkeypatch.setattr(probe, "LAUNCHER_" + name, path)
    callers = json.dumps({"0": list(PROFILES)}) + "\n"
    images = IMAGE + "\n"
    (probe.LAUNCHER_CONFIG / "callers.json").write_text(callers)
    (probe.LAUNCHER_CONFIG / "images").write_text(images)
    monkeypatch.setattr(probe.os, "getuid", lambda: 0)
    monkeypatch.setattr(probe.os, "chown", lambda *args: None)
    monkeypatch.setattr(probe.pwd, "getpwuid", lambda _: (_ for _ in ()).throw(KeyError()))
    commands = []
    monkeypatch.setattr(
        probe.subprocess,
        "run",
        lambda args, **kwargs: commands.append(args) or SimpleNamespace(returncode=0),
    )
    cases = []

    def client(uid, request, expected):
        cases.append((uid, request, expected))
        configured = json.loads((probe.LAUNCHER_CONFIG / "callers.json").read_text())
        assert configured["21998"] == list(PROFILES)
        if expected.get("error") == "image_not_approved":
            assert (probe.LAUNCHER_CONFIG / "images").read_text() != images
        return not failed

    monkeypatch.setattr(probe, "launcher_client", client)
    if failed:
        with pytest.raises(SystemExit) as exc:
            probe.check_launcher()
        assert exc.value.code == 1
    else:
        probe.check_launcher()
    assert len(cases) == 10
    assert cases[-2][0] == 21999
    assert (probe.LAUNCHER_CONFIG / "callers.json").read_text() == callers
    assert (probe.LAUNCHER_CONFIG / "images").read_text() == images
    assert list(probe.LAUNCHER_WORK.iterdir()) == []
    assert list(probe.LAUNCHER_RUNS.iterdir()) == []
    assert commands.count(["systemctl", "restart", "homelab-launcher.service"]) == 2
    assert len(capsys.readouterr().out.splitlines()) == 10


def test_probe_client_script_sends_real_descriptors(env, tmp_path, monkeypatch):
    probe = load_probe()
    original_run = subprocess.run
    observed = []

    def docker(args, **kwargs):
        if args[:2] == ["docker", "run"]:
            observed.append((kwargs["stdout"], kwargs["stderr"]))
            os.write(kwargs["stdout"], b"private discarded output")
        return SimpleNamespace(returncode=0, stdout=b"")

    monkeypatch.setattr(subprocess, "run", docker)
    path = tmp_path / "probe.sock"
    script = probe.LAUNCHER_CLIENT.replace("/run/homelab-launcher.sock", str(path))
    request = {"op": "run", "profile": "usage", "run_id": "probe-script"}
    with socket.socket(socket.AF_UNIX) as listener, concurrent.futures.ThreadPoolExecutor() as pool:
        listener.bind(str(path))
        listener.listen()

        def serve():
            server, _ = listener.accept()
            with server:
                env.service.handle(server)

        future = pool.submit(serve)
        result = original_run(
            [
                "python3",
                "-I",
                "-c",
                script,
                json.dumps(request),
                json.dumps({"ok": True, "exit_code": 0}),
            ],
            capture_output=True,
            timeout=5,
            check=True,
        )
        future.result(timeout=5)
    assert result.stdout == result.stderr == b""
    assert len(observed) == 1
    assert observed[0][0] != observed[0][1]


def test_old_run_cannot_control_reused_chat_name_even_after_restart(env, monkeypatch):
    name = "factory-session-turn-7"
    env.service.write_record(
        "old", {"profile": "chat", "name": name, "uid": os.getuid(), "exit_code": None}
    )
    env.service.write_record(
        "new", {"profile": "chat", "name": name, "uid": os.getuid(), "exit_code": None}
    )
    service = launcher.Launcher(PROFILES, env.service.callers, env.state)
    monkeypatch.setattr(service, "container_exists", lambda _: True)
    calls = []
    monkeypatch.setattr(service, "docker", lambda *args: calls.append(args))
    assert service.execute(os.getuid(), {"op": "status", "run_id": "old"}, []) == {
        "ok": True,
        "running": False,
        "exit_code": None,
    }
    assert service.execute(os.getuid(), {"op": "stop", "run_id": "old"}, []) == {"ok": True}
    assert calls == []
    assert service.execute(os.getuid(), {"op": "stop", "run_id": "new"}, []) == {"ok": True}
    assert calls == [("rm", "-f", name)]


def test_peer_uid_is_unsigned():
    class Peer:
        def getsockopt(self, level, option, size):
            import struct

            return struct.pack("iII", 123, 2**32 - 2, 42)

    assert launcher.peer_uid(Peer()) == 2**32 - 2
