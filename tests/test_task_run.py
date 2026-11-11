"""The task runner: its container flags, its shell steps on a real repository, its record."""

import argparse
import hashlib
import importlib.machinery
import importlib.util
import json
import os
import re
import shlex
import subprocess
import sys
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


@pytest.fixture(autouse=True)
def configuration_environment(monkeypatch):
    monkeypatch.setattr(runner, "DEFAULT_CONFIGURATION", ROOT / "configurations/pi/base")
    monkeypatch.setattr(runner, "IMAGE", "homelab-task:2")
    monkeypatch.setattr(runner, "image_digest", lambda image: "sha256:" + "a" * 64)


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


def flaky_uv(folder, failures):
    """A `uv` that fails `failures` times, counting calls in a file; `sleep` is a no-op."""
    (folder / "sleep").write_text("#!/bin/sh\n")
    (folder / "sleep").chmod(0o755)
    (folder / "uv").write_text(
        f'#!/bin/sh\necho x >> "{folder}/calls"\n[ "$(wc -l < "{folder}/calls")" -gt {failures} ]\n'
    )
    return folder / "calls"


def test_prepare_retries_uv_sync_and_then_passes(sealed, fake_uv):
    calls = flaky_uv(fake_uv, 2)
    result = shell(runner.PREPARE, sealed, path=fake_uv)
    assert result.returncode == 0, result.stderr
    assert result.stdout.strip() == git(sealed, "rev-parse", "HEAD").strip()
    assert len(calls.read_text().split()) == 3


def test_prepare_gives_up_after_three_tries(sealed, fake_uv):
    calls = flaky_uv(fake_uv, 99)
    result = shell(runner.PREPARE, sealed, path=fake_uv)
    assert result.returncode != 0
    assert result.stdout == ""
    assert len(calls.read_text().split()) == 3


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
    "network,venv", [("homelab-packages", False), ("homelab-tasks", True), ("none", True)]
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


def test_a_read_only_work_dir_is_one_read_only_mount(repo):
    command = flags(
        runner.container(
            "name", repo, 1000, 1000, "homelab-tasks", venv_read_only=True, work_read_only=True
        )
    )
    assert values(command, "--mount") == [f"type=bind,src={repo},dst=/work,readonly"]
    # Everything else about its confinement is the same.
    assert values(command, "--cap-drop") == ["ALL"] and "--read-only" in command
    assert [value.split(":")[0] for value in values(command, "--tmpfs")] == ["/tmp", "/home/task"]


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
    monkeypatch.setattr(runner.os, "chown", lambda *args, **kwargs: None)
    path = runner.issue_token("secret-value", "task-worker", 60, 100)
    assert path.name == hashlib.sha256(b"secret-value").hexdigest()
    assert oct(path.stat().st_mode & 0o777) == "0o640"
    assert "secret-value" not in path.read_text()
    assert [item.name for item in tmp_path.iterdir()] == [path.name]
    assert hook.task_grant("secret-value")["purposes"] == ["task-worker"]
    assert hook.task_grant("secret-value")["max_requests"] == 100
    assert hook.task_grant("other") is None
    path.unlink()
    assert hook.task_grant("secret-value") is None


def test_clearing_tokens_drops_what_a_dead_run_left(tmp_path, monkeypatch):
    tokens = tmp_path / "tokens"
    tokens.mkdir()
    (tokens / "stale").write_text("{}")
    monkeypatch.setattr(runner, "TOKENS", tokens)
    monkeypatch.setattr(runner.os, "chown", lambda *args, **kwargs: None)
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
    lines.append(json.dumps({"type": "agent_settled", "aborted": False}))
    path.write_text("\n".join(lines) + "\n")


def test_events_give_steps_tokens_and_the_last_stop(tmp_path):
    events(tmp_path / "events", "toolUse", "stop")
    assert runner.summarize_events(tmp_path / "events") == {
        "steps": 2,
        "tokens": {"fresh": 200, "output": 14, "cached": 80, "cache_write": 0},
        "last_stop": "stop",
        "budget_spent": False,
        "compactions": 0,
        "retries": 0,
        "end_event": {"type": "agent_settled", "aborted": False},
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


def reply(text, stop="stop", blocks=None):
    """A reviewer's events: one tool-using turn, then a final reply of the given text."""
    content = blocks if blocks is not None else [{"type": "text", "text": text}]
    first = {**ASSISTANT, "stopReason": "toolUse", "content": [{"type": "toolCall"}]}
    last = {**ASSISTANT, "stopReason": stop, "content": content}
    return [
        {"type": "message_end", "message": first},
        {"type": "turn_end", "message": first},
        {"type": "message_end", "message": last},
        {"type": "turn_end", "message": last},
        {"type": "agent_settled", "aborted": False},
    ]


class Steps:
    """Replaces the containers with scripted results and records what each was given.

    Steps are named `prepare`, `agent`, `check`, `review`, then `agent2`, `check2`, `review2` for a
    fix round, and `commit`.
    """

    def __init__(self, tmp_path, monkeypatch, repo, *, agent=(0, "exited"), stops=("stop",)):
        self.calls = {}
        self.grants_during = {}
        self.granted = {}
        self.tokens = tmp_path / "tokens"
        self.costs = tmp_path / "costs"
        # Per step, the lines the proxy would have appended for that container's token.
        self.billed = {}
        self.runs = tmp_path / "runs"
        self.agent, self.stops = agent, stops
        self.commit, self.commit_output = (0, "exited"), "commit " + "c" * 40
        self.base = git(repo, "rev-parse", "HEAD").strip()
        self.prepare = (0, "exited")
        self.check = (0, "exited")
        self.check2 = (0, "exited")
        self.agent2 = (0, "exited")
        self.diff = "diff\n"
        # One item per review the run asks for: reply text, or a dict of events and the exit.
        self.reviews = []
        self.leak = None
        monkeypatch.setattr(runner, "TOKENS", self.tokens)
        monkeypatch.setattr(runner, "COSTS", self.costs)
        monkeypatch.setattr(runner, "RUNS", self.runs)
        monkeypatch.setattr(runner.os, "chown", lambda *args, **kwargs: None)
        monkeypatch.setattr(runner.os.path, "ismount", lambda path: True)
        monkeypatch.setattr(runner, "capture", self.capture)

    def capture(self, command, name, stdout, stderr, seconds, *, stdin=None, env=None):
        step = name.rsplit("-", 1)[1]
        self.calls[step] = {
            "command": command,
            "env": env or {},
            "stdin": stdin,
            "seconds": seconds,
            "files": (stdout.name, stderr.name),
        }
        self.grants_during[step] = sorted(path.name for path in self.tokens.iterdir())
        self.granted[step] = [json.loads(path.read_text()) for path in self.tokens.iterdir()]
        if stdin is not None:
            self.calls[step]["ticket"] = stdin.read()
        stdout.write_bytes(b"")
        stderr.write_bytes(b"")
        if step in self.billed:
            digest = hashlib.sha256(env["HOMELAB_TASK_TOKEN"].encode()).hexdigest()
            (self.costs / digest).write_text("".join(line + "\n" for line in self.billed[step]))
        if step == "prepare":
            stdout.write_text(self.base + "\n")
            return self.prepare
        if step in {"agent", "agent2"}:
            token = env["HOMELAB_TASK_TOKEN"]
            events(stdout, *self.stops)
            if self.leak in {"events", "all"}:
                with open(stdout, "a") as stream:
                    stream.write(token + "\n")
            return self.agent if step == "agent" else self.agent2
        if step in {"review", "review2"}:
            item = self.reviews.pop(0)
            if isinstance(item, str):
                item = {"events": reply(item)}
            stdout.write_text("".join(json.dumps(event) + "\n" for event in item["events"]))
            if self.leak == "all":
                with open(stdout, "a") as stream:
                    stream.write(env["HOMELAB_TASK_TOKEN"] + "\n")
            return item.get("exit", (0, "exited"))
        if step == "commit":
            stdout.write_text(self.commit_output + "\n")
            return self.commit
        stderr.write_text(runner.CHECK_STARTED + "\ncheck output of " + step + "\n")
        diff = self.diff if isinstance(self.diff, bytes) else self.diff.encode()
        stdout.write_bytes(diff if step == "check" else diff + b"second\n")
        if self.leak == "diff":
            stdout.write_text("+" + self.calls["agent"]["env"]["HOMELAB_TASK_TOKEN"] + "\n")
        return self.check if step == "check" else self.check2


def arguments(repo, tmp_path, **overrides):
    ticket = tmp_path / "ticket"
    ticket.write_text("Make VALUE two.\n")
    values_ = {
        "working_copy": str(repo),
        "ticket": str(ticket),
        "check": "pytest",
        "pin": ["tests"],
        "purpose": "task-worker",
        "cli_model": None,
        "cli_effort": "medium",
        "cli_name": None,
        "review_purpose": None,
        "time_limit": 30,
        "max_requests": 100,
        "allow": [],
        "author_name": None,
        "author_email": None,
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
    assert values(steps.calls["prepare"]["command"], "--network") == ["homelab-packages"]
    assert values(steps.calls["prepare"]["command"], "--env") == [
        "HTTPS_PROXY=http://package-proxy:8888",
        "UV_HTTP_TIMEOUT=120",
        "HOMELAB_PI_VERSION",
    ]
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
        ((0, "exited"), ("error", "error"), (0, "exited"), "model_error", 1),
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
    [
        {"purpose": "chat:high"},
        {"purpose": "../x"},
        {"time_limit": 0},
        {"pin": ["../outside"]},
        {"max_requests": 0},
        {"allow": ["../outside"]},
        {"allow": ["src"]},
    ],
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
    for package in ["git", "libpq5", "ripgrep"]:
        assert package in dockerfile
    assert "COPY" not in dockerfile and "ARG" not in dockerfile


def test_the_image_tag_follows_the_dockerfile_change():
    assert runner.IMAGE == "homelab-task:2"


def test_a_grant_carries_the_request_budget(tmp_path, monkeypatch):
    monkeypatch.setattr(runner, "TOKENS", tmp_path)
    monkeypatch.setattr(runner.os, "chown", lambda *args, **kwargs: None)
    path = runner.issue_token("token", "task-worker", 60, 25)
    grant = json.loads(path.read_text())
    assert grant["max_requests"] == 25 and grant["purposes"] == ["task-worker"]


def test_the_default_budget_is_a_hundred_requests(repo, tmp_path, monkeypatch):
    assert (
        runner.parse(["--working-copy", "x", "--ticket", "t", "--check", "c"]).max_requests == 100
    )


def test_a_spent_budget_is_its_own_ending(repo, tmp_path, monkeypatch):
    steps = Steps(tmp_path, monkeypatch, repo, stops=("stop",))
    original = steps.capture

    def spent(command, name, stdout, stderr, seconds, **kwargs):
        result = original(command, name, stdout, stderr, seconds, **kwargs)
        if name.endswith("-agent"):
            stdout.write_text(
                json.dumps(
                    {
                        "type": "message_end",
                        "message": {
                            "role": "assistant",
                            "stopReason": "error",
                            "errorMessage": "429 This task's request budget is spent",
                        },
                    }
                )
                + "\n"
            )
        return result

    monkeypatch.setattr(runner, "capture", spent)
    assert runner.run(arguments(repo, tmp_path)) == 1
    _, record = only_run(steps)
    assert record["ended"] == "model_error"
    assert "commit" not in steps.calls


# The commit step.

AUTHOR = {"author_name": "Home Lab", "author_email": "lab@example.invalid"}


def committing(repo, tmp_path, **overrides):
    return arguments(repo, tmp_path, allow=["app.py"], **AUTHOR, **overrides)


def test_a_passing_check_is_followed_by_the_commit_and_nothing_else_is(repo, tmp_path, monkeypatch):
    steps = Steps(tmp_path, monkeypatch, repo)
    assert runner.run(committing(repo, tmp_path)) == 0
    folder, record = only_run(steps)
    assert record["ended"] == "committed"
    assert record["commit"] == "c" * 40
    assert list(steps.calls) == ["prepare", "agent", "check", "commit"]
    assert not (folder / "commit.out").exists()
    assert (folder / "commit.log").exists()


def test_without_an_allow_list_a_run_stays_as_it_was(repo, tmp_path, monkeypatch):
    steps = Steps(tmp_path, monkeypatch, repo)
    assert runner.run(arguments(repo, tmp_path)) == 0
    assert only_run(steps)[1]["ended"] == "check_passed"
    assert "commit" not in steps.calls


def test_a_failed_check_never_reaches_the_commit(repo, tmp_path, monkeypatch):
    steps = Steps(tmp_path, monkeypatch, repo)
    steps.check = (1, "exited")
    assert runner.run(committing(repo, tmp_path)) == 1
    assert "commit" not in steps.calls


def test_a_failed_agent_never_reaches_the_commit(repo, tmp_path, monkeypatch):
    steps = Steps(tmp_path, monkeypatch, repo, stops=("error",))
    assert runner.run(committing(repo, tmp_path)) == 1
    assert "commit" not in steps.calls


def test_a_commit_needs_an_author(repo, tmp_path, monkeypatch):
    Steps(tmp_path, monkeypatch, repo)
    with pytest.raises(runner.Refused, match="author"):
        runner.run(arguments(repo, tmp_path, allow=["app.py"]))


def test_the_commit_step_alone_writes_git_with_nothing_from_the_copy(repo, tmp_path, monkeypatch):
    steps = Steps(tmp_path, monkeypatch, repo)
    runner.run(committing(repo, tmp_path))
    command = steps.calls["commit"]["command"]
    assert values(command, "--network") == ["none"]
    mounts = values(command, "--mount")
    assert f"type=bind,src={repo},dst=/work" in mounts
    assert not any(mount.endswith("dst=/work/.git,readonly") for mount in mounts)
    assert f"type=bind,src={repo}/.venv,dst=/work/.venv,readonly" in mounts
    # `.venv/bin` leads the image's PATH, and prepare ran the repository's build.
    # The image's own entrypoint lives outside that PATH, so the step replaces it.
    assert values(command, "--entrypoint") == ["/usr/bin/python3"]
    assert command[command.index(runner.IMAGE) + 1 :][:2] == ["-I", "-c"]
    assert "PATH=/usr/bin:/bin" in values(command, "--env")
    for step in ["prepare", "agent", "check"]:
        assert any(
            mount.endswith("dst=/work/.git,readonly")
            for mount in values(steps.calls[step]["command"], "--mount")
        )
    assert "HOMELAB_TASK_TOKEN" not in " ".join(command)
    assert steps.calls["commit"]["env"]["GIT_AUTHOR_NAME"] == "Home Lab"
    assert steps.grants_during["commit"] == []


@pytest.mark.parametrize(
    "result,output,ended",
    [
        ((3, "exited"), "refused outside_allowed", "commit_refused_outside_allowed"),
        ((3, "exited"), "refused pinned", "commit_refused_pinned"),
        ((3, "exited"), "nonsense", "commit_failed"),
        ((1, "exited"), "", "commit_failed"),
        ((0, "exited"), "commit nothex", "commit_failed"),
        ((None, "timeout"), "", "commit_timeout"),
    ],
)
def test_a_refused_or_failed_commit_fails_the_run(
    repo, tmp_path, monkeypatch, result, output, ended
):
    steps = Steps(tmp_path, monkeypatch, repo)
    steps.commit, steps.commit_output = result, output
    assert runner.run(committing(repo, tmp_path)) == 1
    _, record = only_run(steps)
    assert record["ended"] == ended
    assert record["commit"] is None


def commit_step(repo, allowed=("app.py",), pinned=(), base=None):
    spec = {
        "base": base or git(repo, "rev-parse", "HEAD").strip(),
        "allowed": list(allowed),
        "pinned": list(pinned),
        "message_file": runner.COMMIT_MESSAGE,
    }
    return subprocess.run(
        [sys.executable, "-I", "-c", runner.COMMIT, json.dumps(spec)],
        cwd=repo,
        env={
            "GIT_AUTHOR_NAME": "Home Lab",
            "GIT_AUTHOR_EMAIL": "lab@example.invalid",
            "TMPDIR": str(repo.parent),
        },
        capture_output=True,
        text=True,
    )


def change(repo, path="app.py", content="VALUE = 2\n", message="Make VALUE two\n"):
    target = repo / path
    target.parent.mkdir(parents=True, exist_ok=True)
    target.write_text(content)
    if message is not None:
        (repo / runner.COMMIT_MESSAGE).write_text(message)


def test_the_commit_is_one_commit_of_the_tree_by_the_configured_author(repo):
    base = git(repo, "rev-parse", "HEAD").strip()
    git(repo, "config", "user.name", "From The Repository")
    change(repo)
    result = commit_step(repo)
    assert result.returncode == 0, result.stderr
    commit = result.stdout.split()[1]
    assert git(repo, "rev-parse", "HEAD").strip() == commit
    assert git(repo, "rev-parse", "HEAD^").strip() == base
    assert git(repo, "log", "-1", "--format=%an|%ae|%cn|%ce|%B").strip() == (
        "Home Lab|lab@example.invalid|Home Lab|lab@example.invalid|Make VALUE two"
    )
    assert git(repo, "show", "--name-only", "--format=", "HEAD").split() == ["app.py"]


def test_the_commit_runs_no_hook(repo, tmp_path):
    marker = tmp_path / "hook-ran"
    for name in ["pre-commit", "commit-msg", "post-commit", "post-index-change",
                 "reference-transaction"]:  # fmt: skip
        hook = repo / ".git/hooks" / name
        hook.parent.mkdir(exist_ok=True)
        hook.write_text(f"#!/bin/sh\ntouch {marker}\n")
        hook.chmod(0o755)
    change(repo)
    assert commit_step(repo).returncode == 0
    assert not marker.exists()


def test_a_directory_in_the_allow_list_covers_what_is_below_it(repo):
    change(repo, "src/deep/new.py")
    assert commit_step(repo, allowed=["src"]).returncode == 0


def test_venv_and_the_message_file_are_never_committed(repo):
    git(repo, "rm", "-q", "--cached", ".gitignore")
    (repo / ".gitignore").unlink()
    git(repo, "commit", "-qm", "no ignore file")
    (repo / ".venv/bin").mkdir()
    (repo / ".venv/bin/tool").write_text("x")
    change(repo)
    assert commit_step(repo).returncode == 0
    assert git(repo, "show", "--name-only", "--format=", "HEAD").split() == ["app.py"]


@pytest.mark.parametrize(
    "path,allowed,pinned,reason",
    [
        ("other.py", ["app.py"], [], "outside_allowed"),
        ("app.py.bak", ["app.py"], [], "outside_allowed"),
        ("srcs/x.py", ["src"], [], "outside_allowed"),
        ("tests/test_app.py", ["tests"], ["tests/test_app.py"], "pinned"),
        ("tests/new.py", ["tests"], ["tests"], "pinned"),
        (".github/workflows/ci.yml", ["app.py"], [], "outside_allowed"),
        ("src/.github/workflows/ci.yml", ["src"], [], "guarded"),
        ("src/.gitattributes", ["src"], [], "guarded"),
        ("src/.gitmodules", ["src"], [], "guarded"),
        ("src/uv.lock", ["src"], [], "guarded"),
    ],
)
def test_the_commit_refuses_a_path(repo, path, allowed, pinned, reason):
    base = git(repo, "rev-parse", "HEAD").strip()
    change(repo, path)
    result = commit_step(repo, allowed=allowed, pinned=pinned)
    assert result.returncode == 3
    assert result.stdout.strip() == f"refused {reason}"
    assert git(repo, "rev-parse", "HEAD").strip() == base


@pytest.mark.parametrize(
    "path,allowed",
    [
        (".github/workflows/ci.yml", [".github/workflows/ci.yml"]),
        (".github/workflows/ci.yml", [".github"]),
        (".gitignore", [".gitignore"]),
        ("uv.lock", ["uv.lock"]),
        ("src/uv.lock", ["src/uv.lock"]),
    ],
)
def test_a_guarded_path_passes_only_when_named(repo, path, allowed):
    change(repo, path, "named\n")
    assert commit_step(repo, allowed=allowed).returncode == 0


def test_a_deletion_outside_the_list_is_refused(repo):
    (repo / "tests/test_app.py").unlink()
    (repo / runner.COMMIT_MESSAGE).write_text("Remove a test\n")
    assert commit_step(repo).stdout.strip() == "refused outside_allowed"


def test_an_embedded_repository_is_refused(repo):
    nested = repo / "app"
    nested.mkdir()
    (nested / "x").write_text("x\n")
    git(nested, "init", "-q")
    git(nested, "add", ".")
    git(nested, "commit", "-qm", "nested")
    (repo / runner.COMMIT_MESSAGE).write_text("Add\n")
    assert commit_step(repo, allowed=["app"]).stdout.strip() == "refused submodule"


def test_no_change_is_refused(repo):
    (repo / runner.COMMIT_MESSAGE).write_text("Nothing\n")
    assert commit_step(repo).stdout.strip() == "refused no_change"


@pytest.mark.parametrize("message", [None, "", "  \n\n"])
def test_no_message_is_refused(repo, message):
    change(repo, message=message)
    assert commit_step(repo).stdout.strip() == "refused no_message"


def test_a_linked_message_is_refused(repo, tmp_path):
    change(repo, message=None)
    secret = tmp_path / "outside"
    secret.write_text("outside the tree\n")
    (repo / runner.COMMIT_MESSAGE).symlink_to(secret)
    assert commit_step(repo).stdout.strip() == "refused no_message"


def test_the_service_unit_parses_to_the_full_author_name():
    unit = Path(__file__).parent.parent / "node/etc/systemd/system/homelab-task-runner.service"
    line = next(
        text
        for text in unit.read_text().splitlines()
        if text.startswith("Environment=HOMELAB_TASK_AUTHOR_NAME=")
    )
    assert shlex.split(line.removeprefix("Environment=")) == ["HOMELAB_TASK_AUTHOR_NAME=Home Lab"]


# The review.

PASS = "Looks right.\nVERDICT: PASS"
FIX = "The skip hides the failure.\nVERDICT: FIX"
FILES = [
    "agent.log",
    "check.log",
    "diff.patch",
    "events.jsonl",
    "prepare.log",
    "review.jsonl",
    "review.log",
    "run.json",
]


def reviewing(repo, tmp_path, **overrides):
    fields = {"allow": ["app.py"], "review_purpose": "task-review", **AUTHOR, **overrides}
    return arguments(repo, tmp_path, **fields)


def digest(steps, step):
    return hashlib.sha256(steps.calls[step]["env"]["HOMELAB_TASK_TOKEN"].encode()).hexdigest()


def test_a_pass_goes_on_to_the_commit(repo, tmp_path, monkeypatch):
    steps = Steps(tmp_path, monkeypatch, repo)
    steps.reviews = [PASS]
    assert runner.run(reviewing(repo, tmp_path)) == 0
    folder, record = only_run(steps)
    assert list(steps.calls) == ["prepare", "agent", "check", "review", "commit"]
    assert record["ended"] == "committed" and record["commit"] == "c" * 40
    assert sorted(path.name for path in folder.iterdir()) == sorted([*FILES, "commit.log"])
    (item,) = record["reviews"]
    assert item["result"] == "pass" and item["steps"] == 2
    assert item["tokens"] == {"fresh": 200, "output": 14, "cached": 80, "cache_write": 0}
    assert item["seconds"] >= 0
    # The reviewer's usage is its own: the worker's totals stay the worker's.
    assert record["steps"] == 1 and record["tokens"]["fresh"] == 100
    assert record["review_notes"] is None and record["check_log"] == "check.log"
    assert record["review_purpose"] == "task-review"
    assert steps.calls["review"]["files"] == ("review.jsonl", "review.log")


def test_a_fix_gives_the_notes_to_one_more_round_in_the_same_copy(repo, tmp_path, monkeypatch):
    steps = Steps(tmp_path, monkeypatch, repo)
    steps.reviews = [FIX, PASS]
    assert runner.run(reviewing(repo, tmp_path)) == 0
    folder, record = only_run(steps)
    assert list(steps.calls) == [
        "prepare",
        "agent",
        "check",
        "review",
        "agent2",
        "check2",
        "review2",
        "commit",
    ]
    second = steps.calls["agent2"]["ticket"]
    assert second.startswith(b"Make VALUE two.")
    assert b"> The skip hides the failure." in second and b"VERDICT" not in second
    assert steps.calls["agent2"]["files"] == ("events-2.jsonl", "agent-2.log")
    assert steps.calls["check2"]["files"] == ("diff-2.patch", "check-2.log")
    assert steps.calls["review2"]["files"] == ("review-2.jsonl", "review-2.log")
    # Same working copy, same base commit.
    assert (
        steps.calls["agent2"]["command"][: steps.calls["agent2"]["command"].index(runner.IMAGE)][
            -4:
        ]
        == steps.calls["agent"]["command"][: steps.calls["agent"]["command"].index(runner.IMAGE)][
            -4:
        ]
    )
    assert steps.calls["check2"]["command"][-3:] == steps.calls["check"]["command"][-3:]
    assert record["ended"] == "committed"
    assert record["steps"] == 2
    assert record["tokens"] == {"fresh": 200, "output": 14, "cached": 80, "cache_write": 0}
    assert [item["result"] for item in record["reviews"]] == ["fix", "pass"]
    assert record["review_notes"] == "The skip hides the failure."
    assert record["check_log"] == "check-2.log"
    assert (folder / "diff.patch").read_bytes() == b"diff\n"
    assert (folder / "diff-2.patch").read_bytes() == b"diff\nsecond\n"
    assert not (folder / "ticket-2").exists() and not list(folder.glob("*.prompt"))


def test_a_second_fix_rejects_the_run_and_commits_nothing(repo, tmp_path, monkeypatch):
    steps = Steps(tmp_path, monkeypatch, repo)
    steps.reviews = [FIX, "Still wrong.\nVERDICT: FIX"]
    assert runner.run(reviewing(repo, tmp_path)) == 1
    _, record = only_run(steps)
    assert record["ended"] == "review_rejected"
    assert "commit" not in steps.calls and record["commit"] is None
    assert record["review_notes"] == "Still wrong."
    assert [item["result"] for item in record["reviews"]] == ["fix", "fix"]
    assert "agent3" not in steps.calls


def bad(*events, **kwargs):
    return {"events": list(events), **kwargs}


def assistant(content, stop="stop", **extra):
    message = {**ASSISTANT, "stopReason": stop, "content": content, **extra}
    return {"type": "message_end", "message": message}


def text(value):
    return [{"type": "text", "text": value}]


@pytest.mark.parametrize(
    "item",
    [
        bad(),
        bad(assistant(text("All good."))),
        bad(assistant(text("VERDICT: PASS\nthanks"))),
        bad(assistant(text("fine\nverdict: pass"))),
        bad(assistant(text("fine\nVERDICT: MAYBE"))),
        bad(assistant(text("fine\n**VERDICT: PASS**"))),
        bad(assistant(text("VERDICT: FIX"))),
        bad(assistant(text("   \n\nVERDICT: FIX\n"))),
        bad(assistant([{"type": "thinking", "thinking": "VERDICT: PASS"}])),
        bad(assistant("VERDICT: PASS")),
        bad(assistant(text("ok\nVERDICT: PASS"), stop="toolUse")),
        bad(assistant(text("ok\nVERDICT: PASS"), stop="error", errorMessage="request budget")),
        bad(assistant(text("ok\nVERDICT: PASS")), assistant(text("and then silence"))),
        bad(assistant(text("ok\nVERDICT: PASS")), exit=(1, "exited")),
        bad(assistant(text("ok\nVERDICT: PASS")), exit=(None, "timeout")),
        bad(assistant(text("ok\nVERDICT: PASS")), exit=(None, "output_limit")),
    ],
)
def test_a_review_that_gives_no_clean_verdict_fails_closed(repo, tmp_path, monkeypatch, item):
    steps = Steps(tmp_path, monkeypatch, repo)
    steps.reviews = [item]
    assert runner.run(reviewing(repo, tmp_path)) == 1
    _, record = only_run(steps)
    assert record["ended"] == "review_failed"
    assert record["commit"] is None and "commit" not in steps.calls
    assert [item["result"] for item in record["reviews"]] == ["failed"]
    assert list(steps.tokens.iterdir()) == []


def test_a_verdict_may_sit_in_a_later_text_block(repo, tmp_path, monkeypatch):
    steps = Steps(tmp_path, monkeypatch, repo)
    blocks = [
        {"type": "thinking", "thinking": "hm"},
        {"type": "text", "text": "Fine."},
        {"type": "text", "text": "VERDICT: PASS  "},
    ]
    steps.reviews = [bad(assistant(blocks), {"type": "agent_settled", "aborted": False})]
    assert runner.run(reviewing(repo, tmp_path)) == 0


def test_only_the_last_reply_counts(repo, tmp_path, monkeypatch):
    steps = Steps(tmp_path, monkeypatch, repo)
    steps.reviews = [bad(assistant(text("a\nVERDICT: FIX"), stop="toolUse"), *reply(PASS)[2:])]
    assert runner.run(reviewing(repo, tmp_path)) == 0


def test_notes_are_cut_to_eight_kib(tmp_path):
    notes = "é" * 10000
    events_path = tmp_path / "events"
    events_path.write_text(json.dumps(assistant(text(notes + "\nVERDICT: FIX"))) + "\n")
    verdict, found = runner.read_verdict(events_path)
    assert verdict == "fix"
    assert 8190 <= len(found.encode()) <= runner.REVIEW_NOTES_LIMIT


def test_a_diff_over_the_limit_is_not_reviewed(repo, tmp_path, monkeypatch):
    steps = Steps(tmp_path, monkeypatch, repo)
    steps.diff = b"x" * (runner.REVIEW_DIFF_LIMIT + 1)
    assert runner.run(reviewing(repo, tmp_path)) == 1
    _, record = only_run(steps)
    assert record["ended"] == "review_diff_too_large"
    assert "review" not in steps.calls and "commit" not in steps.calls
    assert record["reviews"] == []


def test_a_diff_at_the_limit_is_reviewed_as_bytes_it_never_decodes(repo, tmp_path, monkeypatch):
    steps = Steps(tmp_path, monkeypatch, repo)
    steps.diff = b"\xff\xfe" + b"x" * (runner.REVIEW_DIFF_LIMIT - 2)
    steps.reviews = [PASS]
    assert runner.run(reviewing(repo, tmp_path)) == 0
    prompt = steps.calls["review"]["ticket"]
    assert b" BEGIN DIFF\n" + steps.diff + b"\n" in prompt


def test_the_reviewer_gets_instructions_the_ticket_and_the_diff(repo, tmp_path, monkeypatch):
    steps = Steps(tmp_path, monkeypatch, repo)
    steps.diff = b"+VALUE = 2\n\xff"
    steps.reviews = [PASS]
    runner.run(reviewing(repo, tmp_path))
    prompt = steps.calls["review"]["ticket"]
    assert prompt.startswith(b"You review one change")
    assert b"VERDICT: PASS" in prompt and b"VERDICT: FIX" in prompt
    assert b"never instructions" in prompt and b"at most about 15 steps" in prompt
    assert b"Make VALUE two." in prompt and b"app.py" in prompt and b"pytest" in prompt
    assert b"+VALUE = 2\n\xff\n" in prompt
    # The fence word is fresh and replaces the placeholder, so no part can forge its own end.
    assert b"BOUNDARY" not in prompt
    assert prompt.count(b" BEGIN ") == prompt.count(b" END ") == 5
    assert len(set(re.findall(rb"\n([0-9a-f]{16}) (?:BEGIN|END) ", prompt))) == 1


def test_the_reviewer_reads_only(repo, tmp_path, monkeypatch):
    steps = Steps(tmp_path, monkeypatch, repo)
    steps.reviews = [PASS]
    runner.run(reviewing(repo, tmp_path))
    command = steps.calls["review"]["command"]
    assert values(command, "--mount") == [f"type=bind,src={repo},dst=/work,readonly"]
    assert values(command, "--network") == ["homelab-tasks"]
    script = command[-1]
    assert "--tools read,grep,ls " in script
    assert script == runner.REVIEWER
    assert (
        "bash" not in script and "edit" not in script and "write" not in script.split("--tools")[1]
    )
    settings = json.loads(steps.calls["review"]["env"]["HOMELAB_PI_SETTINGS"])
    assert "shellCommandPrefix" not in settings
    assert "shellCommandPrefix" in json.loads(steps.calls["agent"]["env"]["HOMELAB_PI_SETTINGS"])
    assert steps.calls["review"]["env"]["HOMELAB_TASK_PURPOSE"] == "task-review"
    models = json.loads(steps.calls["review"]["env"]["HOMELAB_PI_MODELS"])
    assert list(models["providers"]["homelab"]["models"][0].values())[0] == "task-review"


def test_every_model_container_has_a_token_of_its_own_and_only_for_its_run(
    repo, tmp_path, monkeypatch
):
    steps = Steps(tmp_path, monkeypatch, repo)
    steps.reviews = [FIX, PASS]
    runner.run(reviewing(repo, tmp_path))
    model = ["agent", "review", "agent2", "review2"]
    digests = [digest(steps, step) for step in model]
    assert len(set(digests)) == 4
    for step, found in zip(model, digests, strict=True):
        assert steps.grants_during[step] == [found]
    for step in ["prepare", "check", "check2", "commit"]:
        assert steps.grants_during[step] == []
        assert "HOMELAB_TASK_TOKEN" not in steps.calls[step]["env"]
    assert list(steps.tokens.iterdir()) == []


def test_the_reviewer_may_ask_for_twenty_requests_of_its_purpose_only(repo, tmp_path, monkeypatch):
    steps = Steps(tmp_path, monkeypatch, repo)
    steps.reviews = [FIX, PASS]
    runner.run(reviewing(repo, tmp_path, max_requests=10))
    assert runner.REVIEW_MAX_REQUESTS == 20
    assert steps.granted["review"][0]["purposes"] == ["task-review"]
    assert steps.granted["review"][0]["max_requests"] == 20
    assert steps.granted["review2"][0]["max_requests"] == 20
    assert steps.granted["agent"][0]["max_requests"] == 10
    assert steps.granted["agent"][0]["purposes"] == ["task-worker"]
    # The fix round gets what the first run left; the reviewer's requests are not taken from it.
    assert steps.granted["agent2"][0]["max_requests"] == 9
    assert steps.granted["agent2"][0]["purposes"] == ["task-worker"]


def test_a_fix_with_no_budget_left_ends_without_another_agent(repo, tmp_path, monkeypatch):
    steps = Steps(tmp_path, monkeypatch, repo)
    steps.reviews = [FIX]
    assert runner.run(reviewing(repo, tmp_path, max_requests=1)) == 1
    _, record = only_run(steps)
    assert record["ended"] == "agent_budget_spent"
    assert "agent2" not in steps.calls and "commit" not in steps.calls
    assert record["review_notes"] == "The skip hides the failure."


@pytest.mark.parametrize(
    "agent2,stops,check2,ended,log",
    [
        ((0, "exited"), ("error",), (0, "exited"), "model_error", "check-2.log"),
        ((None, "timeout"), ("toolUse",), (0, "exited"), "agent_timeout", "check-2.log"),
        ((0, "exited"), ("stop",), (1, "exited"), "check_failed", "check-2.log"),
        ((0, "exited"), ("stop",), (None, "timeout"), "check_timeout", "check-2.log"),
    ],
)
def test_a_fix_round_that_fails_ends_as_those_steps_do(
    repo, tmp_path, monkeypatch, agent2, stops, check2, ended, log
):
    steps = Steps(tmp_path, monkeypatch, repo)
    steps.reviews = [FIX]
    steps.agent2, steps.check2 = agent2, check2
    original = steps.capture

    def second(command, name, stdout, *args, **kwargs):
        if name.endswith("-agent2"):
            steps.stops = stops
        return original(command, name, stdout, *args, **kwargs)

    monkeypatch.setattr(runner, "capture", second)
    assert runner.run(reviewing(repo, tmp_path)) == 1
    folder, record = only_run(steps)
    assert record["ended"] == ended and record["check_log"] == log
    assert "review2" not in steps.calls and "commit" not in steps.calls
    assert (folder / log).exists()


def test_every_token_a_run_issued_is_scrubbed(repo, tmp_path, monkeypatch):
    steps = Steps(tmp_path, monkeypatch, repo)
    steps.leak = "all"
    steps.reviews = [FIX, PASS]
    runner.run(reviewing(repo, tmp_path))
    folder, _ = only_run(steps)
    tokens = [
        steps.calls[step]["env"]["HOMELAB_TASK_TOKEN"]
        for step in ["agent", "review", "agent2", "review2"]
    ]
    assert len(set(tokens)) == 4
    for path in folder.iterdir():
        for token in tokens:
            assert token.encode() not in path.read_bytes()
    for name in ["events.jsonl", "review.jsonl", "events-2.jsonl", "review-2.jsonl"]:
        assert b"[task-token]" in (folder / name).read_bytes()


def test_a_run_interrupted_in_the_review_revokes_its_token(repo, tmp_path, monkeypatch):
    steps = Steps(tmp_path, monkeypatch, repo)
    steps.reviews = [PASS]
    original = steps.capture

    def interrupted(command, name, *args, **kwargs):
        result = original(command, name, *args, **kwargs)
        if name.endswith("-review"):
            raise SystemExit(143)
        return result

    monkeypatch.setattr(runner, "capture", interrupted)
    with pytest.raises(SystemExit):
        runner.run(reviewing(repo, tmp_path))
    folder, record = only_run(steps)
    assert record["ended"] == "interrupted"
    assert list(steps.tokens.iterdir()) == []
    assert not list(folder.glob("*.prompt"))


def test_without_a_review_purpose_nothing_is_reviewed(repo, tmp_path, monkeypatch):
    steps = Steps(tmp_path, monkeypatch, repo)
    assert runner.run(committing(repo, tmp_path)) == 0
    folder, record = only_run(steps)
    assert list(steps.calls) == ["prepare", "agent", "check", "commit"]
    assert record["reviews"] == [] and record["review_purpose"] is None
    assert not list(folder.glob("review*"))


@pytest.mark.parametrize(
    "overrides",
    [{"allow": []}, {"review_purpose": "chat:high"}, {"review_purpose": "../x"}],
)
def test_a_review_needs_a_commit_to_guard_and_a_valid_purpose(
    repo, tmp_path, monkeypatch, overrides
):
    steps = Steps(tmp_path, monkeypatch, repo)
    with pytest.raises(runner.Refused):
        runner.run(reviewing(repo, tmp_path, **overrides))
    assert steps.calls == {} and not steps.runs.exists()


def test_the_flag_is_parsed():
    found = runner.parse(
        ["--working-copy", "x", "--ticket", "t", "--check", "c", "--review-purpose", "p"]
    )
    assert found.review_purpose == "p"
    assert (
        runner.parse(["--working-copy", "x", "--ticket", "t", "--check", "c"]).review_purpose
        is None
    )


def test_the_docstring_says_what_the_host_does_with_the_diff():
    assert "handled as bytes" in runner.__doc__ and "never parsed" in runner.__doc__


# What the proxy billed.


def line(cost):
    return json.dumps({"cost_usd": cost})


def test_a_run_clears_the_costs_a_dead_run_left(repo, tmp_path, monkeypatch):
    steps = Steps(tmp_path, monkeypatch, repo)
    steps.costs.mkdir()
    (steps.costs / "stale").write_text(line(9) + "\n")
    runner.run(arguments(repo, tmp_path))
    assert list(steps.costs.iterdir()) == []
    assert oct(steps.costs.stat().st_mode & 0o777) == "0o770"


def test_the_clearing_is_the_same_as_for_tokens(tmp_path, monkeypatch):
    folder = tmp_path / "costs"
    folder.mkdir()
    (folder / "stale").write_text("{}")
    owners = []
    monkeypatch.setattr(runner, "COSTS", folder)
    monkeypatch.setattr(runner.os, "chown", lambda *args: owners.append(args))
    runner.clear_costs()
    assert list(folder.iterdir()) == []
    assert owners == [(folder, 0, runner.LITELLM_GID)]


def test_a_container_costs_the_sum_of_its_lines_and_its_file_goes(repo, tmp_path, monkeypatch):
    steps = Steps(tmp_path, monkeypatch, repo)
    steps.billed = {"agent": [line(0.25), line(0.5), line(1)]}
    runner.run(arguments(repo, tmp_path))
    _, record = only_run(steps)
    assert record["cost_usd"] == 1.75
    assert list(steps.costs.iterdir()) == []


@pytest.mark.parametrize(
    "lines",
    [
        None,
        [],
        [line(0.25), line(None)],
        [line(0.25), "not json"],
        [line(-1)],
        ['{"cost": 1}'],
        ['{"cost_usd": "1"}'],
    ],
)
def test_a_missing_or_unknown_cost_is_null_never_a_partial_sum(repo, tmp_path, monkeypatch, lines):
    steps = Steps(tmp_path, monkeypatch, repo)
    if lines is not None:
        steps.billed = {"agent": lines}
    runner.run(arguments(repo, tmp_path))
    _, record = only_run(steps)
    assert record["cost_usd"] is None
    assert list(steps.costs.iterdir()) == []


def test_the_worker_total_covers_every_run_and_one_unknown_makes_it_null(
    repo, tmp_path, monkeypatch
):
    steps = Steps(tmp_path, monkeypatch, repo)
    steps.reviews = [FIX, PASS]
    steps.billed = {"agent": [line(1)], "agent2": [line(2)], "review": [line(9)]}
    assert runner.run(reviewing(repo, tmp_path)) == 0
    _, record = only_run(steps)
    assert record["cost_usd"] == 3
    steps = Steps(tmp_path / "again", monkeypatch, repo)
    steps.reviews = [FIX, PASS]
    steps.billed = {"agent": [line(1)]}
    (tmp_path / "again").mkdir(exist_ok=True)
    runner.run(reviewing(repo, tmp_path))
    assert only_run(steps)[1]["cost_usd"] is None


def test_each_review_carries_its_own_cost(repo, tmp_path, monkeypatch):
    steps = Steps(tmp_path, monkeypatch, repo)
    steps.reviews = [FIX, PASS]
    steps.billed = {"agent": [line(1)], "review": [line(0.5)], "agent2": [line(1)]}
    runner.run(reviewing(repo, tmp_path))
    _, record = only_run(steps)
    assert [item["cost_usd"] for item in record["reviews"]] == [0.5, None]
    assert record["cost_usd"] == 2


def test_an_interrupted_run_reports_no_partial_cost(repo, tmp_path, monkeypatch):
    steps = Steps(tmp_path, monkeypatch, repo)
    steps.billed = {"agent": [line(1)]}
    original = steps.capture

    def interrupted(command, name, *args, **kwargs):
        if name.endswith("-agent"):
            original(command, name, *args, **kwargs)
            raise KeyboardInterrupt
        return original(command, name, *args, **kwargs)

    monkeypatch.setattr(runner, "capture", interrupted)
    with pytest.raises(KeyboardInterrupt):
        runner.run(arguments(repo, tmp_path))
    assert only_run(steps)[1]["cost_usd"] == 1


@pytest.fixture
def fake_factory(tmp_path, monkeypatch):
    """A real executable at the boundary, with containers elsewhere still scripted by Steps."""
    work, runs, binary = (tmp_path / name for name in ("cli-work", "cli-runs", "cli-bin"))
    for folder in (work, runs, binary):
        folder.mkdir()
    monkeypatch.setattr(runner, "CLI_WORK", work)
    monkeypatch.setattr(runner, "CLI_RUNS", runs)
    monkeypatch.setattr(runner.os, "fchown", lambda *args: None)
    monkeypatch.setenv("PATH", f"{binary}:{os.environ['PATH']}")
    seen = tmp_path / "cli-seen.jsonl"

    def install(body):
        command = binary / "homelab-fence"
        command.write_text(
            f"#!{sys.executable}\n"
            "import json, os, sys, time\nfrom pathlib import Path\n"
            "argv = sys.argv[1:]\n"
            "repo = Path(argv[argv.index('--repo') + 1])\n"
            "run = Path(argv[argv.index('--run') + 1])\n"
            "assert repo.stat().st_mode & 0o777 == 0o700\n"
            "assert run.stat().st_mode & 0o777 == 0o700\n"
            "assert not any(p.name == '.git' for p in repo.rglob('*'))\n"
            f"with open({str(seen)!r}, 'a') as stream:\n"
            "    stream.write(json.dumps({'argv': argv, 'env': dict(os.environ)}) + '\\n')\n" + body
        )
        command.chmod(0o755)
        return seen

    return install


def cli_arguments(repo, tmp_path, **overrides):
    return reviewing(
        repo,
        tmp_path,
        cli_model="example-model",
        cli_effort="high",
        cli_name="ticket-7-1",
        **overrides,
    )


def test_cli_turn_reaches_the_existing_check_review_and_commit(
    repo, tmp_path, monkeypatch, fake_factory
):
    steps = Steps(tmp_path, monkeypatch, repo)
    steps.reviews = [PASS]
    seen = fake_factory(
        "(repo / 'app.py').write_text('VALUE = 2\\n')\n"
        "(repo / '.task-commit-message').write_text('Change value\\n')\n"
    )
    assert runner.run(cli_arguments(repo, tmp_path)) == 0
    _, record = only_run(steps)
    assert list(steps.calls) == ["prepare", "check", "review", "commit"]
    assert (repo / "app.py").read_text() == "VALUE = 2\n"
    assert (repo / runner.COMMIT_MESSAGE).read_text() == "Change value\n"
    assert record["cli_ignored_changes"] == 0
    assert record["ended"] == "committed"
    assert record["steps"] == 0 and not any(record["tokens"].values())
    assert "cost_usd" not in record
    assert record["reviews"][0]["tokens"]["fresh"] == 200
    invocation = json.loads(seen.read_text())
    argv = invocation["argv"]
    assert argv[:4] == ["run", "--tool", "codex", "--repo"]
    assert argv[8:] == [
        "codex",
        "exec",
        "-m",
        "example-model",
        "-c",
        'model_reasoning_effort="high"',
        "--skip-git-repo-check",
        "--",
        "Make VALUE two.\n",
    ]
    assert set(invocation["env"]) <= {"PATH", "LC_CTYPE"}
    assert not list(runner.CLI_WORK.iterdir()) and not list(runner.CLI_RUNS.iterdir())


def test_cli_import_ignores_unallowed_pinned_links_and_escape_paths(
    repo, tmp_path, monkeypatch, fake_factory, caplog
):
    steps = Steps(tmp_path, monkeypatch, repo)
    steps.reviews = [PASS]
    outside = tmp_path / "outside"
    outside.mkdir()
    (outside / "secret").write_text("unchanged")
    (repo / "initial-link").symlink_to(outside, target_is_directory=True)
    (repo / "nested").mkdir()
    (repo / "nested/.git").mkdir()
    (repo / "nested/.git/config").write_text("private git data")
    fake_factory(
        "assert not (repo / 'initial-link').exists()\n"
        "(repo / 'app.py').unlink()\n"
        f"(repo / 'app.py').symlink_to({str(outside / 'secret')!r})\n"
        "(repo / 'tests/test_app.py').write_text('weakened')\n"
        "(repo / 'extra.py').write_text('not allowed')\n"
        "(repo / '.git').mkdir()\n"
        "(repo / '.git/config').write_text('hostile')\n"
        f"(repo / 'escape').symlink_to({str(outside)!r}, target_is_directory=True)\n"
        "(repo / 'new.py').write_text('allowed')\n"
    )
    git_config = (repo / ".git/config").read_bytes()
    args = cli_arguments(repo, tmp_path, allow=["app.py", "tests", "escape", "new.py", "nested"])
    assert runner.run(args) == 0
    assert (repo / "app.py").read_text() == "VALUE = 1\n"
    assert (repo / "tests/test_app.py").read_text() == "expected = 2\n"
    assert not (repo / "extra.py").exists() and not (repo / "escape").exists()
    assert (repo / "new.py").is_file() and not (repo / "new.py").is_symlink()
    assert (outside / "secret").read_text() == "unchanged"
    assert (repo / ".git/config").read_bytes() == git_config
    assert "ignored_changes=5" in caplog.text
    assert "hostile" not in caplog.text and "secret" not in caplog.text


def test_cli_import_refuses_a_linked_destination_parent(repo, tmp_path, monkeypatch, fake_factory):
    steps = Steps(tmp_path, monkeypatch, repo)
    steps.reviews = [PASS]
    outside = tmp_path / "outside"
    outside.mkdir()
    (repo / "output").symlink_to(outside, target_is_directory=True)
    fake_factory("(repo / 'output').mkdir()\n(repo / 'output/new').write_text('escape')\n")
    assert runner.run(cli_arguments(repo, tmp_path, allow=["output"])) == 0
    assert not (outside / "new").exists()


@pytest.mark.parametrize(
    "body,reason",
    [
        ("sys.exit(9)\n", "cli_failed"),
        ("time.sleep(60)\n", "cli_timeout"),
    ],
)
def test_cli_failures_end_as_named_without_a_check(
    repo, tmp_path, monkeypatch, fake_factory, body, reason
):
    steps = Steps(tmp_path, monkeypatch, repo)
    fake_factory(body)
    monkeypatch.setattr(runner, "CLI_SECONDS", 1)
    assert runner.run(cli_arguments(repo, tmp_path)) == 1
    _, record = only_run(steps)
    assert record["ended"] == reason and list(steps.calls) == ["prepare"]
    assert not any(record["tokens"].values()) and "cost_usd" not in record
    assert not list(runner.CLI_WORK.iterdir()) and not list(runner.CLI_RUNS.iterdir())


def test_cli_fix_turn_gets_review_notes_and_keeps_review_on_litellm(
    repo, tmp_path, monkeypatch, fake_factory
):
    steps = Steps(tmp_path, monkeypatch, repo)
    steps.reviews = [FIX, PASS]
    seen = fake_factory("(repo / 'app.py').write_text('VALUE = 2\\n')\n")
    assert runner.run(cli_arguments(repo, tmp_path)) == 0
    prompts = [json.loads(line)["argv"][-1] for line in seen.read_text().splitlines()]
    assert prompts[0] == "Make VALUE two.\n"
    assert "Review notes" in prompts[1] and "> The skip hides the failure." in prompts[1]
    assert list(steps.calls) == ["prepare", "check", "review", "check2", "review2", "commit"]
    assert steps.calls["review"]["env"]["HOMELAB_TASK_PURPOSE"] == "task-review"
    _, record = only_run(steps)
    assert len(record["reviews"]) == 2 and not any(record["tokens"].values())


def test_cli_queue_attempt_reaches_push_with_unmeasured_worker_usage(
    repo, tmp_path, monkeypatch, fake_factory, caplog
):
    import contextlib
    import io

    from tests.test_task_runner import CODEX, REVIEWER, task
    from tests.test_task_runner import runner as service

    steps = Steps(tmp_path, monkeypatch, repo)
    steps.reviews = [PASS]
    seen = fake_factory(
        "(repo / 'app.py').write_text('VALUE = 2\\n')\n"
        "(repo / '.task-commit-message').write_text('Change value\\n')\n"
        "(repo / 'ignored').write_text('do not log content')\n"
    )

    def capture(*args, **kwargs):
        result = steps.capture(*args, **kwargs)
        if args[1].endswith("-commit"):
            committed = commit_step(repo, allowed=("app.py",), base=steps.base)
            args[2].write_text(committed.stdout)
            args[3].write_text(committed.stderr)
            return committed.returncode, "exited"
        return result

    monkeypatch.setattr(runner, "capture", capture)
    original_run = subprocess.run

    def invoke(command, **kwargs):
        if command[0] != service.RUN:
            return original_run(command, **kwargs)
        output = io.StringIO()
        with contextlib.redirect_stdout(output):
            status = runner.run(runner.parse(command[1:]))
        return subprocess.CompletedProcess(command, status, output.getvalue().encode())

    events, pushes = [], []
    monkeypatch.setattr(service.subprocess, "run", invoke)
    monkeypatch.setattr(
        service,
        "configuration_snapshot",
        lambda folder: (ROOT / "configurations/pi/base", "a" * 40),
    )
    monkeypatch.setattr(service, "record", events.append)
    monkeypatch.setattr(service, "fresh_copy", lambda item: repo)
    monkeypatch.setattr(service, "remove_copy", lambda item: None)
    monkeypatch.setattr(service, "refuse_branch", lambda item: None)
    monkeypatch.setattr(service, "push", lambda *args: pushes.append(args) or "pushed")
    monkeypatch.setenv("HOMELAB_TASK_AUTHOR_NAME", "Fixture")
    monkeypatch.setenv("HOMELAB_TASK_AUTHOR_EMAIL", "fixture@example.invalid")
    caplog.set_level("INFO", logger="homelab-task-runner")
    item = task(routes=[CODEX], allowed_files=["app.py"])
    service.run_task(item)
    assert list(steps.calls) == ["prepare", "check", "review", "commit"]
    assert len(pushes) == 1 and pushes[0][:2] == (item, repo)
    assert git(repo, "show", f"{pushes[0][2]}:app.py") == "VALUE = 2\n"
    assert git(repo, "log", "-1", "--format=%s").strip() == "Change value"
    assert (repo / ".task-commit-message").read_text() == "Change value\n"
    assert events[-1]["status"] == "done"
    end = next(event for event in events if event["event"] == "end_attempt")
    assert end["route"] == CODEX and end["review_route"] == REVIEWER
    assert end["fresh_tokens"] == end["cached_tokens"] == end["output_tokens"] == 0
    assert "cost_usd" not in end
    assert "ignored_changes=1" in caplog.text and "do not log content" not in caplog.text
    assert json.loads(seen.read_text())["argv"][-1] == service.ticket(item, None)


def test_cli_message_link_is_not_followed(repo, tmp_path, monkeypatch, fake_factory, caplog):
    steps = Steps(tmp_path, monkeypatch, repo)
    steps.reviews = [PASS]
    outside = tmp_path / "message"
    outside.write_text("Do not import this message\n")
    fake_factory(f"(repo / {runner.COMMIT_MESSAGE!r}).symlink_to({str(outside)!r})\n")
    assert runner.run(cli_arguments(repo, tmp_path)) == 0
    assert not (repo / runner.COMMIT_MESSAGE).exists()
    assert not (repo / runner.COMMIT_MESSAGE).is_symlink()
    assert outside.read_text() == "Do not import this message\n"
    assert only_run(steps)[1]["cli_ignored_changes"] == 1
    assert "Do not import this message" not in caplog.text


def test_cli_input_directory_owners_are_the_factory_user(repo, tmp_path, monkeypatch, fake_factory):
    steps = Steps(tmp_path, monkeypatch, repo)
    steps.reviews = [PASS]
    owners = []
    monkeypatch.setattr(runner.os, "chown", lambda *args, **kwargs: owners.append(args))
    fake_factory("")
    assert runner.run(cli_arguments(repo, tmp_path)) == 0
    assert (runner.CLI_WORK / "ticket-7-1", 21000, 21000) in owners
    assert (runner.CLI_RUNS / "ticket-7-1", 21000, 21000) in owners
    assert runner.CLI_SECONDS == 1200


def test_base_reproduces_original_pi_inputs():
    config, _, _ = runner.load_configuration(ROOT / "configurations/pi/base")
    assert runner.pi_script(config) == runner.AGENT
    expected_models = {
        "providers": {
            "homelab": {
                "baseUrl": "http://litellm:4000/v1",
                "api": "openai-completions",
                "apiKey": "$HOMELAB_TASK_TOKEN",
                "compat": {
                    "supportsStore": False,
                    "supportsDeveloperRole": False,
                    "supportsReasoningEffort": False,
                    "maxTokensField": "max_tokens",
                },
                "models": [
                    {
                        "id": "task-worker",
                        "name": "task-worker",
                        "reasoning": False,
                        "input": ["text"],
                        "contextWindow": 128000,
                        "maxTokens": 16384,
                        "cost": {"input": 0, "output": 0, "cacheRead": 0, "cacheWrite": 0},
                    }
                ],
            }
        }
    }
    assert runner.pi_models(
        config["purpose"], config["context_window"], config["max_tokens"]
    ) == json.dumps(expected_models)
    assert (
        runner.pi_settings()
        == '{"enableInstallTelemetry": false, "shellCommandPrefix": "unset HOMELAB_TASK_TOKEN"}'
    )


def test_configuration_hash_covers_every_file(tmp_path):
    import shutil

    folder = tmp_path / "base"
    shutil.copytree(ROOT / "configurations/pi/base", folder)
    _, files, before = runner.load_configuration(folder)
    assert files["notes.md"] == hashlib.sha256((folder / "notes.md").read_bytes()).hexdigest()
    (folder / "notes.md").write_text("changed\n")
    assert runner.load_configuration(folder)[2] != before


def test_invalid_configuration_is_refused_before_image_or_container(tmp_path, monkeypatch):
    folder = tmp_path / "bad"
    folder.mkdir()
    (folder / "configuration.json").write_text("{}")
    monkeypatch.setattr(runner, "image_digest", lambda image: pytest.fail("inspected image"))
    with pytest.raises(runner.Refused, match="invalid configuration"):
        runner.run(argparse.Namespace(configuration=folder))


def test_configuration_controls_model_limits_and_records_provenance(repo, tmp_path, monkeypatch):
    import shutil

    folder = tmp_path / "configuration"
    shutil.copytree(ROOT / "configurations/pi/base", folder)
    data = json.loads((folder / "configuration.json").read_text())
    data["context_window"], data["max_tokens"] = 64000, 4096
    (folder / "configuration.json").write_text(json.dumps(data))
    steps = Steps(tmp_path, monkeypatch, repo)
    assert (
        runner.run(arguments(repo, tmp_path, configuration=folder, configuration_commit="b" * 40))
        == 0
    )
    _, record = only_run(steps)
    model = json.loads(steps.calls["agent"]["env"]["HOMELAB_PI_MODELS"])["providers"]["homelab"][
        "models"
    ][0]
    assert (model["contextWindow"], model["maxTokens"]) == (64000, 4096)
    assert record["configuration_hash"] == runner.load_configuration(folder)[2]
    assert record["configuration_commit"] == "b" * 40
    assert record["configuration_manifest"]["image_digest"] == "sha256:" + "a" * 64


def test_configuration_refuses_links_and_unsafe_flags(tmp_path):
    import shutil

    folder = tmp_path / "config"
    shutil.copytree(ROOT / "configurations/pi/base", folder)
    (folder / "notes.md").unlink()
    (folder / "notes.md").symlink_to(ROOT / "README.md")
    with pytest.raises(runner.Refused):
        runner.load_configuration(folder)
    (folder / "notes.md").unlink()
    path = folder / "configuration.json"
    data = json.loads(path.read_text())
    data["flags"].append("--extension")
    path.write_text(json.dumps(data))
    with pytest.raises(runner.Refused):
        runner.load_configuration(folder)


@pytest.mark.parametrize(
    "name,compactions,retries,last_stop,total",
    [
        ("clean", 0, 0, "stop", 1),
        ("model-error", 0, 0, "error", 2),
        ("compaction", 1, 0, "stop", 4),
        ("retry", 0, 1, "stop", 6),
    ],
)
def test_recorded_event_streams(name, compactions, retries, last_stop, total):
    summary = runner.summarize_events(ROOT / "tests/fixtures/task-events" / f"{name}.jsonl")
    assert summary["compactions"] == compactions
    assert summary["retries"] == retries
    assert summary["last_stop"] == last_stop
    assert summary["end_event"] == {"type": "agent_settled", "aborted": False}
    assert summary["tokens"] == dict.fromkeys(("fresh", "output", "cached", "cache_write"), total)


def test_end_event_is_invalidated_by_continuation(tmp_path):
    path = tmp_path / "events"
    path.write_text(
        "\n".join(
            json.dumps(e)
            for e in [
                {"type": "agent_settled", "aborted": False},
                {"type": "summarization_retry_scheduled"},
                {"type": "summarization_retry_attempt_start"},
            ]
        )
    )
    summary = runner.summarize_events(path)
    assert summary["end_event"] is None
    assert summary["retries"] == 1


def test_compaction_result_usage_is_added_to_assistant_usage():
    summary = runner.summarize_events(ROOT / "tests/fixtures/task-events/compaction-usage.jsonl")
    assert summary["compactions"] == 1
    assert summary["tokens"] == {"fresh": 14, "output": 24, "cached": 34, "cache_write": 44}
