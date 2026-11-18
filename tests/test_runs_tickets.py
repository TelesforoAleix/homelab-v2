"""Tickets stay on the task queue and execute the catalogue, without hosted reviews."""

# ruff: noqa: F811

import json
from datetime import UTC, datetime

import pytest

from homelab.api import runs
from homelab.runs import runner, slices, usage
from homelab.tasks import finish
from homelab.tasks.executors import Executor, load_executors
from tests.test_runs import CLIENT, HEADERS, api, body, post  # noqa: F401
from tests.test_task_store import connection  # noqa: F401


@pytest.fixture
def ticket_runner(api, monkeypatch, tmp_path):
    monkeypatch.setattr(runner, "RUNS", tmp_path)
    monkeypatch.setattr(runner, "refuse_branch", lambda _: None)
    monkeypatch.setattr(runner, "fresh_copy", lambda _: tmp_path)
    monkeypatch.setattr(runner, "remove_copy", lambda _: None)
    monkeypatch.setattr(runner, "push", lambda *a: "pushed")
    monkeypatch.setattr(runner, "notify", lambda *a: None)
    monkeypatch.setattr(runner, "keep_eval_run", lambda *a: None)
    monkeypatch.setattr(
        runner, "record", lambda e: finish.record(api, finish.parse(json.dumps(e).encode()))
    )
    monkeypatch.setattr(runner, "connection", api)
    monkeypatch.setattr(runner.time, "time", lambda: 1_800_000_000)
    calls = []

    def run_ticket(task, copy, executor, feedback, *, number):
        calls.append((executor, feedback))
        return {"ended": "committed", "commit": "b" * 40}

    monkeypatch.setattr(runner, "run_ticket", run_ticket)
    return calls


def reading(percent, reset=500):
    return usage.Reading(percent, 1_800_000_000 + reset, 1_800_000_000)


@pytest.mark.parametrize("level", ["light", "standard", "strong"])
@pytest.mark.parametrize("limited", [False, True])
def test_ticket_records_executor_that_runs_on_task_queue(
    api, ticket_runner, monkeypatch, level, limited
):
    monkeypatch.setattr(slices, "codex_usage", lambda _: reading(99 if limited else 0))
    monkeypatch.setattr(slices, "claude_probe", lambda _: reading(0))
    response = post(body(level=level))
    assert response.status_code == 201
    assert api.execute("SELECT queue_name FROM procrastinate_jobs").fetchone() == ("tasks",)
    task = runner.store("claim")
    runner.run_task(task)
    selected = load_executors()["ticket"][level][int(limited)]
    result = CLIENT.get(f"/v1/runs/{response.json()['run_id']}", headers=HEADERS).json()
    assert result["status"] == "done"
    assert result["executor"] == selected.selection()
    assert result["configuration_hash"] == selected.fingerprint()
    assert ticket_runner == [(selected, None)]
    assert api.execute("SELECT route FROM homelab_task_attempts").fetchone() == (
        selected.selection(),
    )
    assert api.execute(
        "SELECT count(*) FROM homelab_task_traces WHERE kind = 'review'"
    ).fetchone() == (0,)


def test_both_limited_waits_and_schedules_idempotent_retry(api, ticket_runner, monkeypatch):
    monkeypatch.setattr(slices, "codex_usage", lambda _: reading(99))
    monkeypatch.setattr(slices, "claude_probe", lambda _: reading(90, 300))
    run_id = post().json()["run_id"]
    task = runner.store("claim")
    runner.run_task(task)
    assert ticket_runner == []
    status, reason, resume = api.execute(
        "SELECT status, status_reason, resume_at FROM homelab_runs WHERE id = %s", (run_id,)
    ).fetchone()
    assert (status, reason) == ("waiting", "usage_limit")
    assert resume == datetime.fromtimestamp(1_800_000_300, UTC)
    assert api.execute("SELECT status FROM homelab_tasks").fetchone() == ("queued",)
    assert api.execute("SELECT status, scheduled_at FROM procrastinate_jobs").fetchone() == (
        "todo",
        resume,
    )
    assert api.execute("SELECT count(*) FROM homelab_task_attempts").fetchone() == (0,)
    finish.record(
        api,
        finish.DeferUsage(
            event="defer_usage", task_id=task["id"], job_id=task["job_id"], reset=1_800_000_600
        ),
    )
    assert api.execute("SELECT scheduled_at FROM procrastinate_jobs").fetchone() == (resume,)
    api.execute("UPDATE procrastinate_jobs SET scheduled_at = now()")
    monkeypatch.setattr(slices, "codex_usage", lambda _: reading(0))
    runner.run_task(runner.store("claim"))
    assert api.execute("SELECT status, resume_at FROM homelab_runs").fetchone() == ("done", None)


def test_active_claim_does_not_read_role_table(api, ticket_runner, monkeypatch):
    post()

    class Guarded:
        def __getattr__(self, name):
            return getattr(api, name)

        def execute(self, query, *args, **kwargs):
            assert "homelab_roles" not in str(query)
            return api.execute(query, *args, **kwargs)

    monkeypatch.setattr(runner, "connection", Guarded())
    assert runner.store("claim")["level"] == "standard"


@pytest.mark.parametrize("last", ["committed", "check_failed"])
def test_failed_check_retries_once_with_output_then_finishes(
    api, ticket_runner, monkeypatch, tmp_path, last
):
    monkeypatch.setattr(slices, "codex_usage", lambda _: reading(0))
    monkeypatch.setattr(
        slices, "claude_probe", lambda _: pytest.fail("no fallback on check failure")
    )
    (tmp_path / "check.log").write_text("The check failed with actionable output")
    seen = []

    def run_ticket(task, copy, executor, feedback, *, number):
        seen.append((executor, feedback, runner.ticket(task, feedback)))
        return {
            "ended": "check_failed" if len(seen) == 1 else last,
            "run_folder": str(tmp_path),
            "commit": "b" * 40,
        }

    monkeypatch.setattr(runner, "run_ticket", run_ticket)
    post()
    runner.run_task(runner.store("claim"))
    assert len(seen) == 2 and seen[0][0] == seen[1][0]
    assert seen[0][1] is None
    assert seen[1][1] == ("check", "The check failed with actionable output")
    assert "The check failed with actionable output" in seen[1][2]
    expected = ("done", None) if last == "committed" else ("escalated", "check_failed")
    assert api.execute("SELECT status, status_reason FROM homelab_runs").fetchone() == expected


def test_eval_and_non_eval_pi_configuration_run_without_usage_or_review(
    api, ticket_runner, monkeypatch
):
    monkeypatch.setattr(slices, "codex_usage", lambda _: pytest.fail("Pi has no harness usage"))
    monkeypatch.setattr(slices, "claude_probe", lambda _: pytest.fail("Pi has no harness usage"))
    table = load_executors()
    table["ticket"]["standard"] = [Executor(configuration="pi/base")]
    monkeypatch.setattr(runs, "_table", table)
    monkeypatch.setattr(runner, "load_executors", lambda: table)
    for number, mark in enumerate((None, {"set": "T", "case": "one", "configuration": "base"})):
        post(body(request_id=f"pi-{number}", eval=mark))
        runner.run_task(runner.store("claim"))
    assert [executor.configuration for executor, _ in ticket_runner] == ["pi/base", "pi/base"]
    assert api.execute("SELECT count(*) FROM homelab_runs WHERE status = 'done'").fetchone() == (2,)


def test_live_usage_limit_falls_back_but_other_failures_escalate(tmp_path, monkeypatch):
    monkeypatch.setattr(runner, "RUNS", tmp_path)
    monkeypatch.setattr(slices, "codex_usage", lambda _: reading(0))
    monkeypatch.setattr(slices, "claude_probe", lambda _: reading(0))
    seen = []

    def attempt(task, selected, number, feedback):
        seen.append(selected.harness)
        return ("usage_limit" if selected.harness == "codex" else "pushed"), None

    monkeypatch.setattr(runner, "attempt", attempt)
    assert runner.work({"level": "light", "next_attempt": 1}) == ("done", None)
    assert seen == ["codex", "claude"]
    monkeypatch.setattr(runner, "attempt", lambda *a: ("cli_failed", None))
    assert runner.work({"level": "light", "next_attempt": 1}) == ("escalated", "cli_failed")


def test_runner_passes_named_configuration_and_no_review_argument(tmp_path, monkeypatch):
    seen = []
    monkeypatch.setenv("HOMELAB_TASK_AUTHOR_NAME", "Fixture")
    monkeypatch.setenv("HOMELAB_TASK_AUTHOR_EMAIL", "fixture@example.invalid")
    monkeypatch.setattr(
        runner,
        "configuration_snapshot",
        lambda folder, name: (seen.append(name) or tmp_path, "a" * 40),
    )
    monkeypatch.setattr(
        runner.task_run, "run", lambda args: seen.append(args) or {"ended": "committed"}
    )
    task = {
        "id": 1,
        "title": "Ticket",
        "goal": "Fixture",
        "check_command": "true",
        "pinned_paths": [],
        "allowed_files": ["src"],
        "eval_set": "T",
        "eval_case": "one",
        "eval_configuration": "base",
    }
    runner.run_ticket(task, tmp_path, Executor(configuration="pi/base"), None, number=1)
    assert seen[0] == "pi/base"
    assert seen[1].eval_mark == {"set": "T", "case": "one", "configuration": "base"}
    assert seen[1].executor is None and not hasattr(seen[1], "review_purpose")


def test_check_feedback_and_retry_count_survive_usage_wait(
    api, ticket_runner, monkeypatch, tmp_path
):
    monkeypatch.setattr(slices, "codex_usage", lambda _: reading(0))
    monkeypatch.setattr(slices, "claude_probe", lambda _: reading(90, 300))
    seen = []

    def run_ticket(task, copy, executor, feedback, *, number):
        seen.append(feedback)
        if number == 2:
            return {"ended": "usage_limit"}
        folder = tmp_path / f"run-{number}"
        folder.mkdir()
        (folder / "check.log").write_text("Check output for the retry")
        return {"ended": "check_failed", "run_id": folder.name, "run_folder": str(folder)}

    monkeypatch.setattr(runner, "run_ticket", run_ticket)
    post()
    runner.run_task(runner.store("claim"))
    assert api.execute("SELECT status_reason FROM homelab_runs").fetchone() == ("usage_limit",)
    api.execute("UPDATE procrastinate_jobs SET scheduled_at = now()")
    runner.run_task(runner.store("claim"))
    assert seen == [
        None,
        ("check", "Check output for the retry"),
        ("check", "Check output for the retry"),
    ]
    assert api.execute("SELECT status, status_reason FROM homelab_runs").fetchone() == (
        "escalated",
        "check_failed",
    )


def test_push_rejection_retries_once_with_same_table_choice(tmp_path, monkeypatch):
    monkeypatch.setattr(runner, "RUNS", tmp_path)
    monkeypatch.setattr(slices, "codex_usage", lambda _: reading(0))
    seen = []

    def attempt(task, selected, number, feedback):
        seen.append((selected, number))
        return "push_rejected", None

    monkeypatch.setattr(runner, "attempt", attempt)
    assert runner.work({"level": "standard", "next_attempt": 1}) == ("escalated", "push_rejected")
    assert len(seen) == 2 and seen[0][0] == seen[1][0]
