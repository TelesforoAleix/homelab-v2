"""Task profile parity and the non-root socket boundary, without a Docker daemon."""

import copy
import json
import os
import socket
import subprocess
import sys
import uuid
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path
from types import SimpleNamespace

import pytest

from tests import launcher_task_probe, test_launcher
from tests.test_launcher import IMAGE, PROFILES, ROOT, launcher
from tests.test_task_run import runner

SHAPES = {
    "prepare": {
        "network": "homelab-packages",
        "venv_read_only": False,
        "env": [
            "HTTPS_PROXY=http://package-proxy:8888",
            "UV_HTTP_TIMEOUT=120",
            "HOMELAB_PI_VERSION",
        ],
    },
    "agent": {
        "network": "homelab-tasks",
        "venv_read_only": True,
        "stdin": True,
        "env": [
            "HOMELAB_TASK_TOKEN",
            "HOMELAB_TASK_PURPOSE",
            "HOMELAB_PI_MODELS",
            "HOMELAB_PI_SETTINGS",
        ],
    },
    "review": {
        "network": "homelab-tasks",
        "venv_read_only": True,
        "work_read_only": True,
        "stdin": True,
        "env": [
            "HOMELAB_TASK_TOKEN",
            "HOMELAB_TASK_PURPOSE",
            "HOMELAB_PI_MODELS",
            "HOMELAB_PI_SETTINGS",
        ],
    },
    "check": {
        "network": "none",
        "venv_read_only": True,
        "env": ["UV_NO_SYNC", "PYTHONPYCACHEPREFIX"],
    },
    "commit": {
        "network": "none",
        "venv_read_only": True,
        "git_read_only": False,
        "env": ["PATH=/usr/bin:/bin", "GIT_AUTHOR_NAME", "GIT_AUTHOR_EMAIL"],
        "entrypoint": "/usr/bin/python3",
        "entry": ("-I", "-c"),
    },
}


env = test_launcher.env


@pytest.fixture
def task_env(env, tmp_path, monkeypatch):
    root = tmp_path / "task-work"
    work = root / "copy"
    (work / ".git").mkdir(parents=True)
    (work / ".venv").mkdir()
    monkeypatch.setattr(launcher, "TASK_WORK_ROOT", root, raising=False)
    original = os.fstat
    inode = work.stat().st_ino

    def metadata(fd):
        info = original(fd)
        if info.st_ino == inode:
            values = list(info)
            values[4:6] = [1000, 1000]
            return os.stat_result(values)
        return info

    monkeypatch.setattr(os, "fstat", metadata)
    env.task_work = work
    return env


def task_request(shape="check", **updates):
    environment = {
        "HOMELAB_PI_VERSION": "0.1",
        "HOMELAB_TASK_TOKEN": "PRIVATE TOKEN",
        "HOMELAB_TASK_PURPOSE": "task-worker",
        "HOMELAB_PI_MODELS": runner.pi_models("task-worker"),
        "HOMELAB_PI_SETTINGS": runner.pi_settings(shell=shape != "review"),
        "UV_NO_SYNC": "1",
        "PYTHONPYCACHEPREFIX": "/tmp/pycache",
        "GIT_AUTHOR_NAME": "Fixture",
        "GIT_AUTHOR_EMAIL": "fixture@example.invalid",
    }
    arguments = {
        "prepare": [runner.PREPARE, "prepare", "tests/unit.py"],
        "agent": [runner.AGENT],
        "review": [runner.REVIEWER],
        "check": [runner.CHECK, "check", "a" * 40, "uv run pytest", "tests/unit.py"],
        "commit": [
            runner.COMMIT,
            json.dumps(
                {
                    "base": "a" * 40,
                    "allowed": ["src"],
                    "pinned": ["tests/unit.py"],
                    "message_file": runner.COMMIT_MESSAGE,
                }
            ),
        ],
    }
    return {
        "op": "run",
        "profile": "task",
        "run_id": "task-" + shape,
        "work_id": "copy",
        "shape": shape,
        "env": {name: environment[name] for name in SHAPES[shape]["env"] if "=" not in name},
        "arguments": arguments[shape],
        **updates,
    }


@pytest.mark.parametrize("shape", SHAPES)
def test_task_exact_argument_equality(task_env, shape):
    request = task_request(shape)
    before = copy.deepcopy(request)
    args, name = launcher.run_arguments(request, PROFILES)
    expected = (
        runner.container("previous-name", task_env.task_work, 1000, 1000, **SHAPES[shape])
        + request["arguments"]
    )
    expected[expected.index("previous-name")] = name
    expected[expected.index(runner.IMAGE)] = IMAGE
    assert args == expected
    assert name == "homelab-launcher-task-" + request["run_id"]
    assert request == before


def test_configured_agent_script_is_data_after_fixed_entry(task_env):
    config, _, _ = runner.load_configuration(ROOT / "configurations/pi/base")
    request = task_request("agent", arguments=[runner.pi_script(config)])
    args, _ = launcher.run_arguments(request, PROFILES)
    assert args[args.index(IMAGE) + 1 :] == ["sh", "-c", *request["arguments"]]


def test_task_approves_its_own_tag_immediately_before_run(task_env, monkeypatch):
    tags = []
    monkeypatch.setattr(launcher, "approved_image", lambda tag: tags.append(tag) or IMAGE)
    args, _ = launcher.run_arguments(task_request(), PROFILES)
    assert tags == ["homelab-task:2"]
    assert args[args.index(IMAGE) + 1 :][:2] == ["sh", "-c"]


def test_task_total_data_bound(task_env):
    request = task_request(arguments=["a" * 30000, "b" * 30000])
    with pytest.raises(launcher.Refused, match="task_data_too_large"):
        launcher.run_arguments(request, PROFILES)


@pytest.mark.parametrize(
    "key,value,reason",
    [
        ("shape", "unknown", "unknown_shape"),
        ("shape", [], "unknown_shape"),
        ("work_id", "../copy", "invalid_identifier"),
        ("work_id", "/etc", "invalid_identifier"),
        ("run_id", "a/b", "invalid_identifier"),
        ("env", {"LD_PRELOAD": "PRIVATE"}, "invalid_environment"),
        ("env", {"UV_NO_SYNC": "1"}, "invalid_environment"),
        ("env", [], "invalid_environment"),
        ("arguments", [], "invalid_arguments"),
        ("arguments", "sh", "invalid_arguments"),
        ("arguments", [None], "invalid_arguments"),
        ("arguments", ["a\0b"], "invalid_arguments"),
        ("arguments", ["x" * 32769], "invalid_arguments"),
        ("arguments", ["x"] * 257, "invalid_arguments"),
    ],
)
def test_task_request_refusals(task_env, key, value, reason):
    request = task_request()
    request[key] = value
    with pytest.raises(launcher.Refused, match=reason):
        launcher.run_arguments(request, PROFILES)


@pytest.mark.parametrize("shape", SHAPES)
@pytest.mark.parametrize("value", [None, 1, True, "a\0b", "x" * 32769, "é" * 16385])
def test_environment_is_bounded_utf8_strings(task_env, shape, value):
    request = task_request(shape)
    request["env"][next(iter(request["env"]))] = value
    with pytest.raises(launcher.Refused, match="invalid_environment"):
        launcher.run_arguments(request, PROFILES)


@pytest.mark.parametrize("key", ["image", "network", "mounts", "user", "entrypoint", "stdin"])
def test_task_cannot_supply_docker_options(task_env, key):
    with pytest.raises(launcher.Refused, match="invalid_fields"):
        launcher.run_arguments(task_request(**{key: "/PRIVATE"}), PROFILES)


def test_option_like_arguments_never_become_docker_flags(task_env):
    request = task_request(arguments=["--privileged", "--mount", "/etc:/host", "other:image"])
    args, _ = launcher.run_arguments(request, PROFILES)
    assert args[args.index(IMAGE) + 1 :] == ["sh", "-c", *request["arguments"]]
    assert "--privileged" not in args[: args.index(IMAGE)]


@pytest.mark.parametrize("component", ["root", "copy", ".git", ".venv"])
@pytest.mark.parametrize("kind", ["link", "missing", "file"])
def test_task_mount_sources_are_real_directories(task_env, component, kind):
    target = {
        "root": launcher.TASK_WORK_ROOT,
        "copy": task_env.task_work,
        ".git": task_env.task_work / ".git",
        ".venv": task_env.task_work / ".venv",
    }[component]
    saved = target.with_name(target.name + "-saved")
    target.rename(saved)
    if kind == "link":
        target.symlink_to(saved, target_is_directory=True)
    elif kind == "file":
        target.touch()
    with pytest.raises((OSError, launcher.Refused)):
        launcher.run_arguments(task_request(), PROFILES)


def test_task_refuses_root_owned_work(task_env, monkeypatch):
    original = os.fstat

    def root(fd):
        values = list(original(fd))
        values[4] = 0
        return os.stat_result(values)

    monkeypatch.setattr(os, "fstat", root)
    with pytest.raises(launcher.Refused, match="invalid_owner"):
        launcher.run_arguments(task_request(), PROFILES)


@pytest.mark.parametrize("shape", SHAPES)
def test_task_descriptors_and_clean_environment(task_env, monkeypatch, shape):
    request = task_request(shape)
    task_env.service.callers["21020"] = ["task"]
    calls = []
    monkeypatch.setattr(task_env.service, "container_exists", lambda _: False)

    def run(args, **kwargs):
        calls.append((args, kwargs))
        return SimpleNamespace(returncode=0)

    monkeypatch.setattr(subprocess, "run", run)
    monkeypatch.setenv("LD_PRELOAD", "PRIVATE HOST ENVIRONMENT")
    with open(os.devnull, "wb") as out, open(os.devnull, "rb") as source:
        fds = [out.fileno()] * 2 + ([source.fileno()] if SHAPES[shape].get("stdin") else [])
        assert task_env.service.execute(21020, request, fds) == {"ok": True, "exit_code": 0}
        assert calls[0][1]["stdin"] == (source.fileno() if len(fds) == 3 else subprocess.DEVNULL)
        assert calls[0][1]["env"] == {"PATH": os.defpath, **request["env"]}
        for bad in ([out.fileno()], fds + [source.fileno()], [source.fileno()] * len(fds)):
            with pytest.raises(launcher.Refused, match="invalid_descriptors"):
                task_env.service.execute(21020, request, bad)
        if len(fds) == 3:
            with pytest.raises(launcher.Refused, match="invalid_descriptors"):
                task_env.service.execute(21020, request, [out.fileno()] * 3)
    assert not task_env.service.active


@pytest.mark.parametrize("failure", ["timeout", "start", "cleanup"])
def test_task_forces_cleanup_on_failure(task_env, monkeypatch, failure):
    task_env.service.callers["21020"] = ["task"]
    exists = iter([False, True])
    monkeypatch.setattr(task_env.service, "container_exists", lambda _: next(exists))
    calls = []

    def run(args, **kwargs):
        calls.append(args)
        if args[1] == "run":
            if failure == "timeout":
                raise subprocess.TimeoutExpired(args, 1)
            if failure == "start":
                raise OSError("PRIVATE")
            return SimpleNamespace(returncode=0)
        if failure == "cleanup":
            raise subprocess.CalledProcessError(1, args)
        return SimpleNamespace(stdout=b"")

    monkeypatch.setattr(subprocess, "run", run)
    with open(os.devnull, "wb") as out:
        if failure == "start":
            with pytest.raises(OSError):
                task_env.service.execute(21020, task_request(), [out.fileno()] * 2)
        else:
            assert task_env.service.execute(21020, task_request(), [out.fileno()] * 2) == {
                "ok": True,
                "exit_code": 124 if failure == "timeout" else 125,
            }
    assert calls[-1] == ["docker", "rm", "-f", "homelab-launcher-task-task-check"]
    assert not task_env.service.active


def test_task_metadata_cannot_widen_boundary(monkeypatch):
    for key in ("image", "limits", "user", "tmpfs", "shapes", "timeout"):
        profiles = copy.deepcopy(PROFILES)
        profiles["task"][key] = "arbitrary"
        monkeypatch.setattr(launcher, "protected_read", lambda *_, p=profiles: json.dumps(p))
        with pytest.raises(launcher.Refused, match="invalid_configuration"):
            launcher.load_configuration()


def test_nonroot_socket_boundary(task_env, monkeypatch, caplog):
    """Use real SO_PEERCRED and SCM_RIGHTS, with only Docker execution replaced."""
    uid = 21020 if os.geteuid() == 0 else os.getuid()
    task_env.service.callers = {str(uid): ["task"]}
    address = "launcher-task-" + uuid.uuid4().hex
    requests = [task_request(shape) for shape in SHAPES]
    requests += [
        task_request(shape="check", arguments=["PRIVATE"]),
        task_request(env={"OFF_LIST": "PRIVATE"}),
        task_request(work_id="../PRIVATE"),
        task_request(profile="unknown"),
        task_request(),
    ]
    requests[5]["shape"] = "unknown"
    expected = [{"ok": True, "exit_code": 0}] * 5 + [
        {"ok": False, "error": reason}
        for reason in (
            "unknown_shape",
            "invalid_environment",
            "invalid_identifier",
            "unknown_profile",
            "profile_denied",
        )
    ]
    real_run = subprocess.run
    live = set()
    started = []

    def docker(args, **kwargs):
        if args[1] == "run":
            name = args[args.index("--name") + 1]
            live.add(name)
            started.append(args)
            live.remove(name)  # Docker's --rm.
            return SimpleNamespace(returncode=0)
        return SimpleNamespace(stdout=b"")

    monkeypatch.setattr(subprocess, "run", docker)
    client_script = """import json, os, socket, sys
assert os.getuid() != 0
for request in json.loads(sys.argv[2]):
    with socket.socket(socket.AF_UNIX) as client:
        client.connect('\\0' + sys.argv[1])
        with open('/dev/null', 'wb') as out, open('/dev/null', 'rb') as source:
            fds = [out.fileno()] * 2
            if request.get('shape') in ('agent', 'review'):
                fds.append(source.fileno())
            socket.send_fds(client, [(json.dumps(request) + '\\n').encode()], fds)
            print(client.recv(65536).decode().strip())
"""

    def serve(listener):
        for index in range(len(requests)):
            connection, _ = listener.accept()
            with connection:
                assert launcher.peer_uid(connection) == uid != 0
                if index == len(requests) - 1:
                    task_env.service.callers[str(uid)] = ["usage"]
                task_env.service.handle(connection)

    caplog.set_level("INFO", logger="homelab-launcher")
    with socket.socket(socket.AF_UNIX) as listener, ThreadPoolExecutor() as pool:
        listener.bind("\0" + address)
        listener.listen()
        listener.settimeout(10)
        future = pool.submit(serve, listener)
        result = real_run(
            [sys.executable, "-I", "-c", client_script, address, json.dumps(requests)],
            capture_output=True,
            text=True,
            timeout=15,
            **({"user": uid, "group": uid, "extra_groups": []} if os.geteuid() == 0 else {}),
        )
        future.result(timeout=10)
    assert result.returncode == 0, result.stderr
    assert [json.loads(line) for line in result.stdout.splitlines()] == expected
    assert len(started) == 5
    assert not live and not task_env.service.active
    assert "PRIVATE" not in caplog.text
    assert "HOMELAB_TASK_TOKEN" not in caplog.text


def test_node_probe_client_has_only_launcher_socket(monkeypatch):
    commands = []

    def docker(*args):
        commands.append(args)
        return '{"ok": true, "exit_code": 0}'

    monkeypatch.setattr(launcher_task_probe, "docker", docker)
    assert launcher_task_probe.client(IMAGE, 21900, 21020, task_request(), "probe-client") == {
        "ok": True,
        "exit_code": 0,
    }
    args = commands[0]
    assert args[args.index("--user") + 1] == "21020:21020"
    assert args[args.index("--group-add") + 1] == "21900"
    assert args[args.index("--network") + 1] == "none"
    assert args.count("--mount") == 1
    assert args[args.index("--mount") + 1] == (
        "type=bind,src=/run/homelab-launcher.sock,dst=/run/homelab-launcher.sock"
    )
    assert args[args.index("--entrypoint") + 1 :] == (
        "/usr/bin/python3",
        IMAGE,
        "-I",
        "-c",
        launcher_task_probe.CLIENT,
        "21020",
        json.dumps(task_request()),
    )
    assert "docker.sock" not in " ".join(args)


@pytest.mark.parametrize("failure", [None, "response", "cleanup"])
def test_node_probe_cases_and_fixture_cleanup(tmp_path, monkeypatch, failure):
    probe = launcher_task_probe
    work_root, state = tmp_path / "task-work", tmp_path / "state"
    work_root.mkdir()
    state.mkdir()
    monkeypatch.setattr(probe, "WORK_ROOT", work_root)
    monkeypatch.setattr(probe, "STATE", state)
    monkeypatch.setattr(os, "geteuid", lambda: 0)
    monkeypatch.setattr(os.path, "ismount", lambda _: True)
    monkeypatch.setattr(os, "chown", lambda *args: None)
    monkeypatch.setattr(probe.grp, "getgrnam", lambda _: SimpleNamespace(gr_gid=21900))
    monkeypatch.setattr(probe.pwd, "getpwnam", lambda _: SimpleNamespace(pw_uid=1000, pw_gid=1000))
    original_read = Path.read_text
    monkeypatch.setattr(
        Path,
        "read_text",
        lambda path, *a, **kw: (
            IMAGE if str(path) == "/etc/homelab-launcher/images" else original_read(path, *a, **kw)
        ),
    )
    docker_calls, client_calls = [], []

    def docker(*args):
        docker_calls.append(args)
        if args[0] == "image":
            return IMAGE
        if args[0] == "rm":
            if failure == "cleanup":
                raise subprocess.CalledProcessError(1, args)
            return ""
        return "id" if failure == "cleanup" else ""

    def client(image, group, uid, request, name):
        client_calls.append((uid, request, name))
        (state / (request["run_id"] + ".json")).write_text("fixture")
        (state / ("homelab-launcher-task-" + request["run_id"] + ".current")).touch()
        if failure == "response":
            return {"ok": False}
        reasons = {
            1: "invalid_environment",
            2: "unknown_shape",
            3: "invalid_identifier",
            4: "unknown_profile",
            5: "profile_denied",
        }
        index = len(client_calls) - 1
        return {"ok": False, "error": reasons[index]} if index else {"ok": True, "exit_code": 0}

    monkeypatch.setattr(probe, "docker", docker)
    monkeypatch.setattr(probe, "client", client)
    if failure:
        with pytest.raises(RuntimeError):
            probe.probe()
    else:
        probe.probe()
        assert [uid for uid, _, _ in client_calls] == [21020] * 5 + [21010]
        assert client_calls[0][1]["shape"] == "check"
        assert client_calls[1][1]["env"] == {"LD_PRELOAD": "refused"}
    if failure == "cleanup":
        assert len([args for args in docker_calls if args[0] == "rm"]) == 2
    assert list(work_root.iterdir()) == list(state.iterdir()) == []
