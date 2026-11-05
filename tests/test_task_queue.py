"""Claim and finish: events validate everywhere; the rest needs an isolated Postgres database."""

import json
import os
import subprocess
import sys
from concurrent.futures import ThreadPoolExecutor
from threading import Barrier

import psycopg
import pytest
from psycopg.conninfo import make_conninfo

from homelab.tasks import claim as claiming
from homelab.tasks import finish
from homelab.tasks.store import Refused
from homelab.tasks.submit import parse, submit
from tests.test_task_store import connection, encoded, entry, jobs, task_file  # noqa: F401

ROUTE = {"kind": "litellm", "purpose": "task-worker"}


def plan(*tasks, **fields):
    return parse(encoded(task_file(*tasks, **fields)))


def job_states(database):
    return [row[3] for row in jobs(database)]


def statuses(database):
    rows = database.execute("SELECT status FROM homelab_tasks ORDER BY position").fetchall()
    return [row[0] for row in rows]


def end(database, task, status, reason=None):
    event = finish.EndTask(
        event="end_task", task_id=task["id"], job_id=task["job_id"], status=status, reason=reason
    )
    return finish.record(database, event)


@pytest.mark.parametrize(
    "event",
    [
        {},
        {"event": "unknown"},
        {"event": "end_task", "task_id": 1, "job_id": 1, "status": "queued"},
        {"event": "end_task", "task_id": "1", "job_id": 1, "status": "done"},
        {"event": "begin_attempt", "task_id": 1, "number": 0, "route": ROUTE},
        {
            "event": "end_attempt",
            "task_id": 1,
            "number": 1,
            "route": ROUTE,
            "status": "failed",
            "reviews": [{"result": "pass"}],
        },
        {
            "event": "end_attempt",
            "task_id": 1,
            "number": 1,
            "route": ROUTE,
            "status": "failed",
            "review_route": ROUTE,
            "reviews": [{"result": "maybe"}],
        },
        {
            "event": "end_attempt",
            "task_id": 1,
            "number": 1,
            "route": ROUTE,
            "status": "failed",
            "review_route": ROUTE,
            "reviews": [{"result": "pass", "steps": -1}],
        },
        {"event": "end_attempt", "task_id": 1, "number": 1, "route": ROUTE, "status": "running"},
        {
            "event": "end_attempt",
            "task_id": 1,
            "number": 1,
            "route": ROUTE,
            "status": "done",
            "steps": -1,
        },
    ],
)
def test_bad_event_is_refused(event):
    with pytest.raises(Refused):
        finish.parse(json.dumps(event).encode())


def test_command_refuses_without_repeating_or_touching_the_database():
    result = subprocess.run(
        [sys.executable, "-m", "homelab.tasks.finish"],
        input=b'{"event": "end_task", "task_id": 1, "job_id": 1, "status": "do-not-repeat"}',
        capture_output=True,
        env={**os.environ, "HOMELAB_DATABASE_URL": "postgresql://nobody@127.0.0.1:9/none"},
    )
    assert result.returncode == 1
    assert b"status" in result.stderr
    assert b"do-not-repeat" not in result.stderr


def test_claim_refuses_an_unknown_argument(capsys):
    assert claiming.main(["--everything"]) == 2


def test_claim_hands_over_the_task_and_moves_both_states(connection):  # noqa: F811
    (task_id,) = submit(connection, plan(entry(1, pinned_paths=["tests"])))

    task = claiming.claim(connection)

    assert task["id"] == task_id
    assert task["repository"] == "example/repo"
    assert task["allowed_files"] == ["src/app.py"]
    assert task["pinned_paths"] == ["tests"]
    assert task["routes"] == [ROUTE]
    assert task["review_routes"] == [ROUTE]
    assert task["next_attempt"] == 1
    assert statuses(connection) == ["running"]
    assert job_states(connection) == ["doing"]


def test_claim_hands_over_the_reviewer_routes_of_its_own_role(connection):  # noqa: F811
    edited = [{"kind": "litellm", "purpose": "task-review"}, {"kind": "cli", "tool": "t"}]
    connection.execute(
        "UPDATE homelab_roles SET routes = %s::jsonb WHERE role = 'ticket-reviewer'",
        (json.dumps(edited),),
    )
    submit(connection, plan(entry(1)))
    task = claiming.claim(connection)
    assert task["review_routes"] == edited
    assert task["routes"] == [ROUTE]


def test_nothing_to_claim_is_none(connection):  # noqa: F811
    assert claiming.claim(connection) is None


def test_a_claimed_job_is_not_claimed_again(connection):  # noqa: F811
    submit(connection, plan(entry(1)))
    assert claiming.claim(connection) is not None
    assert claiming.claim(connection) is None


def test_highest_priority_goes_first(connection):  # noqa: F811
    submit(connection, plan(entry(1), entry(2, priority=5)))
    assert claiming.claim(connection)["title"] == "Task 2"


def second_url(database):
    (path,) = database.execute("SHOW search_path").fetchone()
    return make_conninfo(os.environ["HOMELAB_TEST_DATABASE_URL"], options="-c search_path=" + path)


def test_two_claims_racing_never_share_a_job(connection):  # noqa: F811
    submit(connection, plan(entry(1), entry(2), entry(3)))
    url = second_url(connection)
    start = Barrier(4)

    def claimant(_):
        with psycopg.connect(url, autocommit=True) as database:
            start.wait()
            return claiming.claim(database)

    with ThreadPoolExecutor(4) as pool:
        claimed = list(pool.map(claimant, range(4)))

    ids = sorted(task["id"] for task in claimed if task)
    assert len(ids) == len(set(ids)) == 3
    assert claimed.count(None) == 1


def test_a_claim_in_flight_hides_its_job_from_another(connection):  # noqa: F811
    submit(connection, plan(entry(1)))
    with psycopg.connect(second_url(connection)) as other:
        assert claiming.claim(other) is not None
        # Not yet committed: the row is locked and the other caller skips it.
        assert claiming.claim(connection) is None
    assert claiming.claim(connection) is None


def test_a_dead_runner_returns_its_task_to_the_queue(connection):  # noqa: F811
    submit(connection, plan(entry(1)))
    task = claiming.claim(connection)
    begin = finish.BeginAttempt(event="begin_attempt", task_id=task["id"], number=1, route=ROUTE)
    finish.record(connection, begin)

    assert claiming.recover(connection) == 1

    assert statuses(connection) == ["queued"]
    assert job_states(connection) == ["todo"]
    assert connection.execute("SELECT status, reason FROM homelab_task_attempts").fetchall() == [
        ("failed", "runner_died")
    ]
    again = claiming.claim(connection)
    assert again["id"] == task["id"]
    # The dead attempt keeps its number.
    assert again["next_attempt"] == 2


def test_recovery_leaves_finished_and_waiting_work_alone(connection):  # noqa: F811
    submit(connection, plan(entry(1), entry(2, 1)))
    end(connection, claiming.claim(connection), "done")
    assert claiming.recover(connection) == 0
    assert statuses(connection) == ["done", "queued"]


def test_a_job_whose_task_is_not_queued_does_not_run(connection):  # noqa: F811
    submit(connection, plan(entry(1)))
    connection.execute("UPDATE homelab_tasks SET status = 'done'")
    assert claiming.claim(connection) is None
    assert job_states(connection) == ["aborted"]
    assert statuses(connection) == ["done"]


def test_an_attempt_writes_its_row_and_its_lead_trace(connection):  # noqa: F811
    submit(connection, plan(entry(1)))
    task = claiming.claim(connection)
    finish.record(
        connection,
        finish.BeginAttempt(event="begin_attempt", task_id=task["id"], number=1, route=ROUTE),
    )
    event = finish.EndAttempt(
        event="end_attempt",
        task_id=task["id"],
        number=1,
        route=ROUTE,
        status="failed",
        reason="check_failed",
        run_id="run",
        check_exit_code=1,
        steps=4,
        fresh_tokens=100,
        cached_tokens=40,
        output_tokens=7,
        seconds=12.5,
    )

    finish.record(connection, event)
    finish.record(connection, event)

    assert connection.execute(
        """SELECT role, route, status, reason, run_id, check_exit_code, ended_at IS NOT NULL
           FROM homelab_task_attempts"""
    ).fetchall() == [("worker", ROUTE, "failed", "check_failed", "run", 1, True)]
    rows = connection.execute(
        """SELECT attempt, role, route, kind, steps, fresh_tokens, cached_tokens, output_tokens,
           cost_usd, seconds::float, result FROM homelab_task_traces"""
    ).fetchall()
    assert rows == [(1, "worker", ROUTE, "lead", 4, 100, 40, 7, None, 12.5, "check_failed")]


def test_an_event_with_reviews_parses():
    event = finish.parse(
        json.dumps(
            {
                "event": "end_attempt",
                "task_id": 1,
                "number": 1,
                "route": ROUTE,
                "status": "failed",
                "review_route": ROUTE,
                "reviews": [{"result": "fix", "steps": 2, "fresh_tokens": 5}],
            }
        ).encode()
    )
    assert event.reviews[0].result == "fix" and event.reviews[0].fresh_tokens == 5
    assert (
        finish.parse(
            json.dumps(
                {
                    "event": "end_attempt",
                    "task_id": 1,
                    "number": 1,
                    "route": ROUTE,
                    "status": "done",
                }
            ).encode()
        ).reviews
        == []
    )


def test_each_review_writes_one_trace_and_a_repeated_event_writes_nothing(connection):  # noqa: F811
    submit(connection, plan(entry(1)))
    task = claiming.claim(connection)
    finish.record(
        connection,
        finish.BeginAttempt(event="begin_attempt", task_id=task["id"], number=1, route=ROUTE),
    )
    reviewer = {"kind": "litellm", "purpose": "task-reviewer"}
    event = finish.EndAttempt(
        event="end_attempt",
        task_id=task["id"],
        number=1,
        route=ROUTE,
        status="failed",
        reason="review_rejected",
        steps=4,
        review_route=reviewer,
        reviews=[
            finish.Review(
                result="fix",
                steps=3,
                fresh_tokens=30,
                cached_tokens=20,
                output_tokens=5,
                seconds=9.5,
            ),
            finish.Review(result="fix", steps=2, fresh_tokens=10, seconds=4),
        ],
    )

    finish.record(connection, event)
    finish.record(connection, event)

    rows = connection.execute(
        """SELECT attempt, role, route, kind, steps, fresh_tokens, cached_tokens, output_tokens,
           cost_usd, seconds::float, result FROM homelab_task_traces ORDER BY id"""
    ).fetchall()
    assert rows == [
        (1, "worker", ROUTE, "lead", 4, 0, 0, 0, None, 0.0, "review_rejected"),
        (1, "ticket-reviewer", reviewer, "review", 3, 30, 20, 5, None, 9.5, "fix"),
        (1, "ticket-reviewer", reviewer, "review", 2, 10, 0, 0, None, 4.0, "fix"),
    ]


def test_an_attempt_with_no_review_writes_only_its_lead_trace(connection):  # noqa: F811
    submit(connection, plan(entry(1)))
    task = claiming.claim(connection)
    finish.record(
        connection,
        finish.BeginAttempt(event="begin_attempt", task_id=task["id"], number=1, route=ROUTE),
    )
    finish.record(
        connection,
        finish.EndAttempt(
            event="end_attempt", task_id=task["id"], number=1, route=ROUTE, status="done"
        ),
    )
    assert connection.execute("SELECT kind FROM homelab_task_traces").fetchall() == [("lead",)]


def test_done_releases_only_dependents_with_nothing_unmet(connection):  # noqa: F811
    ids = submit(connection, plan(entry(1), entry(2), entry(3, 1), entry(4, 1, 2)))
    first = claiming.claim(connection)
    second = claiming.claim(connection)

    assert end(connection, first, "done") == [ids[2]]
    assert statuses(connection) == ["done", "running", "queued", "queued"]
    assert end(connection, second, "done") == [ids[3]]
    assert job_states(connection) == ["succeeded", "succeeded", "todo", "todo"]


def test_a_released_dependent_keeps_its_priority(connection):  # noqa: F811
    submit(connection, plan(entry(1), entry(2, 1, priority=7)))
    end(connection, claiming.claim(connection), "done")
    assert jobs(connection)[-1][4] == 7


@pytest.mark.parametrize("status", ["escalated", "failed"])
def test_a_stopped_task_holds_its_dependents_with_no_job(connection, status):  # noqa: F811
    submit(connection, plan(entry(1), entry(2, 1)))
    task = claiming.claim(connection)

    assert end(connection, task, status, "check_failed") == []

    assert statuses(connection) == [status, "queued"]
    assert job_states(connection) == ["failed"]
    assert connection.execute(
        "SELECT reason, finished_at IS NOT NULL FROM homelab_tasks WHERE position = 1"
    ).fetchone() == ("check_failed", True)


def test_a_stopped_slice_runs_nothing_more_even_with_a_job(connection):  # noqa: F811
    submit(connection, plan(entry(1), entry(2)))
    end(connection, claiming.claim(connection), "escalated", "check_failed")

    assert claiming.claim(connection) is None

    assert statuses(connection) == ["escalated", "queued"]
    assert job_states(connection) == ["failed", "aborted"]


def test_another_slice_runs_on_after_one_stops(connection):  # noqa: F811
    submit(connection, plan(entry(1)))
    submit(connection, plan(entry(1), slice="other"))
    end(connection, claiming.claim(connection), "failed", "clone_failed")
    assert claiming.claim(connection) is not None


def test_ending_a_task_twice_changes_nothing(connection):  # noqa: F811
    ids = submit(connection, plan(entry(1), entry(2, 1)))
    task = claiming.claim(connection)
    assert end(connection, task, "done") == [ids[1]]
    assert end(connection, task, "done") == []
    assert len(jobs(connection)) == 2


def test_the_retry_of_an_attempt_row_is_ignored(connection):  # noqa: F811
    submit(connection, plan(entry(1)))
    task = claiming.claim(connection)
    event = finish.BeginAttempt(event="begin_attempt", task_id=task["id"], number=1, route=ROUTE)
    finish.record(connection, event)
    finish.record(connection, event)
    assert connection.execute("SELECT count(*) FROM homelab_task_attempts").fetchone() == (1,)
