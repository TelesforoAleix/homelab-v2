"""Launcher owns task work; clone has one fixed command and one mount."""

import os
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
