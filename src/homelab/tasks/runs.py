"""Transactional run records and the adapter to Procrastinate's existing task queue."""

from __future__ import annotations

import hashlib
import json
from uuid import UUID, uuid4

from psycopg.rows import dict_row
from psycopg.types.json import Jsonb

from homelab.tasks.executors import Executor
from homelab.tasks.store import RunError

ACTIVE = {"queued", "running", "question", "waiting"}


def lock(connection) -> None:
    connection.execute("SELECT pg_advisory_xact_lock(157441, 1)")


def lock_task(connection, task_id: int) -> bool:
    linked = connection.execute(
        "SELECT 1 FROM homelab_runs WHERE task_id = %s", (task_id,)
    ).fetchone()
    if linked:
        lock(connection)
    return bool(linked)


def row(connection, application: str, run_id: UUID):
    with connection.cursor(row_factory=dict_row) as cursor:
        return cursor.execute(
            "SELECT * FROM homelab_runs WHERE application = %s AND id = %s",
            (application, run_id),
        ).fetchone()


def by_request(connection, application: str, request_id: str):
    with connection.cursor(row_factory=dict_row) as cursor:
        return cursor.execute(
            "SELECT * FROM homelab_runs WHERE application = %s AND request_id = %s",
            (application, request_id),
        ).fetchone()


def fingerprint(body) -> str:
    return hashlib.sha256(
        json.dumps(body.model_dump(), sort_keys=True, separators=(",", ":")).encode()
    ).hexdigest()


def available(body, executor: Executor) -> bool:
    """Do not queue a writer for an execution policy today's host cannot honor."""
    return body.eval is not None or executor.configuration == "pi/base"


def create(
    connection,
    application: str,
    body,
    executor: Executor,
    *,
    configure_job,
    repositories: dict[str, str],
    legacy_attempt: str | None = None,
    title: str | None = None,
):
    """One transaction owns deduplication, branch exclusion, dependencies, task and job."""
    with connection.transaction():
        lock(connection)
        existing = by_request(connection, application, body.request_id)
        digest = fingerprint(body)
        if existing:
            if not legacy_attempt and existing["request_hash"] != digest:
                raise RunError(409, "request_conflict")
            return existing, False
        if legacy_attempt and application == "factory":
            previous = connection.execute(
                "SELECT id FROM homelab_tasks WHERE slice = %s AND position = 1",
                (f"eval:{legacy_attempt}",),
            ).fetchone()
            if previous:
                return {"task_id": previous[0]}, False
        default = repositories.get(body.repository)
        if default is None:
            raise RunError(403, "repository_not_allowed")
        if body.mode == "write" and body.branch in {"main", default}:
            raise RunError(409, "branch_is_default")
        if body.role != "ticket" and body.eval is None:
            raise RunError(409, "role_not_available")
        predecessors = []
        dependencies = []
        for request_id in body.after:
            parent = by_request(connection, application, request_id)
            if (
                parent is None
                or parent["request"]["repository"] != body.repository
                or parent["request"]["branch"] != body.branch
            ):
                raise RunError(422, "invalid_predecessor")
            predecessors.append(parent)
            dependencies.append(parent["task_id"])
        active = connection.execute(
            """SELECT id FROM homelab_runs WHERE application = %s
               AND request->>'repository' = %s AND request->>'branch' = %s
               AND status IN ('queued', 'running', 'question', 'waiting')""",
            (application, body.repository, body.branch),
        ).fetchall()
        # Naming the last member of a chain also names its transitive predecessors.
        ancestors = set()
        pending = [p["id"] for p in predecessors]
        while pending:
            parent_id = pending.pop()
            if parent_id in ancestors:
                continue
            ancestors.add(parent_id)
            parent = row(connection, application, parent_id)
            pending.extend(parent["predecessors"])
        if not legacy_attempt and any(run_id not in ancestors for (run_id,) in active):
            raise RunError(409, "branch_busy")
        run_id = uuid4()
        request = body.model_dump()
        del request["work"]["goal"]
        cancelled = any(p["status"] not in ACTIVE | {"done"} for p in predecessors)
        usable = True if legacy_attempt else available(body, executor)
        status = "cancelled" if cancelled else "queued" if usable else "failed"
        reason = "predecessor" if cancelled else None if usable else "no_executor_available"
        connection.execute(
            """INSERT INTO homelab_runs
               (id, application, request_id, request_hash, goal, request, executor,
                configuration_hash, predecessors, status, status_reason, finished_at)
               VALUES (%s, %s, %s, %s, %s, %s, %s, %s, %s::uuid[], %s, %s,
                       CASE WHEN %s THEN clock_timestamp() END)""",
            (
                run_id,
                application,
                body.request_id,
                digest,
                body.work.goal,
                Jsonb(request),
                Jsonb(executor.selection()),
                executor.fingerprint(),
                [p["id"] for p in predecessors],
                status,
                reason,
                status != "queued",
            ),
        )
        # The host's task protocol has no optional check; true is the neutral check.
        # An empty allowed list means the sandbox's unrestricted default.
        task_id = connection.execute(
            """INSERT INTO homelab_tasks
               (slice, repository, branch, base_commit, position, depends_on, title, goal,
                allowed_files, check_command, pinned_paths, priority,
                eval_set, eval_case, eval_configuration, status, reason, finished_at)
               VALUES (%s, %s, %s, %s, 1, %s::bigint[], %s, '', %s::text[], %s,
                       %s::text[], %s, %s, %s, %s, %s, %s,
                       CASE WHEN %s THEN clock_timestamp() END) RETURNING id""",
            (
                f"eval:{application}:{legacy_attempt}" if legacy_attempt else f"run:{run_id}",
                body.repository,
                body.branch or default,
                body.base_commit,
                dependencies,
                title or f"Run {body.request_id}",
                body.work.allowed_files or [],
                body.work.check_command or "true",
                body.work.pinned_paths,
                body.priority,
                body.eval.set if body.eval else None,
                body.eval.case if body.eval else None,
                body.eval.configuration.removeprefix("pi/") if body.eval else None,
                "queued" if status == "queued" else "failed",
                reason,
                status != "queued",
            ),
        ).fetchone()[0]
        connection.execute("UPDATE homelab_runs SET task_id = %s WHERE id = %s", (task_id, run_id))
        if status == "queued" and all(p["status"] == "done" for p in predecessors):
            configure_job(connection=connection, priority=body.priority).defer(task_id=task_id)
        return row(connection, application, run_id), True


def cancel(connection, application: str, run_id: UUID):
    with connection.transaction():
        lock(connection)
        record = row(connection, application, run_id)
        if record is None:
            raise RunError(404, "run_not_found")
        if record["status"] == "running":
            # Today's host protocol has no interrupt operation. Never report a live
            # writer as cancelled while it could still push.
            raise RunError(409, "not_resumable")
        if record["status"] not in ACTIVE:
            return record
        connection.execute(
            """WITH RECURSIVE descendants AS (
                   SELECT id FROM homelab_runs WHERE id = %s
                   UNION SELECT r.id FROM homelab_runs r JOIN descendants d
                       ON d.id = ANY(r.predecessors)
               )
               UPDATE homelab_runs SET status = 'cancelled',
                   status_reason = CASE WHEN id = %s THEN 'cancelled_by_client'
                                        ELSE 'predecessor' END,
                   finished_at = clock_timestamp()
               WHERE id IN (SELECT id FROM descendants) AND status = 'queued'""",
            (run_id, run_id),
        )
        connection.execute(
            """UPDATE homelab_tasks t SET status = 'failed', reason = r.status_reason,
                   updated_at = clock_timestamp(), finished_at = clock_timestamp()
               FROM homelab_runs r WHERE t.id = r.task_id AND r.status = 'cancelled'
                   AND r.application = %s AND t.status = 'queued'""",
            (application,),
        )
        return row(connection, application, run_id)


def claimed_goal(connection, task_id: int) -> str | None:
    record = connection.execute(
        "SELECT goal, request FROM homelab_runs WHERE task_id = %s", (task_id,)
    ).fetchone()
    if record is None:
        return None
    goal, request = record
    if request["assets"]:
        goal += "\n\nSupplied assets (JSON):\n" + json.dumps(request["assets"], ensure_ascii=False)
    return goal
