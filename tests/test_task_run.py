"""The task runner: its container flags, its shell steps on a real repository, its record."""

import argparse
import hashlib
import importlib.machinery
import importlib.util
import json
import os
import subprocess
from pathlib import Path

import pytest

from tests.test_image_cost_hook import load_hook

ROOT = Path(__file__).resolve().parents[1]
loader = importlib.machinery.SourceFileLoader(
    "homelab_task_run", str(ROOT / "node/usr/local/sbin/homelab-task-run")
)
spec = importlib.util.spec_from_loader("homelab_task_run", loader)
runner = importlib.util.module_from_spec(spec)
loader.exec_module(runner)

GIT_ENV = {
    "GIT_AUTHOR_NAME": "Fixture",
    "GIT_AUTHOR_EMAIL": "fixture@example.invalid",
    "GIT_COMMITTER_NAME": "Fixture",
    "GIT_COMMITTER_EMAIL": "fixture@example.invalid",
    "GIT_CONFIG_GLOBAL": os.devnull,
    "GIT_CONFIG_NOSYSTEM": "1",
}


def git(repo, *args):
    return subprocess.run(
        ["git", *args],
        cwd=repo,
        env={**os.environ, **GIT_ENV},
        check=True,
        capture_output=True,
        text=True,
    ).stdout


@pytest.fixture
def repo(tmp_path):
    path = tmp_path / "copy"
    (path / "tests").mkdir(parents=True)
    (path / "app.py").write_text("VALUE = 1\n")
    (path / "tests/test_app.py").write_text("expected = 2\n")
    (path / ".gitignore").write_text(".venv/\n")
    git(path, "init", "-q")
    git(path, "add", ".")
    git(path, "commit", "-qm", "base")
    (path / ".venv").mkdir()
    return path


def read_only(path):
    """As the bind mount makes it: writes below it fail."""
    for folder, _, files in os.walk(path):
        os.chmod(folder, 0o555)
        for name in files:
            os.chmod(Path(folder) / name, 0o444)


def writable(path):
    for folder, _, _ in os.walk(path):
        os.chmod(folder, 0o755)


@pytest.fixture
def sealed(repo):
    read_only(repo / ".git")
    yield repo
    writable(repo / ".git")


def shell(script, repo, *args, path=None):
    env = {
        **os.environ,
        "GIT_CONFIG_GLOBAL": os.devnull,
        "GIT_CONFIG_NOSYSTEM": "1",
        "TMPDIR": str(repo.parent),
    }
    if path:
        env["PATH"] = f"{path}:{env['PATH']}"
    return subprocess.run(
        ["sh", "-c", script, "step", *args], cwd=repo, env=env, capture_output=True, text=True
    )


@pytest.fixture
def fake_uv(tmp_path):
    folder = tmp_path / "bin"
    folder.mkdir()
    (folder / "uv").write_text('#!/bin/sh\necho "uv $*"\n')
    (folder / "uv").chmod(0o755)
    return folder


def test_prepare_reports_only_the_base_commit(sealed, fake_uv):
    result = shell(runner.PREPARE, sealed, "tests/test_app.py", path=fake_uv)
    assert result.returncode == 0, result.stderr
    assert result.stdout.strip() == git(sealed, "rev-parse", "HEAD").strip()
    assert "uv sync --locked" in result.stderr


def test_prepare_refuses_a_dirty_copy_and_an_unknown_pin(repo, fake_uv):
    assert shell(runner.PREPARE, repo, "missing.py", path=fake_uv).returncode == 3
    (repo / "app.py").write_text("VALUE = 2\n")
    result = shell(runner.PREPARE, repo, path=fake_uv)
    assert result.returncode == 3
    assert "not clean" in result.stderr


def check(repo, base, command, *pins):
    return shell(runner.CHECK, repo, base, command, *pins)


def test_check_diffs_the_agents_work_then_judges_with_pinned_files(sealed):
    base = git(sealed, "rev-parse", "HEAD").strip()
    (sealed / "app.py").write_text("VALUE = 2\n")
    (sealed / "new.py").write_text("created\n")
    (sealed / "tests/test_app.py").write_text("expected = 'tampered'\n")
    (sealed / "tests/test_extra.py").write_text("planted\n")
    (sealed / ".venv/marker").write_text("not part of the change\n")

    result = check(sealed, base, "cat tests/test_app.py; ls tests; exit 7", "tests")

    assert result.returncode == 7
    # The diff shows what the agent tried, including what the restore then undoes.
    assert "+VALUE = 2" in result.stdout
    assert "new.py" in result.stdout
    assert "+expected = 'tampered'" in result.stdout
    assert "marker" not in result.stdout
    assert runner.CHECK_STARTED in result.stderr
    judged = result.stderr.split(runner.CHECK_STARTED)[1]
    assert "expected = 2" in judged and "tampered" not in judged
    assert "test_extra.py" not in judged
    assert (sealed / "app.py").read_text() == "VALUE = 2\n"


def test_an_unignored_venv_stays_out_of_the_diff(repo):
    (repo / ".gitignore").write_text("")
    git(repo, "add", ".gitignore")
    git(repo, "commit", "-qm", "no ignores")
    base = git(repo, "rev-parse", "HEAD").strip()
    (repo / ".venv/marker").write_text("installed\n")
    (repo / "app.py").write_text("VALUE = 2\n")
    result = check(repo, base, "true")
    assert result.returncode == 0, result.stderr
    assert "+VALUE = 2" in result.stdout and "marker" not in result.stdout


def test_check_output_never_enters_the_diff(sealed):
    base = git(sealed, "rev-parse", "HEAD").strip()
    result = check(sealed, base, "echo to-stdout; echo to-stderr >&2")
    assert result.returncode == 0
    assert result.stdout == ""
    assert "to-stdout" in result.stderr and "to-stderr" in result.stderr


def test_a_deleted_pin_comes_back(sealed):
    base = git(sealed, "rev-parse", "HEAD").strip()
    (sealed / "tests/test_app.py").unlink()
    result = check(sealed, base, "test -f tests/test_app.py", "tests/test_app.py")
    assert result.returncode == 0, result.stderr


def test_no_pinned_restore_or_check_runs_if_the_diff_fails(sealed):
    result = check(sealed, "0" * 40, "echo must-not-run")
    assert result.returncode != 0
    assert runner.CHECK_STARTED not in result.stderr
    assert "must-not-run" not in result.stderr


def flags(command):
    return command[: command.index(runner.IMAGE)]


def values(command, flag):
    return [command[index + 1] for index, item in enumerate(command) if item == flag]


@pytest.mark.parametrize(
    "network,venv", [("bridge", False), ("homelab-tasks", True), ("none", True)]
)
def test_every_container_has_the_same_confinement(repo, network, venv):
    command = flags(runner.container("name", repo, 1000, 1000, network, venv_read_only=venv))
    assert values(command, "--user") == ["1000:1000"]
    assert values(command, "--cap-drop") == ["ALL"]
    assert values(command, "--security-opt") == ["no-new-privileges"]
    assert "--read-only" in command
    assert [value.split(":")[0] for value in values(command, "--tmpfs")] == ["/tmp", "/home/task"]
    assert values(command, "--memory") == values(command, "--memory-swap") == [runner.MEMORY]
    assert values(command, "--cpus") == [runner.CPUS]
    assert values(command, "--pids-limit") == [runner.PIDS]
    assert values(command, "--network") == [network]
    # A json-file log would keep prompts and code on disk beyond the run folder.
    assert values(command, "--log-driver") == ["none"]
    mounts = values(command, "--mount")
    assert mounts[:2] == [
        f"type=bind,src={repo},dst=/work",
        f"type=bind,src={repo}/.git,dst=/work/.git,readonly",
    ]
    assert mounts[2:] == ([f"type=bind,src={repo}/.venv,dst=/work/.venv,readonly"] if venv else [])
    assert "--privileged" not in command and "--volume" not in command and "-v" not in command
    assert "docker.sock" not in " ".join(command)


def test_agent_ignores_what_the_folder_carries():
    for flag in [
        "--no-approve",
        "--no-extensions",
        "--no-skills",
        "--no-prompt-templates",
        "--no-themes",
        "--no-mcp",
        "--no-session",
        "--offline",
    ]:
        assert flag in runner.AGENT


def test_pi_reaches_the_purpose_with_the_token_by_name():
    provider = json.loads(runner.pi_models("task-worker"))["providers"]["homelab"]
    assert provider["baseUrl"] == "http://litellm:4000/v1"
    assert provider["api"] == "openai-completions"
    assert provider["apiKey"] == "$HOMELAB_TASK_TOKEN"
    assert [model["id"] for model in provider["models"]] == ["task-worker"]
    assert provider["models"][0]["reasoning"] is False


def test_symlinks_cannot_choose_a_bind_source(repo, tmp_path):
    (repo / ".venv").rmdir()
    (repo / ".venv").symlink_to(tmp_path)
    with pytest.raises(runner.Refused):
        runner.container("name", repo, 1000, 1000, "none", venv_read_only=True)
    plain = tmp_path / "plain"
    plain.mkdir()
    (plain / ".git").symlink_to(tmp_path)
    with pytest.raises(runner.Refused):
        runner.working_copy(str(plain))


def test_working_copy_needs_a_git_directory_and_an_unprivileged_owner(repo, tmp_path, monkeypatch):
    path, uid, gid = runner.working_copy(str(repo))
    assert (path, uid, gid) == (repo.resolve(), repo.stat().st_uid, repo.stat().st_gid)
    with pytest.raises(runner.Refused):
        runner.working_copy(str(tmp_path))
    comma = tmp_path / "a,b"
    comma.mkdir()
    with pytest.raises(runner.Refused):
        runner.working_copy(str(comma))
    info = os.stat_result((0o040755, 0, 0, 1, 0, 0, 0, 0, 0, 0))
    monkeypatch.setattr(Path, "lstat", lambda self: info)
    with pytest.raises(runner.Refused, match="unprivileged"):
        runner.working_copy(str(repo))


@pytest.mark.parametrize(
    "value",
    ["", "/etc/passwd", "../x", "a/../b", "a//b", "-x", ".git/config", ".venv/bin", "./a", "a\nb"],
)
def test_bad_pins_are_refused(value):
    with pytest.raises(runner.Refused):
        runner.pinned_path(value)


def test_good_pins_pass():
    assert runner.pinned_path("tests/test_app.py") == "tests/test_app.py"
    assert runner.pinned_path("tests") == "tests"


def test_a_token_the_script_issues_is_one_the_proxy_accepts(tmp_path, monkeypatch):
    hook = load_hook(monkeypatch)
    monkeypatch.setattr(hook, "task_token_dir", tmp_path)
    monkeypatch.setattr(runner, "TOKENS", tmp_path)
    monkeypatch.setattr(runner.os, "chown", lambda *args: None)
    path = runner.issue_token("secret-value", "task-worker", 60)
    assert path.name == hashlib.sha256(b"secret-value").hexdigest()
    assert oct(path.stat().st_mode & 0o777) == "0o640"
    assert "secret-value" not in path.read_text()
    assert [item.name for item in tmp_path.iterdir()] == [path.name]
    assert hook.task_grant("secret-value") == ["task-worker"]
    assert hook.task_grant("other") is None
    path.unlink()
    assert hook.task_grant("secret-value") is None


def test_clearing_tokens_drops_what_a_dead_run_left(tmp_path, monkeypatch):
    tokens = tmp_path / "tokens"
    tokens.mkdir()
    (tokens / "stale").write_text("{}")
    monkeypatch.setattr(runner, "TOKENS", tokens)
    monkeypatch.setattr(runner.os, "chown", lambda *args: None)
    runner.clear_tokens()
    assert list(tokens.iterdir()) == []
    assert oct(tokens.stat().st_mode & 0o777) == "0o750"


ASSISTANT = {
    "role": "assistant",
    "usage": {"input": 100, "output": 7, "cacheRead": 40, "cacheWrite": 0, "totalTokens": 147},
}


def events(path, *stops):
    lines = ['{"type":"session","version":3}', "not json", '{"type":"message_end"}']
    lines.append(json.dumps({"type": "message_end", "message": {"role": "user", "content": "x"}}))
    for stop in stops:
        message = {**ASSISTANT, "stopReason": stop}
        lines.append(json.dumps({"type": "message_end", "message": message}))
        lines.append(json.dumps({"type": "turn_end", "message": message, "toolResults": []}))
    path.write_text("\n".join(lines) + "\n")


def test_events_give_steps_tokens_and_the_last_stop(tmp_path):
    events(tmp_path / "events", "toolUse", "stop")
    assert runner.summarize_events(tmp_path / "events") == {
        "steps": 2,
        "tokens": {"fresh": 200, "output": 14, "cached": 80, "cache_write": 0},
        "last_stop": "stop",
    }


def test_scrub_removes_the_token_from_every_record(tmp_path):
    (tmp_path / "events").write_bytes(b"before TOKEN after\nTOKEN\n")
    (tmp_path / "clean").write_bytes(b"nothing here\n")
    runner.scrub([tmp_path / "events", tmp_path / "clean", tmp_path / "missing"], "TOKEN")
    assert (tmp_path / "events").read_bytes() == b"before [task-token] after\n[task-token]\n"
    assert (tmp_path / "clean").read_bytes() == b"nothing here\n"


@pytest.fixture
def fake_docker(tmp_path, monkeypatch):
    folder = tmp_path / "docker-bin"
    folder.mkdir()
    (folder / "docker").write_text(f'#!/bin/sh\necho "$@" >> {folder}/calls\n')
    (folder / "docker").chmod(0o755)
    monkeypatch.setenv("PATH", f"{folder}:{os.environ['PATH']}")
    return folder / "calls"


def test_capture_separates_streams_and_keeps_the_exit_code(tmp_path):
    result = runner.capture(
        ["sh", "-c", "echo out; echo err >&2; exit 4"],
        "name",
        tmp_path / "out",
        tmp_path / "err",
        5,
    )
    assert result == (4, "exited")
    assert (tmp_path / "out").read_text() == "out\n"
    assert (tmp_path / "err").read_text() == "err\n"


def test_capture_stops_a_container_at_the_deadline(tmp_path, fake_docker):
    result = runner.capture(
        ["sh", "-c", "echo started; exec sleep 30"],
        "name",
        tmp_path / "out",
        tmp_path / "err",
        0.3,
    )
    assert result == (None, "timeout")
    assert (tmp_path / "out").read_text() == "started\n"
    assert fake_docker.read_text() == "rm --force name\n"


def test_capture_stops_a_container_that_floods_the_disk(tmp_path, fake_docker, monkeypatch):
    monkeypatch.setattr(runner, "OUTPUT_LIMIT", 1000)
    result = runner.capture(
        ["sh", "-c", "while :; do echo xxxxxxxxxxxxxxxxxxxxxxxx; done"],
        "name",
        tmp_path / "out",
        tmp_path / "err",
        10,
    )
    assert result == (None, "output_limit")
    assert (tmp_path / "out").stat().st_size <= 1000
    assert fake_docker.read_text() == "rm --force name\n"


class Steps:
    """Replaces the containers with scripted results and records what each was given."""

    def __init__(self, tmp_path, monkeypatch, repo, *, agent=(0, "exited"), stops=("stop",)):
        self.calls = {}
        self.grants_during = {}
        self.tokens = tmp_path / "tokens"
        self.runs = tmp_path / "runs"
        self.agent, self.stops = agent, stops
        self.base = git(repo, "rev-parse", "HEAD").strip()
        self.prepare = (0, "exited")
        self.check = (0, "exited")
        self.leak = None
        monkeypatch.setattr(runner, "TOKENS", self.tokens)
        monkeypatch.setattr(runner, "RUNS", self.runs)
        monkeypatch.setattr(runner.os, "chown", lambda *args: None)
        monkeypatch.setattr(runner.os.path, "ismount", lambda path: True)
        monkeypatch.setattr(runner, "capture", self.capture)

    def capture(self, command, name, stdout, stderr, seconds, *, stdin=None, env=None):
        step = name.rsplit("-", 1)[1]
        self.calls[step] = {
            "command": command,
            "env": env or {},
            "stdin": stdin,
            "seconds": seconds,
        }
        self.grants_during[step] = sorted(path.name for path in self.tokens.iterdir())
        if stdin is not None:
            self.calls[step]["ticket"] = stdin.read()
        stdout.write_bytes(b"")
        stderr.write_bytes(b"")
        if step == "prepare":
            stdout.write_text(self.base + "\n")
            return self.prepare
        if step == "agent":
            token = env["HOMELAB_TASK_TOKEN"]
            events(stdout, *self.stops)
            if self.leak == "events":
                with open(stdout, "a") as stream:
                    stream.write(token + "\n")
            return self.agent
        stderr.write_text(runner.CHECK_STARTED + "\ncheck output\n")
        stdout.write_text("diff\n")
        if self.leak == "diff":
            stdout.write_text("+" + self.calls["agent"]["env"]["HOMELAB_TASK_TOKEN"] + "\n")
        return self.check


def arguments(repo, tmp_path, **overrides):
    ticket = tmp_path / "ticket"
    ticket.write_text("Make VALUE two.\n")
    values_ = {
        "working_copy": str(repo),
        "ticket": str(ticket),
        "check": "pytest",
        "pin": ["tests"],
        "purpose": "task-worker",
        "time_limit": 30,
    }
    return argparse.Namespace(**{**values_, **overrides})


def only_run(steps):
    (folder,) = steps.runs.iterdir()
    return folder, json.loads((folder / "run.json").read_text())


def test_a_run_leaves_its_record_and_no_token(repo, tmp_path, monkeypatch, capsys):
    steps = Steps(tmp_path, monkeypatch, repo)
    assert runner.run(arguments(repo, tmp_path)) == 0
    folder, record = only_run(steps)
    assert sorted(path.name for path in folder.iterdir()) == [
        "agent.log",
        "check.log",
        "diff.patch",
        "events.jsonl",
        "prepare.log",
        "run.json",
    ]
    assert record["ended"] == "check_passed"
    assert record["base_commit"] == steps.base
    assert record["check_exit_code"] == 0
    assert record["steps"] == 1
    assert record["tokens"] == {"fresh": 100, "output": 7, "cached": 40, "cache_write": 0}
    assert "seconds" in record and "started_at" in record and "ended_at" in record
    assert list(steps.tokens.iterdir()) == []
    assert steps.calls["agent"]["ticket"] == b"Make VALUE two.\n"
    assert steps.calls["agent"]["seconds"] == 1800
    assert json.loads(capsys.readouterr().out)["run_folder"] == str(folder)


def test_only_the_agent_has_a_token_and_only_while_it_runs(repo, tmp_path, monkeypatch):
    steps = Steps(tmp_path, monkeypatch, repo)
    runner.run(arguments(repo, tmp_path))
    token = steps.calls["agent"]["env"]["HOMELAB_TASK_TOKEN"]
    digest = hashlib.sha256(token.encode()).hexdigest()
    assert steps.grants_during == {"prepare": [], "agent": [digest], "check": []}
    assert "HOMELAB_TASK_TOKEN" not in steps.calls["prepare"]["env"]
    assert "HOMELAB_TASK_TOKEN" not in steps.calls["check"]["env"]
    for step in steps.calls.values():
        assert token not in " ".join(step["command"])
    assert values(steps.calls["prepare"]["command"], "--network") == ["bridge"]
    assert values(steps.calls["agent"]["command"], "--network") == ["homelab-tasks"]
    assert values(steps.calls["check"]["command"], "--network") == ["none"]


@pytest.mark.parametrize("leak", ["events", "diff"])
def test_a_token_the_agent_printed_is_scrubbed(repo, tmp_path, monkeypatch, leak):
    steps = Steps(tmp_path, monkeypatch, repo)
    steps.leak = leak
    runner.run(arguments(repo, tmp_path))
    folder, _ = only_run(steps)
    token = steps.calls["agent"]["env"]["HOMELAB_TASK_TOKEN"].encode()
    for path in folder.iterdir():
        assert token not in path.read_bytes()
    target = "events.jsonl" if leak == "events" else "diff.patch"
    assert b"[task-token]" in (folder / target).read_bytes()


@pytest.mark.parametrize(
    "agent,stops,check,ended,status",
    [
        ((0, "exited"), ("stop",), (1, "exited"), "check_failed", 1),
        ((0, "exited"), ("stop",), (None, "timeout"), "check_timeout", 1),
        # Pi exits 0 after exhausting its retries; the stop reason is what says it failed.
        ((0, "exited"), ("error", "error"), (0, "exited"), "agent_failed", 1),
        ((1, "exited"), ("stop",), (0, "exited"), "agent_failed", 1),
        ((None, "timeout"), ("toolUse",), (0, "exited"), "agent_timeout", 1),
        ((None, "output_limit"), ("toolUse",), (0, "exited"), "agent_output_limit", 1),
    ],
)
def test_why_a_run_ended(repo, tmp_path, monkeypatch, agent, stops, check, ended, status):
    steps = Steps(tmp_path, monkeypatch, repo, agent=agent, stops=stops)
    steps.check = check
    assert runner.run(arguments(repo, tmp_path)) == status
    folder, record = only_run(steps)
    assert record["ended"] == ended
    assert (folder / "diff.patch").read_text() == "diff\n"
    assert list(steps.tokens.iterdir()) == []


def test_a_failed_prepare_stops_before_any_token(repo, tmp_path, monkeypatch):
    steps = Steps(tmp_path, monkeypatch, repo)
    steps.prepare = (3, "exited")
    assert runner.run(arguments(repo, tmp_path)) == 1
    _, record = only_run(steps)
    assert record["ended"] == "prepare_failed"
    assert list(steps.calls) == ["prepare"]


def test_an_interrupted_run_revokes_its_token(repo, tmp_path, monkeypatch):
    steps = Steps(tmp_path, monkeypatch, repo)
    original = steps.capture

    def interrupted(command, name, *args, **kwargs):
        result = original(command, name, *args, **kwargs)
        if name.endswith("-agent"):
            raise SystemExit(143)
        return result

    monkeypatch.setattr(runner, "capture", interrupted)
    with pytest.raises(SystemExit):
        runner.run(arguments(repo, tmp_path))
    _, record = only_run(steps)
    assert record["ended"] == "interrupted"
    assert list(steps.tokens.iterdir()) == []


@pytest.mark.parametrize(
    "overrides",
    [{"purpose": "chat:high"}, {"purpose": "../x"}, {"time_limit": 0}, {"pin": ["../outside"]}],
)
def test_bad_inputs_stop_before_a_run_exists(repo, tmp_path, monkeypatch, overrides):
    steps = Steps(tmp_path, monkeypatch, repo)
    with pytest.raises(runner.Refused):
        runner.run(arguments(repo, tmp_path, **overrides))
    assert steps.calls == {} and not steps.runs.exists()


def test_a_locked_volume_stops_before_a_run_exists(repo, tmp_path, monkeypatch):
    steps = Steps(tmp_path, monkeypatch, repo)
    monkeypatch.setattr(runner.os.path, "ismount", lambda path: False)
    with pytest.raises(runner.Refused, match="locked"):
        runner.run(arguments(repo, tmp_path))
    assert steps.calls == {}


def test_only_root_runs_it(monkeypatch, capsys):
    monkeypatch.setattr(runner.os, "geteuid", lambda: 1000)
    assert runner.main(["--working-copy", "x", "--ticket", "t", "--check", "true"]) == 2
    assert "sudo" in capsys.readouterr().err


def test_a_second_run_is_refused_while_one_is_active(tmp_path, monkeypatch, capsys):
    import fcntl

    lock = tmp_path / "lock"
    monkeypatch.setattr(runner, "LOCK", lock)
    monkeypatch.setattr(runner.os, "geteuid", lambda: 0)
    monkeypatch.setattr(runner.os, "umask", lambda mask: mask)
    monkeypatch.setattr(runner.signal, "signal", lambda *args: None)
    monkeypatch.setattr(runner, "run", lambda args: pytest.fail("ran concurrently"))
    with open(lock, "w") as holder:
        fcntl.flock(holder, fcntl.LOCK_EX)
        assert runner.main(["--working-copy", "x", "--ticket", "t", "--check", "true"]) == 2
    assert "another run is active" in capsys.readouterr().err


def test_image_is_pinned_and_holds_no_secret():
    dockerfile = (ROOT / "node/opt/homelab-task/Dockerfile").read_text()
    assert "FROM node:22-bookworm-slim@sha256:" in dockerfile
    assert "npm install -g --ignore-scripts @earendil-works/pi-coding-agent@1.4.0" in dockerfile
    assert "uv==0.9.30" in dockerfile
    assert "uv python install 3.13" in dockerfile
    for package in ["git", "ripgrep"]:
        assert package in dockerfile
    assert "COPY" not in dockerfile and "ARG" not in dockerfile
