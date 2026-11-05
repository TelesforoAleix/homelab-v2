"""The task tables: applying their schema and writing a submitted file."""

from __future__ import annotations

from graphlib import TopologicalSorter
from pathlib import Path
from typing import Any

QUEUE = "tasks"
JOB = "run_coding_task"


class Refused(Exception):
    """Input that will not be accepted. The message carries no task content."""


def apply_schema(connection: Any) -> None:
    connection.execute(Path(__file__).with_name("schema.sql").read_text())


def earlier_tasks(connection: Any, plan: Any, positions: set[int]) -> dict[int, tuple[int, str]]:
    """Id and status of already submitted dependencies, or refuse if one does not exist.

    The share lock holds each row until commit. Whatever marks a dependency done therefore
    runs after this commit and sees the tasks waiting for it, so none is left without a job.
    """
    if not positions:
        return {}
    rows = connection.execute(
        """SELECT position, id, status FROM homelab_tasks
           WHERE slice = %s AND repository = %s AND branch = %s AND position = ANY(%s)
           FOR SHARE""",
        (plan.slice, plan.repository, plan.branch, sorted(positions)),
    ).fetchall()
    found = {position: (task_id, status) for position, task_id, status in rows}
    unknown = sorted(positions - found.keys())
    if unknown:
        raise Refused(f"unknown dependency: position {unknown[0]}")
    return found


def write_tasks(connection: Any, plan: Any) -> tuple[dict[int, int], list[tuple[int, int]]]:
    """Insert a file's tasks; return ids by position, and (id, priority) for those ready to run.

    The caller owns the transaction. Dependencies are written first so that each row can name
    their ids; validation has already ruled out a cycle.
    """
    tasks = {task.position: task for task in plan.tasks}
    graph = {position: task.depends_on for position, task in tasks.items()}
    earlier = earlier_tasks(
        connection, plan, {dep for deps in graph.values() for dep in deps} - tasks.keys()
    )
    ids: dict[int, int] = {}
    ready: list[tuple[int, int]] = []
    for position in TopologicalSorter(graph).static_order():
        task = tasks.get(position)
        if task is None:
            continue
        depends_on = [ids[d] if d in tasks else earlier[d][0] for d in task.depends_on]
        (ids[position],) = connection.execute(
            """INSERT INTO homelab_tasks
               (slice, repository, branch, base_commit, position, depends_on, title, goal,
                allowed_files, check_command, pinned_paths, priority, severity)
               VALUES (%s, %s, %s, %s, %s, %s::bigint[], %s, %s, %s::text[], %s, %s::text[], %s, %s)
               RETURNING id""",
            (
                plan.slice,
                plan.repository,
                plan.branch,
                plan.base_commit,
                position,
                depends_on,
                task.title,
                task.goal,
                task.allowed_files,
                task.check_command,
                task.pinned_paths,
                task.priority,
                task.severity,
            ),
        ).fetchone()
        if all(d in earlier and earlier[d][1] == "done" for d in task.depends_on):
            ready.append((ids[position], task.priority))
    return ids, ready
