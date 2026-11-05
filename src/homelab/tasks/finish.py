"""Record what the host runner did: `python -m homelab.tasks.finish`, one JSON event on stdin.

Every event can be sent twice: the runner repeats one when `docker exec` fails, and the first may
have landed. A refused event exits 1 and names a field, never its content.
"""

from __future__ import annotations

import json
import sys
from typing import Annotated, Any, Literal

import psycopg
from pydantic import Field, TypeAdapter, ValidationError, model_validator

from homelab.jobs.tasks import run_coding_task
from homelab.settings import get_settings
from homelab.tasks.store import Refused
from homelab.tasks.submit import Strict

Count = Annotated[int, Field(ge=0)]


class BeginAttempt(Strict):
    event: Literal["begin_attempt"]
    task_id: int
    number: int = Field(ge=1)
    route: dict[str, Any]


class Review(Strict):
    """One review of an attempt's change; it becomes one `review` trace."""

    result: Literal["pass", "fix", "failed"]
    steps: Count = 0
    fresh_tokens: Count = 0
    cached_tokens: Count = 0
    output_tokens: Count = 0
    seconds: float = Field(default=0, ge=0)


class EndAttempt(Strict):
    event: Literal["end_attempt"]
    task_id: int
    number: int = Field(ge=1)
    route: dict[str, Any]
    status: Literal["done", "failed"]
    reason: str | None = None
    run_id: str | None = None
    check_exit_code: int | None = None
    steps: Count = 0
    fresh_tokens: Count = 0
    cached_tokens: Count = 0
    output_tokens: Count = 0
    seconds: float = Field(default=0, ge=0)
    review_route: dict[str, Any] | None = None
    reviews: list[Review] = Field(default_factory=list, max_length=8)

    @model_validator(mode="after")
    def reviews_name_their_route(self) -> EndAttempt:
        if self.reviews and self.review_route is None:
            raise ValueError("reviews need a review_route")
        return self


class EndTask(Strict):
    event: Literal["end_task"]
    task_id: int
    job_id: int
    status: Literal["done", "escalated", "failed"]
    reason: str | None = None


Event = Annotated[BeginAttempt | EndAttempt | EndTask, Field(discriminator="event")]
EVENT: TypeAdapter[BeginAttempt | EndAttempt | EndTask] = TypeAdapter(Event)


def parse(raw: bytes) -> BeginAttempt | EndAttempt | EndTask:
    try:
        return EVENT.validate_json(raw)
    except ValidationError as error:
        first = error.errors(include_input=False, include_url=False, include_context=False)[0]
        where = ".".join(str(part) for part in first["loc"]) or "event"
        raise Refused(f"{where}: {first['msg']}") from None


def begin_attempt(connection: Any, event: BeginAttempt) -> None:
    connection.execute(
        """INSERT INTO homelab_task_attempts (task_id, number, role, route)
           VALUES (%s, %s, 'worker', %s::jsonb) ON CONFLICT DO NOTHING""",
        (event.task_id, event.number, json.dumps(event.route)),
    )


def end_attempt(connection: Any, event: EndAttempt) -> None:
    """Close the attempt and write its `lead` trace and one `review` trace per review.

    The traces ride on the close that changed a row, so a repeated event writes nothing twice.
    Cost stays empty: nothing measures it yet.
    """
    with connection.transaction():
        closed = connection.execute(
            """UPDATE homelab_task_attempts
               SET status = %s, reason = %s, run_id = %s, check_exit_code = %s, ended_at = now()
               WHERE task_id = %s AND number = %s AND status = 'running'""",
            (
                event.status,
                event.reason,
                event.run_id,
                event.check_exit_code,
                event.task_id,
                event.number,
            ),
        ).rowcount
        if closed:
            connection.execute(
                """INSERT INTO homelab_task_traces
                   (task_id, attempt, role, route, kind, steps, fresh_tokens, cached_tokens,
                    output_tokens, seconds, result)
                   VALUES (%s, %s, 'worker', %s::jsonb, 'lead', %s, %s, %s, %s, %s, %s)""",
                (
                    event.task_id,
                    event.number,
                    json.dumps(event.route),
                    event.steps,
                    event.fresh_tokens,
                    event.cached_tokens,
                    event.output_tokens,
                    event.seconds,
                    event.reason or event.status,
                ),
            )
            for review in event.reviews:
                connection.execute(
                    """INSERT INTO homelab_task_traces
                       (task_id, attempt, role, route, kind, steps, fresh_tokens, cached_tokens,
                        output_tokens, seconds, result)
                       VALUES (%s, %s, 'ticket-reviewer', %s::jsonb, 'review', %s, %s, %s, %s, %s,
                               %s)""",
                    (
                        event.task_id,
                        event.number,
                        json.dumps(event.review_route),
                        review.steps,
                        review.fresh_tokens,
                        review.cached_tokens,
                        review.output_tokens,
                        review.seconds,
                        review.result,
                    ),
                )


def end_task(connection: Any, event: EndTask) -> list[int]:
    """Close the task and its job; a `done` task frees the dependents with nothing else unmet.

    The update waits for any submission holding a share lock on this row, so the select after it
    sees every task that submission wrote. Dependents of any other end simply get no job.
    """
    released = []
    with connection.transaction():
        closed = connection.execute(
            """UPDATE homelab_tasks
               SET status = %s, reason = %s, finished_at = now(), updated_at = now()
               WHERE id = %s AND status = 'running'""",
            (event.status, event.reason, event.task_id),
        ).rowcount
        if not closed:
            return released
        connection.execute(
            "SELECT procrastinate_finish_job_v1(%s, %s::procrastinate_job_status, false)",
            (event.job_id, "succeeded" if event.status == "done" else "failed"),
        )
        if event.status != "done":
            return released
        rows = connection.execute(
            """SELECT task.id, task.priority FROM homelab_tasks task
               WHERE task.status = 'queued' AND %s = ANY(task.depends_on)
                 AND NOT EXISTS (SELECT 1 FROM homelab_tasks dependency
                                 WHERE dependency.id = ANY(task.depends_on)
                                   AND dependency.status <> 'done')
               ORDER BY task.position""",
            (event.task_id,),
        ).fetchall()
        for task_id, priority in rows:
            run_coding_task.configure(connection=connection, priority=priority).defer(
                task_id=task_id
            )
            released.append(task_id)
    return released


def record(connection: Any, event: BeginAttempt | EndAttempt | EndTask) -> list[int]:
    """Apply one event; returns the ids of the tasks it released."""
    if isinstance(event, EndTask):
        return end_task(connection, event)
    if isinstance(event, BeginAttempt):
        begin_attempt(connection, event)
    else:
        end_attempt(connection, event)
    return []


def main() -> int:
    try:
        event = parse(sys.stdin.buffer.read())
        with psycopg.connect(get_settings().database_url) as connection:
            released = record(connection, event)
    except Refused as error:
        print(f"homelab.tasks.finish: {error}", file=sys.stderr)
        return 1
    print(json.dumps(released))
    return 0


if __name__ == "__main__":
    sys.exit(main())
