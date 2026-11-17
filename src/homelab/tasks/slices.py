"""Transactional whole-run records. Procrastinate owns scheduling and repository exclusion."""

from datetime import UTC, datetime, timedelta
from uuid import uuid4

import procrastinate
from psycopg.rows import dict_row
from psycopg.types.json import Jsonb

from homelab.tasks import runs
from homelab.tasks.executors import Executor
from homelab.tasks.store import JOB, RunError

app = procrastinate.App(connector=procrastinate.PsycopgConnector())


def enqueue(connection, task_id, repository, priority, *, at=None):
    return app.configure_task(
        JOB,
        connection=connection,
        queue=runs.HARNESS_QUEUE,
        lock="runs:" + repository,
        priority=priority,
        schedule_at=at,
    ).defer(task_id=task_id)


def claim(connection):
    with connection.transaction():
        runs.lock(connection)
        while True:
            job_id, args = connection.execute(
                "SELECT id, args FROM procrastinate_fetch_job_v2(%s::varchar[], NULL)",
                ([runs.HARNESS_QUEUE],),
            ).fetchone()
            if job_id is None:
                return None
            with connection.cursor(row_factory=dict_row) as cursor:
                saved = cursor.execute(
                    "SELECT * FROM homelab_runs WHERE task_id = %s FOR UPDATE", (args["task_id"],)
                ).fetchone()
            if (
                saved is None
                or saved["status"] not in {"queued", "waiting"}
                or (saved["status"] == "waiting" and saved["status_reason"] != "usage_limit")
            ):
                finish_job(connection, job_id, "aborted")
                continue
            connection.execute(
                """UPDATE homelab_runs SET status = 'running',
                resume_at = NULL, finished_at = NULL, resumable = false,
                started_at = coalesce(started_at, clock_timestamp()) WHERE id = %s""",
                (saved["id"],),
            )
            saved.update(job_id=job_id)
            return saved


def begin(connection, saved, selected):
    executor = selected if isinstance(selected, Executor) else Executor(**selected)
    with connection.transaction():
        runs.lock(connection)
        number = connection.execute(
            """SELECT coalesce(max(number), 0) + 1
            FROM homelab_task_attempts WHERE task_id = %s""",
            (saved["task_id"],),
        ).fetchone()[0]
        turn_id = "turn-" + uuid4().hex
        connection.execute(
            """INSERT INTO homelab_task_attempts
            (task_id, number, role, route, run_id, configuration_hash)
            VALUES (%s, %s, %s, %s, %s, %s)""",
            (
                saved["task_id"],
                number,
                saved["request"]["role"],
                Jsonb(executor.selection()),
                turn_id,
                executor.fingerprint(),
            ),
        )
        connection.execute(
            """UPDATE homelab_runs SET executor = %s, configuration_hash = %s,
            harness_started = true WHERE id = %s""",
            (Jsonb(executor.selection()), executor.fingerprint(), saved["id"]),
        )
        saved.update(number=number, turn_id=turn_id, executor=executor.selection())


def prepared(connection, run_id, work_id, *, ready=True):
    with connection.transaction():
        runs.lock(connection)
        connection.execute(
            "UPDATE homelab_runs SET work_id = %s, copy_ready = %s WHERE id = %s",
            (work_id, ready, run_id),
        )


def prompt(connection, saved):
    answer = connection.execute(
        """SELECT text FROM homelab_run_messages
        WHERE run_id = %s AND NOT consumed ORDER BY created_at LIMIT 1""",
        (saved["id"],),
    ).fetchone()
    if answer:
        return answer[0]
    if saved["session_id"]:
        return "Continue with the run."
    return saved["goal"]


def finish_job(connection, job_id, status):
    connection.execute(
        "SELECT procrastinate_finish_job_v1(%s, %s::procrastinate_job_status, false)",
        (job_id, status),
    )


def defer_usage(connection, saved, reset):
    with connection.transaction():
        runs.lock(connection)
        connection.execute(
            """UPDATE homelab_runs SET status = 'waiting', status_reason = 'usage_limit',
            resume_at = %s, resumable = (session_id IS NOT NULL AND NOT EXISTS
                (SELECT 1 FROM homelab_run_messages
                 WHERE run_id = homelab_runs.id AND NOT consumed))
            WHERE id = %s""",
            (reset, saved["id"]),
        )
        finish_job(connection, saved["job_id"], "succeeded")
        enqueue(
            connection,
            saved["task_id"],
            saved["request"]["repository"],
            saved["request"]["priority"],
            at=reset,
        )


def complete(connection, saved, result, session, commits, metrics):
    with connection.transaction():
        runs.lock(connection)
        status, reason = result["status"], result.get("status_reason")
        resumable = result.get("resumable", False)
        connection.execute(
            """UPDATE homelab_task_attempts SET status = %s, reason = %s,
            ended_at = clock_timestamp() WHERE task_id = %s AND number = %s""",
            (
                "failed" if status == "failed" or reason == "usage_limit" else "done",
                reason,
                saved["task_id"],
                saved["number"],
            ),
        )
        if metrics:
            connection.execute(
                """INSERT INTO homelab_task_traces
                (task_id, attempt, role, route, kind, steps, fresh_tokens, cached_tokens,
                 output_tokens, cost_usd, seconds, result)
                VALUES (%s, %s, %s, %s, 'lead', %s, %s, %s, %s, %s, %s, %s)""",
                (
                    saved["task_id"],
                    saved["number"],
                    saved["request"]["role"],
                    Jsonb(saved["executor"]),
                    metrics.get("requests", 0),
                    metrics.get("fresh", 0),
                    metrics.get("cached", 0),
                    metrics.get("output", 0),
                    metrics.get("usd"),
                    metrics.get("seconds", 0),
                    status,
                ),
            )
        connection.execute(
            """UPDATE homelab_runs SET status = %s, status_reason = %s,
            resumable = %s, session_id = %s, final_message = %s, resume_at = %s,
            commits = ARRAY(SELECT DISTINCT unnest(commits || %s::text[])),
            finished_at = clock_timestamp() WHERE id = %s""",
            (
                status,
                reason,
                resumable,
                session,
                result.get("final_message"),
                result.get("resume_at"),
                commits,
                saved["id"],
            ),
        )
        connection.execute(
            "UPDATE homelab_run_messages SET consumed = true WHERE run_id = %s", (saved["id"],)
        )
        task_status = "done" if status == "done" else "failed" if status == "failed" else "queued"
        connection.execute(
            """UPDATE homelab_tasks SET status = %s, reason = %s,
            updated_at = clock_timestamp(), finished_at = clock_timestamp() WHERE id = %s""",
            (task_status, reason, saved["task_id"]),
        )
        finish_job(connection, saved["job_id"], "failed" if status == "failed" else "succeeded")
        if reason == "usage_limit":
            enqueue(
                connection,
                saved["task_id"],
                saved["request"]["repository"],
                saved["request"]["priority"],
                at=result["resume_at"],
            )
        elif status == "done":
            release(connection, saved["task_id"])
        elif status == "failed":
            cancel_descendants(connection, saved["id"])


def cancel_descendants(connection, run_id):
    connection.execute(
        """WITH RECURSIVE descendants AS (
        SELECT id FROM homelab_runs WHERE %s = ANY(predecessors)
        UNION SELECT r.id FROM homelab_runs r JOIN descendants d ON d.id = ANY(r.predecessors))
        UPDATE homelab_runs SET status = 'cancelled', status_reason = 'predecessor',
            resumable = false, finished_at = clock_timestamp()
        WHERE id IN (SELECT id FROM descendants) AND status = 'queued'""",
        (run_id,),
    )
    connection.execute("""UPDATE homelab_tasks SET status = 'failed', reason = 'predecessor',
        updated_at = clock_timestamp(), finished_at = clock_timestamp()
        WHERE status = 'queued' AND id IN (SELECT task_id FROM homelab_runs
            WHERE status = 'cancelled' AND status_reason = 'predecessor')""")


def release(connection, task_id):
    rows = connection.execute(
        """SELECT t.id, t.priority FROM homelab_tasks t
        WHERE t.status = 'queued' AND %s = ANY(t.depends_on)
        AND NOT EXISTS (SELECT 1 FROM homelab_tasks p WHERE p.id = ANY(t.depends_on)
                        AND p.status <> 'done')
        AND NOT EXISTS (SELECT 1 FROM procrastinate_jobs j WHERE (j.args->>'task_id')::bigint = t.id
                        AND j.status IN ('todo', 'doing')) ORDER BY t.position""",
        (task_id,),
    ).fetchall()
    for child, priority in rows:
        release_task(connection, child, priority)


def release_task(connection, task_id, priority):
    saved = connection.execute(
        "SELECT request FROM homelab_runs WHERE task_id = %s", (task_id,)
    ).fetchone()
    if saved and runs.is_harness(saved[0]):
        enqueue(connection, task_id, saved[0]["repository"], priority)
    else:
        from homelab.jobs.tasks import run_coding_task

        run_coding_task.configure(connection=connection, priority=priority).defer(task_id=task_id)


def message(connection, application, run_id, body):
    with connection.transaction():
        runs.lock(connection)
        saved = runs.row(connection, application, run_id)
        if saved is None:
            raise RunError(404, "run_not_found")
        if connection.execute(
            "SELECT 1 FROM homelab_run_messages WHERE run_id = %s AND message_id = %s",
            (run_id, body.message_id),
        ).fetchone():
            return saved
        if not (
            saved["resumable"]
            or (
                saved["session_id"]
                and saved["work_id"] is None
                and not connection.execute(
                    "SELECT 1 FROM homelab_run_messages WHERE run_id = %s AND NOT consumed",
                    (run_id,),
                ).fetchone()
            )
        ) or saved["status"] not in {
            "question",
            "waiting",
            "done",
            "escalated",
        }:
            raise RunError(409, "not_resumable")
        # A completed branch may have a queued/running successor by now.
        if (
            saved["status"] == "done"
            and connection.execute(
                """SELECT 1 FROM homelab_runs
            WHERE id <> %s AND application = %s AND request->>'repository' = %s
            AND request->>'branch' IS NOT DISTINCT FROM %s
            AND (status IN ('queued', 'running', 'question', 'waiting') OR
                 (status = 'escalated' AND (resumable OR session_id IS NOT NULL)))""",
                (run_id, application, saved["request"]["repository"], saved["request"]["branch"]),
            ).fetchone()
        ):
            raise RunError(409, "branch_busy")
        connection.execute(
            "INSERT INTO homelab_run_messages(run_id, message_id, text) VALUES (%s,%s,%s)",
            (run_id, body.message_id, body.text),
        )
        if saved["status_reason"] == "usage_limit":
            # The already scheduled job applies the answer after reset. Never create a
            # second job or let a message bypass the subscription window.
            connection.execute("UPDATE homelab_runs SET resumable = false WHERE id = %s", (run_id,))
            return runs.row(connection, application, run_id)
        connection.execute(
            """UPDATE homelab_runs SET status = 'queued', status_reason = NULL,
            resumable = false, finished_at = NULL WHERE id = %s""",
            (run_id,),
        )
        connection.execute(
            "UPDATE homelab_tasks SET status = 'queued', reason = NULL WHERE id = %s",
            (saved["task_id"],),
        )
        enqueue(
            connection,
            saved["task_id"],
            saved["request"]["repository"],
            saved["request"]["priority"],
        )
        return runs.row(connection, application, run_id)


def recover(connection):
    """Caller has removed its launcher containers before ending interrupted attempts."""
    from homelab.runs.slices import interrupted_state

    count = 0
    with connection.transaction():
        runs.lock(connection)
        jobs = connection.execute(
            """SELECT id, args FROM procrastinate_jobs
            WHERE queue_name = %s AND status = 'doing'""",
            (runs.HARNESS_QUEUE,),
        ).fetchall()
        for job_id, args in jobs:
            with connection.cursor(row_factory=dict_row) as cursor:
                saved = cursor.execute(
                    "SELECT * FROM homelab_runs WHERE task_id = %s", (args["task_id"],)
                ).fetchone()
            if saved is None:
                finish_job(connection, job_id, "aborted")
                continue
            result = interrupted_state(saved["interruptions"])
            connection.execute(
                """UPDATE homelab_task_attempts SET status = 'failed', reason = 'interrupted',
                ended_at = clock_timestamp() WHERE task_id = %s AND status = 'running'""",
                (saved["task_id"],),
            )
            connection.execute(
                """UPDATE homelab_runs SET status = %s, status_reason = 'interrupted',
                resumable = false, interruptions = interruptions + 1,
                finished_at = CASE WHEN %s = 'failed' THEN clock_timestamp() END WHERE id = %s""",
                (result["status"], result["status"], saved["id"]),
            )
            finish_job(connection, job_id, "failed")
            if result["status"] == "queued":
                enqueue(
                    connection,
                    saved["task_id"],
                    saved["request"]["repository"],
                    saved["request"]["priority"],
                )
            else:
                connection.execute(
                    """UPDATE homelab_tasks SET status = 'failed', reason = 'interrupted',
                    updated_at = clock_timestamp(), finished_at = clock_timestamp()
                    WHERE id = %s""",
                    (saved["task_id"],),
                )
                cancel_descendants(connection, saved["id"])
            count += 1
    return count


def sweep(connection, *, now=None, failed_only=False):
    """Remove ended copies under the claim/message lock; busy folders retry later."""
    from homelab.runs import launcher

    now = now or datetime.now(UTC)
    removed = 0
    with connection.transaction():
        runs.lock(connection)
        candidates = connection.execute(
            """SELECT id, work_id, status,
                coalesce((SELECT max(ended_at) FROM homelab_task_attempts
                          WHERE task_id = homelab_runs.task_id), finished_at)
            FROM homelab_runs
            WHERE work_id IS NOT NULL AND status NOT IN ('queued', 'running')
                AND (NOT %s OR status IN ('failed', 'cancelled'))""",
            (failed_only,),
        ).fetchall()
        for run_id, work_id, status, ended in candidates:
            if status in {"queued", "running"}:
                continue
            if status not in {"failed", "cancelled"} and (
                ended is None or ended > now - timedelta(days=3)
            ):
                continue
            try:
                launcher.request({"op": "cli-remove", "work_id": work_id})
            except launcher.Refused:
                continue
            connection.execute(
                """UPDATE homelab_runs SET work_id = NULL, copy_ready = false,
                resumable = false WHERE id = %s""",
                (run_id,),
            )
            removed += 1
    return removed
