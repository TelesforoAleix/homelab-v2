"""Container runner boundaries: fake GitHub, real signatures, and queue exclusion."""

import base64
import json
import subprocess
from contextlib import contextmanager
from pathlib import Path
from types import SimpleNamespace

import httpx
import pytest

from homelab.runs import app, delivery, locking, task_run


def decode(value):
    return base64.urlsafe_b64decode(value + "=" * (-len(value) % 4))


@pytest.fixture
def app_files(tmp_path):
    key = tmp_path / "app.pem"
    subprocess.run(
        ["openssl", "genrsa", "-out", str(key), "2048"],
        check=True,
        stdout=subprocess.DEVNULL,
        stderr=subprocess.DEVNULL,
    )
    config = tmp_path / "app.json"
    config.write_text(json.dumps({"app_id": 123, "installation_id": 456}))
    return key, config


def test_repository_scoped_installation_token_with_real_rs256(app_files, tmp_path):
    key, config = app_files
    calls = []

    def github(request):
        calls.append(request)
        assert request.url.path == "/app/installations/456/access_tokens"
        assert request.method == "POST"
        assert json.loads(request.content) == {
            "repositories": ["repo"],
            "permissions": {"contents": "write", "metadata": "read"},
        }
        jwt = request.headers["authorization"].removeprefix("Bearer ")
        header, payload, signature = jwt.split(".")
        assert json.loads(decode(header)) == {"alg": "RS256", "typ": "JWT"}
        assert json.loads(decode(payload)) == {"iat": 940, "exp": 1540, "iss": "123"}
        public = tmp_path / "public.pem"
        subprocess.run(
            ["openssl", "rsa", "-in", str(key), "-pubout", "-out", str(public)],
            check=True,
            capture_output=True,
        )
        signed = tmp_path / "signature"
        signed.write_bytes(decode(signature))
        subprocess.run(
            ["openssl", "dgst", "-sha256", "-verify", str(public), "-signature", str(signed)],
            input=f"{header}.{payload}".encode(),
            check=True,
            capture_output=True,
        )
        return httpx.Response(201, json={"token": "FAKE-IN-MEMORY-TOKEN"})

    with httpx.Client(transport=httpx.MockTransport(github)) as client:
        auth = app.GitHubApp(key, config, client=client, clock=lambda: 1000)
        assert auth.token("owner/repo") == "FAKE-IN-MEMORY-TOKEN"
        assert auth.token("owner/repo") == "FAKE-IN-MEMORY-TOKEN"
    assert len(calls) == 2  # Mint afresh for every push.
    assert sorted(p.name for p in tmp_path.iterdir()) == [
        "app.json",
        "app.pem",
        "public.pem",
        "signature",
    ]


@pytest.mark.parametrize("repository", ["../repo", "owner/repo/extra", "owner/re..po", "-x/repo"])
def test_token_refuses_invalid_repository(app_files, repository):
    with pytest.raises(app.Refused):
        app.GitHubApp(*app_files).token(repository)


def test_github_failure_does_not_expose_token_or_response(app_files):
    with httpx.Client(
        transport=httpx.MockTransport(lambda _: httpx.Response(403, text="PRIVATE RESPONSE"))
    ) as client:
        with pytest.raises(app.Refused, match="^app_token_failed$"):
            app.GitHubApp(*app_files, client=client).token("owner/repo")


def test_push_ignores_copy_config_and_keeps_token_out_of_command_and_disk(tmp_path, monkeypatch):
    from homelab.runs import runner

    monkeypatch.setattr(
        delivery, "git", lambda *a: SimpleNamespace(stdout=("a" * 40 + "\n").encode())
    )

    calls = []
    monkeypatch.setattr(runner, "refuse_branch", lambda task: None)
    monkeypatch.setattr(runner, "private_content_check", lambda *args, **kwargs: None)
    monkeypatch.setattr(runner, "installation_token", lambda repo: "FAKE-PUSH-TOKEN")
    monkeypatch.setattr(
        runner, "sealed_git_dir", lambda copy: contextmanager(lambda: iter([tmp_path]))()
    )
    monkeypatch.setattr(
        subprocess,
        "run",
        lambda command, **kwargs: (
            calls.append((command, kwargs)) or SimpleNamespace(returncode=0, stdout="")
        ),
    )
    assert runner.push(
        {"repository": "owner/repo", "branch": "slice/test"}, tmp_path, "a" * 40
    ) == ("pushed", ["a" * 40])
    command, kwargs = calls[-1]
    assert command[-2:] == [
        "https://github.com/owner/repo.git",
        "a" * 40 + ":refs/heads/slice/test",
    ]
    assert "core.hooksPath=/dev/null" in command
    assert "credential.helper=" in command
    assert "FAKE-PUSH-TOKEN" not in repr(command)
    assert kwargs["env"]["GIT_CONFIG_VALUE_0"].startswith("Authorization: Basic ")
    assert kwargs["cwd"] == "/"
    assert not list(tmp_path.iterdir())


class Database:
    def __init__(self, granted=True):
        self.granted, self.queries, self.closed = granted, [], False

    def execute(self, query, args=None):
        self.queries.append(query)
        return SimpleNamespace(fetchone=lambda: (self.granted,))

    def close(self):
        self.closed = True


def test_lock_refuses_old_runner_before_any_database_or_claim(tmp_path):
    service = tmp_path / "homelab-task-runner.service"
    service.mkdir()
    (service / "cgroup.events").write_text("populated 1\nfrozen 0\n")
    connection = Database()
    with pytest.raises(locking.Refused, match="legacy_runner_active"):
        with locking.runner_lock(connection, tmp_path):
            pytest.fail("must not claim")
    assert not connection.queries


def test_lock_refuses_a_second_runs(tmp_path):
    with pytest.raises(locking.Refused, match="runner_active"):
        with locking.runner_lock(Database(False), tmp_path):
            pytest.fail("must not claim")


def test_lock_is_held_for_whole_runner_and_rechecks_legacy(tmp_path):
    connection = Database()
    with locking.runner_lock(connection, tmp_path):
        assert not any("unlock" in query for query in connection.queries)
    assert any("unlock" in query for query in connection.queries)


def test_lock_fails_closed_without_host_state(tmp_path):
    with pytest.raises(locking.Refused, match="legacy_state_unavailable"):
        with locking.runner_lock(Database(), tmp_path / "missing"):
            pytest.fail("must not claim")


def test_embedded_commit_and_other_commands_retain_host_behavior():
    from tests.test_task_run import runner as old

    for name in ("PREPARE", "CHECK", "COMMIT", "AGENT"):
        assert getattr(task_run, name) == getattr(old, name)


@pytest.mark.parametrize("harness", [None, "codex", "claude"])
def test_complete_attempt_uses_launcher_without_review(tmp_path, monkeypatch, harness):
    import hashlib

    from homelab.runs import launcher, slices
    from homelab.tasks.executors import Executor
    from tests.test_task_run import ROOT

    work = tmp_path / "copy"
    (work / ".git").mkdir(parents=True)
    (work / ".venv").mkdir()
    monkeypatch.setattr(task_run, "VOLUME", tmp_path)
    monkeypatch.setattr(task_run, "RUNS", tmp_path / "runs")
    monkeypatch.setattr(task_run, "TOKENS", tmp_path / "tokens")
    monkeypatch.setattr(task_run, "COSTS", tmp_path / "costs")
    monkeypatch.setattr(task_run.os, "chown", lambda *args: None)
    monkeypatch.setattr(task_run, "image_digest", lambda _: "sha256:" + "a" * 64)
    requests = []

    def capture(request, out, err, seconds, *, stdin=None, **kwargs):
        requests.append(request)
        shape = request.get("shape")
        Path(err).write_bytes(b"")
        if shape == "prepare":
            Path(out).write_text("a" * 40 + "\n")
        elif shape == "agent":
            assert stdin.read()
            token = request["env"]["HOMELAB_TASK_TOKEN"]
            cost = task_run.COSTS / hashlib.sha256(token.encode()).hexdigest()
            cost.write_text('{"cost_usd":0.1}\n')
            events = [
                {
                    "type": "message_end",
                    "message": {
                        "role": "assistant",
                        "stopReason": "stop",
                        "content": [{"type": "text", "text": "Done"}],
                        "usage": {"input": 10, "output": 2, "cacheRead": 3},
                    },
                },
                {"type": "turn_end"},
                {"type": "agent_settled", "aborted": False},
            ]
            Path(out).write_text("\n".join(json.dumps(e) for e in events) + "\n")
        elif shape == "data":
            _, spec = request["arguments"]
            Path(out).write_text("[]" if json.loads(spec)["op"] == "import" else "")
        elif request["profile"] == "cli-run":
            selected = Executor(harness=harness, model="fixture-model", effort="high")
            assert request["tool"] == harness
            assert request["command"] == slices.command(selected, "", stdin=True, ticket=True)
            assert json.loads(stdin.read())["work"]["ticket"] == "A ticket"
            Path(out).write_text("{}")
        elif shape == "check":
            Path(out).write_bytes(b"DIFF\x00bytes")
            Path(err).write_text(task_run.CHECK_STARTED + "\n")
        elif shape == "commit":
            Path(out).write_text("commit " + "b" * 40 + "\n")
        return 0, "exited"

    monkeypatch.setattr(launcher, "capture", capture)
    monkeypatch.setattr(launcher, "request", lambda _: {"ok": True})
    ticket = tmp_path / "ticket"
    ticket.write_text("A ticket")
    args = task_run.parse(
        [
            "--working-copy",
            str(work),
            "--ticket",
            str(ticket),
            "--check",
            "true",
            "--allow",
            "src",
            "--author-name",
            "Fixture",
            "--author-email",
            "fixture@example.invalid",
            "--configuration",
            str(ROOT / "configurations/pi/base"),
        ]
    )
    if harness:
        args.executor = Executor(harness=harness, model="fixture-model", effort="high")
        args.cli_name = "ticket-7-1"
    record = task_run.run(args)
    assert record["ended"] == "committed"
    expected = (
        ["prepare", "data", "data", "cli-run", "data", "data", "check", "commit"]
        if harness
        else ["prepare", "agent", "check", "commit"]
    )
    assert [r.get("shape", r["profile"]) for r in requests] == expected
    if harness:
        assert record["executor"] == args.executor.selection()
        assert record["configuration_hash"] == args.executor.fingerprint()
        assert "cost_usd" not in record
    else:
        assert record["cost_usd"] == 0.1 and record["steps"] == 1
    assert "reviews" not in record and "review_purpose" not in record
    assert not list(task_run.TOKENS.iterdir())


def test_healthcheck_answers_and_fails_on_a_dead_socket(tmp_path, monkeypatch):
    import socket
    from concurrent.futures import ThreadPoolExecutor

    from homelab.runs import launcher

    address = str(tmp_path / "launcher.sock")
    monkeypatch.setattr(launcher, "SOCKET", address)
    with socket.socket(socket.AF_UNIX) as server, ThreadPoolExecutor() as pool:
        server.bind(address)
        server.listen()

        def answer():
            connection, _ = server.accept()
            with connection:
                assert json.loads(connection.recv(65536)) == {"op": "ping"}
                connection.sendall(b'{"ok":true}\n')

        future = pool.submit(answer)
        assert launcher.healthy()
        future.result(timeout=3)
    with pytest.raises(launcher.Refused, match="launcher_unavailable"):
        launcher.healthy()


def test_legacy_claim_refuses_while_runs_holds_database_lock(monkeypatch, capsys):
    from homelab.tasks import claim

    connection = Database(False)

    @contextmanager
    def connect(*args, **kwargs):
        yield connection

    monkeypatch.setattr(claim.psycopg, "connect", connect)
    monkeypatch.setattr(claim, "claim", lambda _: pytest.fail("must not claim"))
    monkeypatch.setattr(claim, "recover", lambda _: pytest.fail("must not recover"))
    assert claim.main([]) == 2
    assert claim.main(["--recover"]) == 2
    assert "runner_active" in capsys.readouterr().err


def test_startup_rechecks_legacy_after_taking_database_lock(tmp_path):
    class RacingDatabase(Database):
        def execute(self, query, args=None):
            if "try_advisory_lock" in query:
                service = tmp_path / "homelab-task-runner.service"
                service.mkdir()
                (service / "cgroup.events").write_text("populated 1\n")
            return super().execute(query, args)

    connection = RacingDatabase()
    with pytest.raises(locking.Refused, match="legacy_runner_active"):
        with locking.runner_lock(connection, tmp_path):
            pytest.fail("must not recover or claim")
    assert any("unlock" in query for query in connection.queries)


def test_compose_disables_runs_and_preserves_ownership_boundary():
    import yaml

    from tests.test_launcher import ROOT

    compose = yaml.safe_load((ROOT / "compose.yaml").read_text())
    service = compose["services"]["runs"]
    assert service["profiles"] == ["runs"]
    assert service["image"] == f"{compose['name']}-api"
    assert service["user"] == "21020:21020"
    assert service["cap_drop"] == ["ALL"]
    assert service["read_only"] is True
    assert "ports" not in service
    assert service["healthcheck"]["test"][-1] == "--healthcheck"
    mounts = {mount["target"]: mount for mount in service["volumes"]}
    assert mounts["/srv/homelab/task-work"]["read_only"] is True
    assert mounts["/run/host-services"]["read_only"] is True
    assert mounts["/run/secrets"]["read_only"] is True
    assert all(
        "docker.sock" not in str(mount) and "/login" not in str(mount) for mount in mounts.values()
    )


@pytest.mark.parametrize("outcome", ["exit", "timeout", "flood"])
def test_launcher_capture_enforces_deadline_and_output_limit(tmp_path, monkeypatch, outcome):
    import os
    import threading

    from homelab.runs import launcher

    stop = threading.Event()
    calls = []

    def request(value, descriptors=(), **kwargs):
        calls.append(value["op"])
        if value["op"] == "stop":
            stop.set()
        elif outcome == "exit":
            os.write(descriptors[0], b"OUT")
            os.write(descriptors[1], b"ERR")
            return {"ok": True, "exit_code": 17}
        elif outcome == "flood":
            os.write(descriptors[0], b"X" * 3000)
            stop.wait(5)
        else:
            stop.wait(5)
        return {"ok": True, "exit_code": 0}

    monkeypatch.setattr(launcher, "request", request)
    result = launcher.capture(
        {"op": "run", "run_id": "fixture"},
        tmp_path / "out",
        tmp_path / "err",
        0.1 if outcome == "timeout" else 2,
        limit=1024,
    )
    if outcome == "exit":
        assert result == (17, "exited")
        assert (tmp_path / "out").read_bytes() == b"OUT"
        assert (tmp_path / "err").read_bytes() == b"ERR"
        assert calls == ["run"]
    else:
        assert result == (None, "timeout" if outcome == "timeout" else "output_limit")
        assert calls == ["run", "stop"]
        assert (tmp_path / "out").stat().st_size <= 1024
    assert not launcher.active


def test_readonly_host_flock_excludes_unchanged_manual_runner(tmp_path):
    import fcntl

    path = tmp_path / "run.lock"
    path.touch()
    with locking.host_run_lock(path):
        with path.open("w") as manual:
            with pytest.raises(BlockingIOError):
                fcntl.flock(manual, fcntl.LOCK_EX | fcntl.LOCK_NB)
    with path.open("w") as manual:
        fcntl.flock(manual, fcntl.LOCK_EX | fcntl.LOCK_NB)
        with pytest.raises(locking.Refused, match="manual_run_active"):
            with locking.host_run_lock(path):
                pytest.fail("must not claim during manual run")


def test_missing_host_flock_fails_closed(tmp_path):
    with pytest.raises(locking.Refused, match="host_run_lock_unavailable"):
        with locking.host_run_lock(tmp_path / "missing"):
            pytest.fail("must not claim")


def test_sanitized_git_view_never_reads_work_copy_config(tmp_path):
    from homelab.runs import runner

    repo = tmp_path / "repo"
    repo.mkdir()
    subprocess.run(["git", "init", "-q", str(repo)], check=True)
    sentinel = tmp_path / "sentinel"
    hook = tmp_path / "fsmonitor"
    hook.write_text("#!/bin/sh\ntouch " + str(sentinel) + "\n")
    hook.chmod(0o700)
    subprocess.run(["git", "-C", str(repo), "config", "core.fsmonitor", str(hook)], check=True)
    subprocess.run(
        ["git", "-C", str(repo), "config", "include.path", "/PRIVATE-CONFIG"], check=True
    )
    with runner.sealed_git_dir(repo) as directory:
        result = subprocess.run(
            ["git", "--git-dir", str(directory), "config", "--local", "--get", "core.fsmonitor"],
            capture_output=True,
            env=runner.git_env(),
            cwd="/",
        )
        assert result.returncode == 1 and not result.stdout
        assert not (directory / "config").exists()
        assert (directory / "objects").resolve() == repo / ".git/objects"
    assert not sentinel.exists()


def test_push_deny_list_still_precedes_token_and_push(tmp_path, monkeypatch):
    from homelab.runs import runner

    monkeypatch.setattr(
        delivery, "git", lambda *a: SimpleNamespace(stdout=("a" * 40 + "\n").encode())
    )

    monkeypatch.setattr(runner, "refuse_branch", lambda _: None)
    monkeypatch.setattr(
        runner, "private_content_check", lambda *args, **kwargs: "push_private_content: path:1"
    )
    monkeypatch.setattr(runner, "installation_token", lambda _: pytest.fail("must not mint"))
    assert runner.push(
        {"repository": "owner/repo", "branch": "slice/test"}, tmp_path, "a" * 40
    ) == ("push_private_content: path:1", [])


def test_default_branch_refusal_precedes_minting(tmp_path, monkeypatch):
    from homelab.runs import runner

    monkeypatch.setattr(runner, "default_branch", lambda _: "trunk")
    monkeypatch.setattr(runner, "installation_token", lambda _: pytest.fail("must not mint"))
    for branch in ("main", "trunk"):
        assert runner.push({"repository": "owner/repo", "branch": branch}, tmp_path, "a" * 40) == (
            "push_default_branch_refused",
            [],
        )


def test_service_entry_refuses_legacy_runner_before_recovery_or_claim(
    tmp_path, monkeypatch, caplog
):
    from homelab.runs import runner

    service = tmp_path / "homelab-task-runner.service"
    service.mkdir()
    (service / "cgroup.events").write_text("populated 1\nfrozen 0\n")
    database = Database()

    @contextmanager
    def connect(*args, **kwargs):
        yield database

    lock = locking.runner_lock
    monkeypatch.setattr(locking, "runner_lock", lambda connection: lock(connection, tmp_path))
    monkeypatch.setattr(runner.psycopg, "connect", connect)
    monkeypatch.setattr(runner.os, "geteuid", lambda: 21020)
    monkeypatch.setattr(runner.signal, "signal", lambda *args: None)
    monkeypatch.setattr(runner, "connection", None)
    monkeypatch.setattr(runner, "recover", lambda: pytest.fail("must not recover"))
    monkeypatch.setattr(runner, "store", lambda *args, **kwargs: pytest.fail("must not claim"))
    monkeypatch.setattr(runner.launcher, "stop_all", lambda: None)
    assert runner.main([]) == 2
    assert not database.queries
    assert "runner refused reason=legacy_runner_active" in caplog.text


def test_ambiguous_ticket_claim_stops_before_recovery_beside_live_cli_workers(monkeypatch):
    from homelab.runs import runner

    def unavailable(*args, **kwargs):
        raise runner.StoreUnavailable("database_error")

    monkeypatch.setattr(runner, "run_lock_free", lambda: True)
    monkeypatch.setattr(runner, "store", unavailable)
    monkeypatch.setattr(runner, "recover", lambda: pytest.fail("live workers must stop first"))
    state = {"recovered": True, "slices": object()}
    with pytest.raises(runner.LostLock):
        runner.turn(state)
    assert state["recovered"]


def test_recovery_removes_launcher_containers_before_recording_interruptions(tmp_path, monkeypatch):
    from homelab.runs import runner
    from homelab.tasks import slices

    calls = []
    monkeypatch.setattr(runner, "WORK", tmp_path)
    monkeypatch.setattr(runner.launcher, "request", lambda r: calls.append(r["op"]))
    monkeypatch.setattr(runner, "database", lambda: contextmanager(lambda: iter([None]))())
    monkeypatch.setattr(slices, "recover", lambda _: calls.append("interrupt") or 1)
    monkeypatch.setattr(runner, "store", lambda *a: calls.append("tickets") or 0)
    runner.recover()
    assert calls == ["task-recover", "interrupt", "tickets"]


def test_whole_run_capture_without_deadline_still_stops_on_shutdown(tmp_path, monkeypatch):
    import threading

    from homelab.runs import launcher

    entered, cancelled, stopped = threading.Event(), threading.Event(), threading.Event()

    def request(value, descriptors=(), **kwargs):
        if value["op"] == "stop":
            stopped.set()
        else:
            assert kwargs["timeout"] is None
            entered.set()
            assert stopped.wait(5)
        return {"ok": True, "exit_code": 0}

    def cancel():
        assert entered.wait(5)
        cancelled.set()

    monkeypatch.setattr(launcher, "request", request)
    thread = threading.Thread(target=cancel)
    thread.start()
    try:
        assert launcher.capture(
            {"op": "run", "run_id": "whole-run"},
            tmp_path / "out",
            tmp_path / "err",
            None,
            cancelled=cancelled.is_set,
        ) == (None, "interrupted")
    finally:
        cancelled.set()
        stopped.set()
        thread.join(5)
    assert stopped.is_set() and not launcher.active
