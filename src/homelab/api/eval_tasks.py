"""Keyed submission and observations of eval tasks; content is never logged."""

from __future__ import annotations

import base64
import hashlib
import re
from pathlib import Path

import psycopg
from fastapi import APIRouter, Request
from fastapi.encoders import jsonable_encoder
from fastapi.responses import JSONResponse
from pydantic import ValidationError
from starlette.concurrency import run_in_threadpool

from homelab.api import runs
from homelab.api.run_contract import RunRequest, Work
from homelab.api.run_keys import ApplicationKey, RunError
from homelab.api.run_keys import authorize as run_authorize
from homelab.settings import get_settings
from homelab.tasks.store import Refused
from homelab.tasks.submit import EvalMark, EvalName, Strict, TaskEntry, TaskFile

router = APIRouter()
DIFF_LIMIT = 2 * 1024 * 1024


class EvalSubmission(Strict):
    attempt: EvalName
    repository: str
    branch: str
    base_commit: str
    title: str
    goal: str
    allowed_files: list[str]
    check_command: str
    pinned_paths: list[str]
    eval: EvalMark


def authorize(request: Request, *scopes):
    try:
        return run_authorize(request, *scopes, "runs:eval")
    except RunError as error:
        message = {
            503: "Eval tasks are not configured",
            401: "Eval task key required",
            403: "Eval task scope required",
        }.get(error.status, error.code)
        return JSONResponse({"error": message}, status_code=error.status)


def parse_submission(raw: bytes) -> TaskFile:
    try:
        body = EvalSubmission.model_validate_json(raw)
        fields = body.model_dump(exclude={"attempt", "repository", "branch", "base_commit"})
        return TaskFile(
            slice=f"eval:{body.attempt}",
            repository=body.repository,
            branch=body.branch,
            base_commit=body.base_commit,
            tasks=[TaskEntry(position=1, **fields)],
        )
    except ValidationError as error:
        first = error.errors(include_input=False, include_url=False, include_context=False)[0]
        # Even an unknown field name is caller content. Only known locations reach the client.
        known = set(EvalSubmission.model_fields) | set(EvalMark.model_fields)
        where = ".".join(str(p) for p in first["loc"] if p in known or isinstance(p, int))
        raise Refused(f"{where or 'body'}: {first['msg']}") from None


def create_task(plan: TaskFile, identity: ApplicationKey) -> JSONResponse:
    attempt = plan.slice.removeprefix("eval:")
    task = plan.tasks[0]
    # The plan has already passed the historical alias validation. Construct the
    # internal adapter without imposing new v1 constraints on existing clients.
    body = RunRequest.model_construct(
        contract=1,
        request_id="eval:" + hashlib.sha256(attempt.encode()).hexdigest()[:59],
        attribution={"eval_attempt": attempt},
        repository=plan.repository,
        branch=plan.branch,
        base_commit=plan.base_commit,
        mode="write",
        role="ticket",
        level="standard",
        work=Work.model_construct(
            goal=task.goal,
            allowed_files=task.allowed_files,
            check_command=task.check_command,
            pinned_paths=task.pinned_paths,
        ),
        eval=task.eval,
    )
    record, created = runs.create(identity, body, legacy_attempt=attempt, title=task.title)
    if created:
        return JSONResponse({"task": record["task_id"]}, status_code=201)
    return JSONResponse(
        {"error": "Attempt already submitted", "task": record["task_id"]}, status_code=409
    )


@router.post("/v1/tasks/eval")
async def post_eval(request: Request):
    identity = authorize(request, "runs:write")
    if isinstance(identity, JSONResponse):
        return identity
    try:
        plan = parse_submission(await runs.request_bytes(request))
        return await run_in_threadpool(create_task, plan, identity)
    except RunError as error:
        return runs.failure(error)
    except Refused as error:
        return JSONResponse({"error": str(error)}, status_code=422)


def read_diff(root: Path, run_id: str | None) -> dict:
    if not run_id or not re.fullmatch(r"[A-Za-z0-9][A-Za-z0-9._-]{0,63}", run_id):
        return {"diff_base64": None}
    path = root / f"{run_id}.patch"
    try:
        if root.is_symlink() or path.is_symlink() or not path.is_file():
            return {"diff_base64": None}
        with path.open("rb") as stream:
            data = stream.read(DIFF_LIMIT + 1)
        if len(data) > DIFF_LIMIT:
            return {"diff_base64": None, "diff_too_large": True}
        return {"diff_base64": base64.b64encode(data).decode("ascii")}
    except OSError:
        return {"diff_base64": None}


@router.get("/v1/tasks/eval/{task_id}")
def get_eval(task_id: str, request: Request):
    identity = authorize(request, "runs:read")
    if isinstance(identity, JSONResponse):
        return identity
    if not re.fullmatch(r"[0-9]{1,19}", task_id) or not 0 < int(task_id) < 2**63:
        return JSONResponse({"error": "Task not found"}, status_code=404)
    with psycopg.connect(get_settings().database_url) as connection:
        task = connection.execute(
            """SELECT t.id, t.status, t.reason FROM homelab_tasks t
               LEFT JOIN homelab_runs r ON r.task_id = t.id
               WHERE t.id = %s AND t.eval_set IS NOT NULL
                 AND (r.application = %s OR
                      (r.id IS NULL AND %s = 'factory' AND t.slice LIKE 'eval:%%'))""",
            (int(task_id), identity.application, identity.application),
        ).fetchone()
        if task is None:
            return JSONResponse({"error": "Task not found"}, status_code=404)
        rows = connection.execute(
            """SELECT a.number, a.status, a.reason, a.started_at, a.ended_at,
                      a.configuration_hash, a.run_id,
                      sum(t.steps), sum(t.fresh_tokens), sum(t.cached_tokens),
                      sum(t.output_tokens),
                      CASE WHEN count(t.cost_usd) = count(t.id) THEN sum(t.cost_usd) END
               FROM homelab_task_attempts a
               LEFT JOIN homelab_task_traces t ON t.task_id = a.task_id AND t.attempt = a.number
               WHERE a.task_id = %s
               GROUP BY a.task_id, a.number ORDER BY a.number""",
            (int(task_id),),
        ).fetchall()
    attempts = [
        dict(
            zip(
                ("number", "status", "reason", "started_at", "ended_at", "configuration_hash"),
                row[:6],
                strict=True,
            ),
            usage=dict(
                zip(
                    ("steps", "fresh_tokens", "cached_tokens", "output_tokens", "cost_usd"),
                    row[7:],
                    strict=True,
                )
            ),
        )
        for row in rows
    ]
    last = next((row for row in reversed(rows) if row[4] is not None), None)
    diff = read_diff(get_settings().eval_diffs_dir, last[6] if last else None)
    return JSONResponse(
        jsonable_encoder(
            {"task": task[0], "status": task[1], "reason": task[2], "attempts": attempts, **diff}
        )
    )
