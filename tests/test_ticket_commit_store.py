"""The ticket finish protocol appends delivered SHAs atomically, without a migration."""

import hashlib
import json
from pathlib import Path

import psycopg
import pytest

from homelab.tasks import claim, finish
from homelab.tasks.store import Refused
from tests.test_runs import CLIENT, HEADERS, OTHER, api, body, post  # noqa: F401
from tests.test_task_store import connection  # noqa: F401

OLD, FIRST, SECOND = "f" * 40, "b" * 40, "a" * 40


def event(task=None, **changes):
    task = task or {"id": 1, "job_id": 2}
    return {
        "event": "end_task",
        "task_id": task["id"],
        "job_id": task["job_id"],
        "status": "done",
        "reason": None,
        **changes,
    }


def parsed(task=None, **changes):
    return finish.parse(json.dumps(event(task, **changes)).encode())


def test_finish_protocol_accepts_sha_lists_and_legacy_events():
    assert parsed().commits == []
    assert parsed(commits=[FIRST, SECOND, "c" * 64]).commits == [FIRST, SECOND, "c" * 64]
    changed = parsed()
    changed.commits.append(FIRST)
    assert parsed().commits == []


@pytest.mark.parametrize("commits", [None, "a" * 40, [123], ["a" * 39], ["G" * 40], ["a" * 65]])
def test_finish_protocol_rejects_invalid_commit_lists_without_content(commits):
    with pytest.raises(Refused) as error:
        parsed(commits=commits)
    assert "commits" in str(error.value)
    assert "G" * 40 not in str(error.value)


def test_finish_keeps_the_existing_schema_file():
    # This task explicitly requires no migration. Freeze the starting schema, not Python machinery.
    schema = Path(__file__).resolve().parents[1] / "src/homelab/tasks/schema.sql"
    assert hashlib.sha256(schema.read_bytes()).hexdigest() == (
        "cc22e84da548c92f4769c13975b59eee65f391ea70154cb9950edb708de7c9b4"
    )


@pytest.fixture
def running_ticket(api):  # noqa: F811
    response = post()
    assert response.status_code == 201
    run_id = response.json()["run_id"]
    task = claim.claim(api)
    api.execute("UPDATE homelab_runs SET commits = %s WHERE task_id = %s", ([OLD], task["id"]))
    return api, run_id, task


def read(run_id):
    response = CLIENT.get(f"/v1/runs/{run_id}", headers=HEADERS)
    assert response.status_code == 200
    return response.json()


def test_finish_appends_in_order_deduplicates_and_ignores_replayed_end(running_ticket):
    database, run_id, task = running_ticket
    ending = parsed(task, commits=[FIRST, OLD, FIRST, SECOND, SECOND])
    assert finish.record(database, ending) == []
    saved = read(run_id)
    assert saved["status"] == "done" and saved["status_reason"] is None
    assert saved["commits"] == [OLD, FIRST, SECOND]
    assert saved["final_message"] is None
    assert database.execute("SELECT status FROM homelab_tasks").fetchone() == ("done",)
    assert database.execute("SELECT status::text FROM procrastinate_jobs").fetchone() == (
        "succeeded",
    )
    finish.record(database, ending)
    # Even a different payload for an already closed task must not append anything.
    finish.record(database, parsed(task, commits=["c" * 40]))
    assert read(run_id)["commits"] == [OLD, FIRST, SECOND]
    assert CLIENT.get(f"/v1/runs/{run_id}", headers=OTHER).status_code == 404
    listed = CLIENT.get("/v1/runs?request_id=ticket-1", headers=HEADERS).json()
    assert listed["runs"][0]["commits"] == [OLD, FIRST, SECOND]


def test_finish_first_push_populates_initially_empty_commits(running_ticket):
    database, run_id, task = running_ticket
    database.execute("UPDATE homelab_runs SET commits = '{}' WHERE task_id = %s", (task["id"],))
    finish.record(database, parsed(task, commits=[FIRST, SECOND]))
    assert read(run_id)["commits"] == [FIRST, SECOND]


@pytest.mark.parametrize(
    "changes",
    [
        {"status": "failed", "reason": "runner_error_ValueError"},
        {"status": "escalated", "reason": "push_rejected"},
        {"status": "escalated", "reason": "check_failed"},
        {"status": "done", "reason": "eval_kept"},
    ],
)
def test_finish_other_endings_preserve_commits_even_with_a_payload(running_ticket, changes):
    database, run_id, task = running_ticket
    finish.record(database, parsed(task, commits=[FIRST, SECOND], **changes))
    assert read(run_id)["commits"] == [OLD]


def test_finish_old_protocol_preserves_existing_commits(running_ticket):
    database, run_id, task = running_ticket
    finish.record(database, parsed(task))
    assert read(run_id)["commits"] == [OLD]


def test_finish_eval_record_cannot_gain_commits_from_a_success_payload(api):  # noqa: F811
    response = post(body(eval={"set": "T", "case": "one", "configuration": "base"}))
    assert response.status_code == 201
    task = claim.claim(api)
    finish.record(api, parsed(task, commits=[FIRST]))
    assert read(response.json()["run_id"])["commits"] == []


@pytest.mark.parametrize("failure", ["commits", "job"])
def test_finish_commit_and_terminal_state_roll_back_together(running_ticket, failure):
    database, run_id, task = running_ticket

    class FailDuringFinish:
        def transaction(self):
            return database.transaction()

        def execute(self, query, args=None):
            normalized = " ".join(query.lower().split())
            if failure == "job" and "procrastinate_finish_job_v1" in normalized:
                raise psycopg.OperationalError("injected job failure")
            if failure == "commits" and normalized.startswith("update homelab_runs"):
                if "commits" in normalized:
                    raise psycopg.OperationalError("injected commits failure")
            return database.execute(query, args)

    with pytest.raises(psycopg.OperationalError, match="injected"):
        finish.record(FailDuringFinish(), parsed(task, commits=[FIRST, SECOND]))
    saved = read(run_id)
    assert saved["status"] == "running" and saved["commits"] == [OLD]
    assert database.execute("SELECT status FROM homelab_tasks").fetchone() == ("running",)
    assert database.execute("SELECT status::text FROM procrastinate_jobs").fetchone() == ("doing",)
    finish.record(database, parsed(task, commits=[FIRST, SECOND]))
    assert read(run_id)["status"] == "done"
    assert read(run_id)["commits"] == [OLD, FIRST, SECOND]


def test_finish_commits_are_invisible_until_the_terminal_transaction_commits(running_ticket):
    database, run_id, task = running_ticket
    with psycopg.connect(database.info.dsn, autocommit=True) as observer:
        with database.transaction():
            finish.record(database, parsed(task, commits=[FIRST, SECOND]))
            assert observer.execute("SELECT status, commits FROM homelab_runs").fetchone() == (
                "running",
                [OLD],
            )
        assert observer.execute("SELECT status, commits FROM homelab_runs").fetchone() == (
            "done",
            [OLD, FIRST, SECOND],
        )
