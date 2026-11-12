"""Hand the host runner its next task: `python -m homelab.tasks.claim`.

Prints the claimed task as JSON, or `null` when the queue is empty. With `--recover`, returns what
a dead runner left `running` to the queue instead. The runner reaches this through `docker exec`,
because Postgres has no host port and no container has Docker.
"""

from __future__ import annotations

import json
import sys
from typing import Any

import psycopg

from homelab.settings import get_settings
from homelab.tasks.store import QUEUE

FIELDS = (
    "id",
    "slice",
    "repository",
    "branch",
    "title",
    "goal",
    "allowed_files",
    "check_command",
    "pinned_paths",
    "eval_set",
    "eval_case",
    "eval_configuration",
)


def claim(connection: Any) -> dict[str, Any] | None:
    """Take the next runnable job and its task, in one transaction.

    Procrastinate's own function moves the job to `doing` under SKIP LOCKED, so a job goes to one
    caller only. It is called in SQL, not through the job manager, to share the transaction with
    the task's row: there is never a `doing` job with a `queued` task, or the reverse.
    """
    with connection.transaction():
        while True:
            job_id, args = connection.execute(
                "SELECT id, args FROM procrastinate_fetch_job_v2(%s::varchar[], NULL)", ([QUEUE],)
            ).fetchone()
            if job_id is None:
                return None
            row = connection.execute(
                f"SELECT status, {', '.join(FIELDS)} FROM homelab_tasks WHERE id = %s FOR UPDATE",
                (args["task_id"],),
            ).fetchone()
            if row is None or row[0] != "queued" or slice_stopped(connection, row[2]):
                # The task keeps its status; a task of a stopped slice stays queued with no job.
                connection.execute(
                    "SELECT procrastinate_finish_job_v1(%s, 'aborted', false)", (job_id,)
                )
                continue
            task = dict(zip(FIELDS, row[1:], strict=True))
            connection.execute(
                """UPDATE homelab_tasks SET status = 'running', started_at = now(),
                   updated_at = now() WHERE id = %s""",
                (task["id"],),
            )
            roles = dict(
                connection.execute(
                    """SELECT role, routes FROM homelab_roles
                       WHERE role IN ('worker', 'ticket-reviewer')"""
                ).fetchall()
            )
            (attempts,) = connection.execute(
                "SELECT coalesce(max(number), 0) FROM homelab_task_attempts WHERE task_id = %s",
                (task["id"],),
            ).fetchone()
            return {
                **task,
                "job_id": job_id,
                "routes": roles.get("worker", []),
                "review_routes": roles.get("ticket-reviewer", []),
                "next_attempt": attempts + 1,
            }


def slice_stopped(connection: Any, name: str) -> bool:
    return (
        connection.execute(
            """SELECT 1 FROM homelab_tasks
               WHERE slice = %s AND status IN ('escalated', 'failed') LIMIT 1""",
            (name,),
        ).fetchone()
        is not None
    )


def recover(connection: Any) -> int:
    """Return everything a dead runner held to the queue; only a starting runner calls this."""
    with connection.transaction():
        jobs = connection.execute(
            "SELECT id FROM procrastinate_jobs WHERE queue_name = %s AND status = 'doing'",
            (QUEUE,),
        ).fetchall()
        for (job_id,) in jobs:
            connection.execute(
                "SELECT procrastinate_retry_job_v2(%s, now(), NULL, NULL, NULL)", (job_id,)
            )
        connection.execute(
            """UPDATE homelab_task_attempts
               SET status = 'failed', reason = 'runner_died', ended_at = now()
               WHERE status = 'running'"""
        )
        connection.execute(
            """UPDATE homelab_tasks SET status = 'queued', started_at = NULL, updated_at = now()
               WHERE status = 'running'"""
        )
    return len(jobs)


def main(argv: list[str] | None = None) -> int:
    args = sys.argv[1:] if argv is None else argv
    if args not in ([], ["--recover"]):
        print("usage: python -m homelab.tasks.claim [--recover]", file=sys.stderr)
        return 2
    with psycopg.connect(get_settings().database_url) as connection:
        result = recover(connection) if args else claim(connection)
    print(json.dumps(result))
    return 0


if __name__ == "__main__":
    sys.exit(main())
