"""Launcher owns task work; clone has one fixed command and one mount."""

import json
import os
import tempfile
from types import SimpleNamespace

import pytest

from tests import test_launcher
from tests.test_launcher import IMAGE, PROFILES, launcher

env = test_launcher.env


def test_launcher_creates_work_as_existing_task_account(env, tmp_path, monkeypatch):
    root = tmp_path / "task-work"
    root.mkdir()
    monkeypatch.setattr(launcher, "TASK_WORK_ROOT", root)
    monkeypatch.setattr(
        launcher.pwd, "getpwnam", lambda name: SimpleNamespace(pw_uid=21001, pw_gid=21001)
    )
    original = os.fstat

    def metadata(fd):
        values = list(original(fd))
        if values[1] == root.stat().st_ino:
            values[4] = 0
        return os.stat_result(values)

    monkeypatch.setattr(os, "fstat", metadata)
    ownership = []
    monkeypatch.setattr(os, "fchown", lambda fd, uid, gid: ownership.append((uid, gid)))
    env.service.callers["21020"] = ["task"]
    previous = os.umask(0o077)
    try:
        assert env.service.execute(21020, {"op": "task-create", "work_id": "run-1"}, []) == {
            "ok": True
        }
    finally:
        os.umask(previous)
    assert (root / "run-1").stat().st_mode & 0o777 == 0o755
    assert ownership == [(21001, 21001)]
    assert (root / "run-1").is_dir()
    assert not (root / "run-1").is_symlink()
    with pytest.raises(FileExistsError):
        env.service.execute(21020, {"op": "task-create", "work_id": "run-1"}, [])


@pytest.mark.parametrize("uid", [0, 21020])
def test_creation_refuses_root_and_runner_as_owner(env, tmp_path, monkeypatch, uid):
    monkeypatch.setattr(launcher, "TASK_WORK_ROOT", tmp_path)
    monkeypatch.setattr(launcher.pwd, "getpwnam", lambda _: SimpleNamespace(pw_uid=uid, pw_gid=uid))
    env.service.callers["21020"] = ["task"]
    with pytest.raises(launcher.Refused, match="invalid_owner"):
        env.service.execute(21020, {"op": "task-create", "work_id": "bad"}, [])
    assert not (tmp_path / "bad").exists()


def clone_request(**extra):
    return {
        "op": "run",
        "profile": "task",
        "shape": "clone",
        "run_id": "clone-1",
        "work_id": "copy",
        "repository": "owner/repo",
        "branch": "slice/test",
        **extra,
    }


def test_fixed_clone_command_and_mounts(env, tmp_path, monkeypatch):
    from tests.test_launcher_task import task_env  # noqa: F401

    root = tmp_path / "task-work"
    (root / "copy").mkdir(parents=True)
    monkeypatch.setattr(launcher, "TASK_WORK_ROOT", root)
    original = os.fstat

    def info(fd):
        values = list(original(fd))
        values[4:6] = [21001, 21001]
        return os.stat_result(values)

    monkeypatch.setattr(os, "fstat", info)
    command, _ = launcher.run_arguments(clone_request(), PROFILES)
    assert command[command.index("--entrypoint") + 1] == "/usr/bin/git"
    assert command.count("--mount") == 1
    assert command[command.index("--network") + 1] == "homelab-fence"
    assert command[command.index(IMAGE) + 1 :] == [
        "-c",
        "core.hooksPath=/dev/null",
        "-c",
        "credential.helper=",
        "clone",
        "--template=",
        "--no-tags",
        "--single-branch",
        "--branch",
        "slice/test",
        "--",
        "https://github.com/owner/repo.git",
        "/work",
    ]
    assert "/login" not in repr(command)
    assert "--interactive" not in command


@pytest.mark.parametrize(
    "updates",
    [
        {"repository": "../repo"},
        {"repository": "owner/re..po"},
        {"branch": "--upload-pack=x"},
        {"branch": "a..b"},
        {"branch": "a.lock"},
        {"branch": "a@{b"},
        {"branch": "a b"},
        {"branch": "HEAD"},
        {"arguments": ["arbitrary"]},
        {"env": {}},
        {"network": "host"},
    ],
)
def test_clone_rejects_nonfixed_input(env, updates):
    with pytest.raises(launcher.Refused):
        launcher.run_arguments(clone_request(**updates), PROFILES)


def test_new_operations_require_task_authorization(env):
    env.service.callers["21010"] = ["usage"]
    with pytest.raises(launcher.Refused, match="profile_denied"):
        env.service.execute(21010, {"op": "task-create", "work_id": "anything"}, [])


def test_live_task_excludes_another_shape_and_folder_replacement(env, monkeypatch):
    import threading
    from concurrent.futures import ThreadPoolExecutor

    env.service.callers["21020"] = ["task", "cli-run"]
    entered, release = threading.Event(), threading.Event()

    def execute(*args):
        entered.set()
        assert release.wait(3)
        return {"ok": True}

    monkeypatch.setattr(env.service, "_execute", execute)
    request = clone_request()
    with ThreadPoolExecutor() as pool:
        future = pool.submit(env.service.execute, 21020, request, [])
        assert entered.wait(3)
        try:
            for next_request in (
                {**request, "shape": "data"},
                {"op": "task-remove", "work_id": request["work_id"]},
                {"op": "task-create", "work_id": request["work_id"]},
            ):
                with pytest.raises(launcher.Refused, match="work_active"):
                    env.service.execute(21020, next_request, [])
        finally:
            release.set()
        assert future.result(timeout=3) == {"ok": True}
    assert not env.service.active_work
    assert env.service.execute(21020, request, []) == {"ok": True}


def test_cli_data_cannot_overlap_a_cli_turn_or_remove_its_folder(env, monkeypatch):
    env.service.callers["21020"] = ["task", "cli-run"]
    key = ("cli", "ticket-7-1")
    env.service.active_work.add(key)
    for request in (
        {"op": "run", "profile": "cli-run", "folders": {"work": "ticket-7-1"}},
        {"op": "run", "profile": "task", "workspace": "cli", "work_id": "ticket-7-1"},
        {"op": "cli-remove", "work_id": "ticket-7-1"},
    ):
        with pytest.raises(launcher.Refused, match="work_active"):
            env.service.execute(21020, request, [])


@pytest.mark.parametrize("read_only", [None, False, True])
def test_cli_work_read_only_is_optional_and_run_stays_writable(env, read_only):
    request = dict(env.request)
    if read_only is not None:
        request["read_only"] = read_only
    command, _ = launcher.run_arguments(request, PROFILES)
    mounts = [command[i + 1] for i, arg in enumerate(command) if arg == "--mount"]
    work = next(m for m in mounts if m.endswith("dst=/work,readonly") or m.endswith("dst=/work"))
    assert work.endswith(",readonly") is (read_only is True)
    assert f"type=bind,src={env.run},dst=/run/factory" in mounts


@pytest.mark.parametrize("value", [0, 1, "true", None, [], {}])
def test_cli_read_only_is_a_boolean(env, value):
    with pytest.raises(launcher.Refused, match="invalid_read_only"):
        launcher.run_arguments({**env.request, "read_only": value}, PROFILES)


def test_private_clone_header_is_environment_only(env, tmp_path, monkeypatch):
    import base64

    root = tmp_path / "work"
    (root / "copy").mkdir(parents=True)
    monkeypatch.setattr(launcher, "TASK_WORK_ROOT", root)
    original = os.fstat

    def info(fd):
        values = list(original(fd))
        values[4:6] = [21001, 21001]
        return os.stat_result(values)

    monkeypatch.setattr(os, "fstat", info)
    header = "Authorization: Basic " + base64.b64encode(b"x-access-token:FAKE-TOKEN").decode()
    request = clone_request(env={"GIT_CONFIG_VALUE_0": header})
    command, _ = launcher.run_arguments(request, PROFILES)
    assert header not in repr(command) and "FAKE-TOKEN" not in repr(command)
    assert "GIT_CONFIG_VALUE_0" in command
    assert "GIT_CONFIG_COUNT=1" in command
    assert "GIT_CONFIG_KEY_0=http.https://github.com/.extraHeader" in command
    assert "http.followRedirects=false" in command
    assert "/login" not in repr(command)


@pytest.mark.parametrize(
    "env_value",
    [
        {},
        {"GIT_CONFIG_VALUE_0": "bad"},
        {"GIT_CONFIG_VALUE_0": "Authorization: Basic abc\nInjected: value"},
        {"GIT_CONFIG_VALUE_0": "Authorization: Basic abc", "GIT_CONFIG_KEY_0": "include.path"},
    ],
)
def test_private_clone_refuses_nonfixed_environment(env, env_value):
    with pytest.raises(launcher.Refused):
        launcher.run_arguments(clone_request(env=env_value), PROFILES)


def test_bundle_stream_does_not_need_an_orchestrator_mount(env):
    request = {
        "op": "run",
        "profile": "bundle",
        "run_id": "export",
        "folders": env.request["folders"],
        "branch": "slice/test",
        "stream": True,
    }
    command, _ = launcher.run_arguments(request, PROFILES)
    assert command[-5:] == ["bundle", "create", "-", "HEAD", "refs/heads/slice/test"]
    assert "/run/factory/branch.bundle" not in command


def test_private_probe_passes_request_in_environment_and_scans_copy(tmp_path, monkeypatch):
    from tests import launcher_task_probe as probe

    calls = []

    def docker(*args, **kwargs):
        calls.append((args, kwargs))
        return '{"ok":true,"exit_code":0}'

    monkeypatch.setattr(probe, "docker", docker)
    request = clone_request(env={"GIT_CONFIG_VALUE_0": "Authorization: Basic PRIVATE"})
    assert probe.client(IMAGE, 21900, 21020, request, "private-probe", private=True)["ok"]
    args, kwargs = calls[0]
    assert "PRIVATE" not in repr(args)
    assert "PRIVATE" in kwargs["env"]["HOMELAB_PROBE_REQUEST"]
    assert args[-1] == "-"
    (tmp_path / "ordinary").write_bytes(b"ordinary file")
    (tmp_path / "link").symlink_to("/unreadable-secret")
    assert probe.token_absent(tmp_path, [b"PRIVATE"])
    (tmp_path / "ordinary").write_bytes(b"x" * 65533 + b"PRIVATE")
    assert not probe.token_absent(tmp_path, [b"PRIVATE"])


@pytest.mark.parametrize("stdin_flag", [None, False, True])
def test_cli_stdin_is_optional_and_adds_only_interactive(env, stdin_flag):
    request = {**env.request}
    if stdin_flag is not None:
        request["stdin"] = stdin_flag
    command, _ = launcher.run_arguments(request, PROFILES)
    assert ("--interactive" in command) is (stdin_flag is True)


@pytest.mark.parametrize("value", [None, 0, 1, "true", [], {}])
def test_cli_stdin_flag_requires_boolean(env, value):
    with pytest.raises(launcher.Refused, match="invalid_stdin"):
        launcher.run_arguments({**env.request, "stdin": value}, PROFILES)


@pytest.mark.parametrize("goal", ["x" * 200_000, "🌍" * 200_000], ids=["ascii", "unicode"])
def test_cli_stdin_accepts_contract_sized_goal_and_assets(goal):
    asset = {"text": "x" * 199_980}
    document = {"goal": goal, "assets": [asset], "work": {"allowed_files": ["src"]}}
    with tempfile.TemporaryFile() as stream:
        stream.write(json.dumps(document, ensure_ascii=False).encode())
        result = launcher.cli_input(stream.fileno()).decode()
    assert result.startswith(goal)
    assert "Run work policy" in result and "Supplied assets" in result


@pytest.mark.parametrize(
    "document",
    [
        {"goal": "x" * 200_001, "assets": []},
        {"goal": "x", "assets": [{"text": "x" * 200_000}]},
        {"goal": "x", "assets": [{}] * 33},
        {"goal": "", "assets": []},
        {"goal": "x\x00y", "assets": []},
        {"goal": "x", "assets": "bad"},
        {"goal": "x", "assets": [], "extra": "no"},
        {"goal": "x", "assets": [], "work": []},
    ],
)
def test_cli_stdin_refuses_above_contract_limits_and_malformed_input(document):
    with tempfile.TemporaryFile() as stream:
        stream.write(json.dumps(document).encode())
        with pytest.raises(launcher.Refused, match="invalid_stdin|stdin_too_large"):
            launcher.cli_input(stream.fileno())


def test_cli_stdin_is_snapshotted_validated_and_not_logged(env, monkeypatch, caplog):
    calls = []
    monkeypatch.setattr(env.service, "container_exists", lambda _: False)

    def docker(command, **kwargs):
        calls.append(command)
        assert "PRIVATE-PROMPT" not in repr(command)
        assert kwargs["stdin"].read() == b"PRIVATE-PROMPT"
        return SimpleNamespace(returncode=0)

    monkeypatch.setattr(launcher.subprocess, "run", docker)
    with tempfile.TemporaryFile() as source, open(os.devnull, "wb") as out:
        source.write(b'{"goal":"PRIVATE-PROMPT","assets":[]}')
        source.seek(0)
        request = {**env.request, "command": ["codex", "exec", "--", "-"], "stdin": True}
        assert env.service.execute(
            os.getuid(), request, [out.fileno(), out.fileno(), source.fileno()]
        ) == {"ok": True, "exit_code": 0}
    assert len(calls) == 1
    assert "PRIVATE-PROMPT" not in caplog.text
    assert all("PRIVATE-PROMPT" not in p.read_text() for p in env.state.iterdir())


def test_cli_stdin_is_rejected_before_a_container_or_record(env, monkeypatch):
    monkeypatch.setattr(env.service, "container_exists", lambda _: pytest.fail("must not inspect"))
    with tempfile.TemporaryFile() as source, open(os.devnull, "wb") as out:
        source.write(json.dumps({"goal": "x" * 200_001, "assets": []}).encode())
        request = {**env.request, "stdin": True}
        with pytest.raises(launcher.Refused):
            env.service.execute(os.getuid(), request, [out.fileno(), out.fileno(), source.fileno()])
    assert not list(env.state.iterdir())


def test_cli_stdin_rejects_nonregular_descriptors_and_oversized_envelopes():
    read, write = os.pipe()
    try:
        with pytest.raises(launcher.Refused, match="invalid_stdin"):
            launcher.cli_input(read)
    finally:
        os.close(read)
        os.close(write)
    with tempfile.TemporaryFile() as source:
        source.truncate(launcher.CLI_INPUT_BYTES + 1)
        with pytest.raises(launcher.Refused, match="stdin_too_large"):
            launcher.cli_input(source.fileno())


def test_readonly_node_probe_checks_both_actual_work_mount_modes(tmp_path, monkeypatch, capsys):
    from tests import launcher_task_probe as probe

    calls = []
    monkeypatch.setattr(probe, "STATE", tmp_path)
    monkeypatch.setattr(probe.grp, "getgrnam", lambda _: SimpleNamespace(gr_gid=21900))
    monkeypatch.setattr(probe, "client", lambda *a: {"ok": True})
    monkeypatch.setattr(probe, "exists", lambda _: False)

    def docker(*args):
        calls.append(args)
        return "0" if args[0] == "wait" else "fixture"

    monkeypatch.setattr(probe, "docker", docker)
    probe.readonly_probe()
    requests = [json.loads(args[-1]) for args in calls if args[0] == "run"]
    assert "read_only" not in requests[0]
    assert requests[1]["read_only"] is True and requests[1]["stdin"] is True
    checks = [args for args in calls if args[0] == "exec"]
    assert [args[-1] for args in checks] == ["rw", "ro"]
    assert all(probe.WRITE_CHECK in args for args in checks)
    assert "cannot write /work; run folder remains writable" in capsys.readouterr().out


def test_private_clone_forwards_only_header_environment_and_records_no_credential(
    env, tmp_path, monkeypatch
):
    import base64

    work_id = "run-" + "a" * 32
    work = launcher.fence.WORK_ROOT / work_id
    work.mkdir()
    original = os.fstat

    def metadata(fd):
        info = original(fd)
        if info.st_ino == work.stat().st_ino:
            values = list(info)
            values[4:6] = [21000, 21000]
            return os.stat_result(values)
        return info

    monkeypatch.setattr(os, "fstat", metadata)
    monkeypatch.setattr(env.service, "container_exists", lambda _: False)
    header = "Authorization: Basic " + base64.b64encode(b"x-access-token:PRIVATE-TOKEN").decode()
    calls = []

    def docker(command, **kwargs):
        calls.append((command, kwargs))
        assert kwargs["env"] == {"PATH": os.defpath, "GIT_CONFIG_VALUE_0": header}
        assert "PRIVATE-TOKEN" not in repr(command) and header not in repr(command)
        return SimpleNamespace(returncode=0)

    monkeypatch.setattr(launcher.subprocess, "run", docker)
    request = clone_request(workspace="cli", work_id=work_id, env={"GIT_CONFIG_VALUE_0": header})
    with open(os.devnull, "wb") as out:
        assert env.service.execute(os.getuid(), request, [out.fileno()] * 2)["exit_code"] == 0
    assert len(calls) == 1
    assert all(header not in path.read_text() for path in env.state.iterdir())
