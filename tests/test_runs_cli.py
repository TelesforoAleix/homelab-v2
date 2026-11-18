"""The CLI bridge transfers only regular allowed files, without mounted orchestrator data."""

import base64
import json
import os
import subprocess
import sys
from pathlib import Path
from types import SimpleNamespace

import pytest

from homelab.runs import cli, launcher, slices
from homelab.tasks.executors import Executor


@pytest.mark.parametrize("harness", ["codex", "claude"])
@pytest.mark.parametrize("large", [False, True])
def test_cli_data_bridge_and_existing_profile(tmp_path, monkeypatch, harness, large):
    task = tmp_path / "task"
    plain = tmp_path / "plain"
    task.mkdir()
    (task / ".git").mkdir()
    (task / ".git/config").write_text("metadata")
    (task / ".venv").mkdir()
    (task / "src").mkdir()
    (task / "src/app.py").write_text("before")
    (task / "tests").mkdir()
    (task / "tests/check.py").write_text("pinned")
    (task / "extra.txt").write_text("extra")
    (task / "outside-link").symlink_to(tmp_path / "outside")
    outside = tmp_path / "outside"
    outside.write_text("outside")
    folder = tmp_path / "records"
    folder.mkdir()
    ticket = tmp_path / "ticket"
    ticket.write_text("A CLI ticket" if not large else "🌍" * 200_000 + "\n" + "Asset" * 40_000)
    context = SimpleNamespace(folder=folder, owner=(task, os.getuid(), os.getgid()), seconds=3)
    requests = []
    operations = []

    def request(value):
        operations.append(value["op"])
        if value["op"] == "cli-create":
            plain.mkdir()
        return {"ok": True}

    def capture(value, stdout, stderr, seconds, *, stdin=None, **kwargs):
        requests.append(value)
        Path(stderr).write_bytes(b"")
        if value["profile"] == "cli-run":
            assert value["tool"] == harness and value["exports"] == []
            assert value["command"] == slices.command(args.executor, "", stdin=True, ticket=True)
            assert value["stdin"] is True
            payload = json.loads(stdin.read())
            assert payload["work"]["ticket"] == ticket.read_text()
            from tests.test_launcher import launcher as installed_launcher

            assert (
                ticket.read_text()
                in json.loads(
                    installed_launcher.cli_input(stdin.fileno())
                    .decode()
                    .split("Run work policy (JSON):\n", 1)[1]
                )["ticket"]
            )
            assert not (plain / ".git").exists() and not (plain / ".venv").exists()
            assert not (plain / "outside-link").exists()
            (plain / "src/app.py").write_text("changed")
            (plain / "tests/check.py").write_text("tampered")
            (plain / "extra.txt").unlink()
            (plain / "ignored.txt").write_text("ignored")
            (plain / ".task-commit-message").write_text("Change application")
            (plain / "src/link.py").symlink_to(outside)
            Path(stdout).write_bytes(b"PRIVATE CLI OUTPUT")
            return 0, "exited"
        script, spec = value["arguments"]
        work = task if value["workspace"] == "task" else plain
        script = script.replace("os.open('/work',", "os.open(" + repr(str(work)) + ",")
        result = subprocess.run(
            [sys.executable, "-I", "-c", script, spec],
            input=stdin.read(),
            capture_output=True,
            timeout=3,
        )
        Path(stdout).write_bytes(result.stdout)
        Path(stderr).write_bytes(result.stderr)
        assert result.returncode == 0, result.stderr
        return 0, "exited"

    monkeypatch.setattr(launcher, "request", request)
    monkeypatch.setattr(launcher, "capture", capture)
    args = SimpleNamespace(
        cli_name="ticket-7-1",
        executor=Executor(harness=harness, model="fixture-model", effort="high"),
        allow=["src"],
    )
    record = {}
    assert cli.worker(args, context, ["tests"], ticket, record) is None
    assert (task / "src/app.py").read_text() == "changed"
    assert (task / "tests/check.py").read_text() == "pinned"
    assert (task / "extra.txt").read_text() == "extra"
    assert not (task / "ignored.txt").exists()
    assert not (task / "src/link.py").exists()
    assert (task / ".git/config").read_text() == "metadata"
    assert outside.read_text() == "outside"
    assert (task / ".task-commit-message").read_text() == "Change application"
    assert record["cli_ignored_changes"] == 4  # pin, deletion, new file, link
    assert operations == ["cli-create", "cli-remove"]
    assert [r["profile"] for r in requests] == ["task", "task", "cli-run", "task", "task"]
    assert not list(folder.iterdir())


def test_import_refuses_linked_destination_and_traversal(tmp_path):
    task = tmp_path / "task"
    task.mkdir()
    outside = tmp_path / "outside"
    outside.mkdir()
    (task / "src").symlink_to(outside, target_is_directory=True)
    rows = [
        [path, base64.b64encode(b"changed").decode(), False]
        for path in ("src/file", "../outside/file", "/absolute", ".git/config", "ok/.venv/file")
    ]
    script = cli.DATA.replace("os.open('/work',", "os.open(" + repr(str(task)) + ",")
    result = subprocess.run(
        [sys.executable, "-I", "-c", script, json.dumps({"op": "import", "allowed": None})],
        input="\n".join(json.dumps(row) for row in rows).encode(),
        capture_output=True,
        timeout=3,
    )
    assert result.returncode == 0, result.stderr
    assert json.loads(result.stdout) == []
    assert not list(outside.iterdir())
