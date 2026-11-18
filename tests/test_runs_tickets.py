"""Tickets stay on the task queue and execute the catalogue, without hosted reviews."""

# ruff: noqa: F811

import json
import subprocess
from datetime import UTC, datetime
from types import SimpleNamespace

import pytest

from homelab.api import runs
from homelab.runs import runner, slices, usage
from homelab.runs.runner import push as deliver_ticket
from homelab.tasks import finish
from homelab.tasks.executors import Executor, load_executors
from tests.test_runs import CLIENT, HEADERS, api, body, post  # noqa: F401
from tests.test_runs_delivery import bundle, git  # noqa: F401
from tests.test_task_store import connection  # noqa: F401


@pytest.fixture
def ticket_runner(api, monkeypatch, tmp_path):
    monkeypatch.setattr(runner, "RUNS", tmp_path)
    monkeypatch.setattr(runner, "refuse_branch", lambda _: None)
    monkeypatch.setattr(runner, "fresh_copy", lambda _: tmp_path)
    monkeypatch.setattr(runner, "remove_copy", lambda _: None)
    monkeypatch.setattr(runner, "push", lambda *a: ("pushed", ["b" * 40]))
    monkeypatch.setattr(runner, "notify", lambda *a: None)
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
    assert result["commits"] == ["b" * 40]
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
    for number, mark in enumerate(
        (None, {"set": "T", "case": "one", "executor": {"configuration": "pi/base"}})
    ):
        post(body(request_id=f"pi-{number}", branch="eval/one" if mark else "work/one", eval=mark))
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
        return ("usage_limit" if selected.harness == "codex" else "pushed"), None, []

    monkeypatch.setattr(runner, "attempt", attempt)
    assert runner.work({"level": "light", "next_attempt": 1}) == ("done", None, [])
    assert seen == ["codex", "claude"]
    monkeypatch.setattr(runner, "attempt", lambda *a: ("cli_failed", None, []))
    assert runner.work({"level": "light", "next_attempt": 1}) == ("escalated", "cli_failed", [])


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
    assert seen[1].eval_mark == {
        "set": "T",
        "case": "one",
        "executor": {"configuration": "pi/base"},
    }
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
        return "push_rejected", None, []

    monkeypatch.setattr(runner, "attempt", attempt)
    assert runner.work({"level": "standard", "next_attempt": 1}) == (
        "escalated",
        "push_rejected",
        [],
    )
    assert len(seen) == 2 and seen[0][0] == seen[1][0]


@pytest.mark.parametrize("first", ["check_failed", "push_rejected"])
def test_ticket_records_only_successful_attempt_commits(
    api, ticket_runner, monkeypatch, tmp_path, first
):
    monkeypatch.setattr(slices, "codex_usage", lambda _: reading(0))
    (tmp_path / "check.log").write_text("Retry the check")
    attempts = []
    pushes = []

    def run_ticket(task, copy, executor, feedback, *, number):
        attempts.append(number)
        return {
            "ended": "check_failed" if first == "check_failed" and number == 1 else "committed",
            "commit": ("a" if number == 1 else "c") * 40,
            "run_folder": str(tmp_path),
        }

    def push(task, copy, commit):
        pushes.append(commit)
        return ("push_rejected", []) if commit == "a" * 40 else ("pushed", ["b" * 40, "c" * 40])

    monkeypatch.setattr(runner, "run_ticket", run_ticket)
    monkeypatch.setattr(runner, "push", push)
    run_id = post().json()["run_id"]
    runner.run_task(runner.store("claim"))
    result = CLIENT.get(f"/v1/runs/{run_id}", headers=HEADERS).json()
    assert result["status"] == "done"
    assert result["commits"] == ["b" * 40, "c" * 40]
    assert attempts == [1, 2]
    assert pushes == (["a" * 40, "c" * 40] if first == "push_rejected" else ["c" * 40])


@pytest.mark.parametrize("end", ["clone_failed", "agent_failed", "check_failed", "push_failed"])
def test_other_ticket_endings_leave_commits_unchanged(
    api, ticket_runner, monkeypatch, tmp_path, end
):
    monkeypatch.setattr(slices, "codex_usage", lambda _: reading(0))
    (tmp_path / "check.log").write_text("Failed check")
    document = body()
    run_id = post(document).json()["run_id"]
    api.execute("UPDATE homelab_runs SET commits = %s", (["e" * 40],))
    monkeypatch.setattr(
        runner,
        "run_ticket",
        lambda *a, **kw: {
            "ended": "committed" if end == "push_failed" else end,
            "commit": "a" * 40,
            "run_folder": str(tmp_path),
        },
    )
    if end == "clone_failed":
        monkeypatch.setattr(runner, "fresh_copy", lambda _: None)
    monkeypatch.setattr(runner, "push", lambda *a: ("push_failed", []))
    runner.run_task(runner.store("claim"))
    result = CLIENT.get(f"/v1/runs/{run_id}", headers=HEADERS).json()
    assert result["commits"] == ["e" * 40]
    assert result["status"] == "escalated"


def test_ticket_finish_appends_in_order_once_and_rolls_back_with_run_end(api):
    run_id = post().json()["run_id"]
    task = runner.claim.claim(api, legacy_roles=False)
    old, first, last = [letter * 40 for letter in "cab"]
    api.execute("UPDATE homelab_runs SET commits = %s", ([old, first],))
    event = finish.EndTask(
        event="end_task",
        task_id=task["id"],
        job_id=task["job_id"],
        status="done",
        commits=[first, last, first],
    )

    class FailingJobFinish:
        def __getattr__(self, name):
            return getattr(api, name)

        def execute(self, query, *args, **kwargs):
            if "procrastinate_finish_job_v1" in query:
                raise RuntimeError("simulated finish failure")
            return api.execute(query, *args, **kwargs)

    with pytest.raises(RuntimeError, match="simulated finish failure"):
        finish.record(FailingJobFinish(), event)
    assert api.execute("SELECT status, commits FROM homelab_runs").fetchone() == (
        "running",
        [old, first],
    )
    finish.record(api, event)
    finish.record(api, event.model_copy(update={"commits": ["f" * 40]}))
    result = CLIENT.get(f"/v1/runs/{run_id}", headers=HEADERS).json()
    assert result["status"] == "done" and result["commits"] == [old, first, last]
    assert result["final_message"] is None


@pytest.mark.parametrize(
    "status,reason", [("failed", None), ("escalated", "push_failed"), ("done", "discarded")]
)
def test_finish_ignores_commits_from_other_endings(api, status, reason):
    post()
    task = runner.claim.claim(api, legacy_roles=False)
    api.execute("UPDATE homelab_runs SET commits = %s", (["e" * 40],))
    finish.record(
        api,
        finish.EndTask(
            event="end_task",
            task_id=task["id"],
            job_id=task["job_id"],
            status=status,
            reason=reason,
            commits=["a" * 40],
        ),
    )
    assert api.execute("SELECT commits FROM homelab_runs").fetchone() == (["e" * 40],)


def test_ticket_record_receives_the_range_from_real_git(api, ticket_runner, bundle, monkeypatch):
    copy, _, document, first = bundle
    git(copy, "update-ref", "refs/remotes/origin/slice/test", document["base_commit"])
    (copy / "src/app").write_text("another change")
    git(copy, "commit", "-qam", "Another change")
    tip = git(copy, "rev-parse", "HEAD")
    monkeypatch.setattr(slices, "codex_usage", lambda _: reading(0))
    monkeypatch.setattr(runner, "fresh_copy", lambda _: copy)
    monkeypatch.setattr(runner, "push", deliver_ticket)
    monkeypatch.setattr(runner, "installation_token", lambda _: "FAKE-APP-TOKEN")
    monkeypatch.setattr(runner, "repository_is_private", lambda _: True)
    monkeypatch.setattr(
        runner, "run_ticket", lambda *a, **kw: {"ended": "committed", "commit": tip}
    )
    real_run = subprocess.run

    def run(command, **kwargs):
        if "push" in command:
            return SimpleNamespace(returncode=0, stdout="")
        return real_run(command, **kwargs)

    monkeypatch.setattr(subprocess, "run", run)
    run_id = post(body(branch="slice/test")).json()["run_id"]
    runner.run_task(runner.store("claim"))
    result = CLIENT.get(f"/v1/runs/{run_id}", headers=HEADERS).json()
    assert result["status"] == "done" and result["commits"] == [first, tip]
