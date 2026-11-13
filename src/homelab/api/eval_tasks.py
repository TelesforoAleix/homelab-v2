"""Keyed submission and observations of eval tasks; content is never logged."""

from __future__ import annotations

import base64
import hmac
import re
from pathlib import Path

import psycopg
from fastapi import APIRouter, Request
from fastapi.encoders import jsonable_encoder
from fastapi.responses import JSONResponse
from pydantic import ValidationError
from starlette.concurrency import run_in_threadpool

from homelab.settings import get_settings
from homelab.tasks.store import Refused
from homelab.tasks.submit import EvalMark, EvalName, Strict, TaskEntry, TaskFile, submit

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


def authorize(request: Request) -> JSONResponse | None:
    key = get_settings().eval_task_key.get_secret_value()
    if not key:
        return JSONResponse({"error": "Eval tasks are not configured"}, status_code=503)
    scheme, _, supplied = request.headers.get("authorization", "").partition(" ")
    if scheme.lower() != "bearer" or not hmac.compare_digest(supplied.encode(), key.encode()):
        return JSONResponse({"error": "Eval task key required"}, status_code=401)
    return None


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


def create_task(plan: TaskFile) -> JSONResponse:
    with psycopg.connect(get_settings().database_url) as connection:

        def existing():
            return connection.execute(
                "SELECT id FROM homelab_tasks WHERE slice = %s AND position = 1", (plan.slice,)
            ).fetchone()

        row = existing()
        if row is None:
            try:
                (task_id,) = submit(connection, plan)
            except Refused:
                # submit rolls back its savepoint on a concurrent duplicate insert.
                row = existing()
                if row is None:
                    raise
            else:
                return JSONResponse({"task": task_id}, status_code=201)
        return JSONResponse({"error": "Attempt already submitted", "task": row[0]}, status_code=409)


@router.post("/v1/tasks/eval")
async def post_eval(request: Request):
    if denied := authorize(request):
        return denied
    try:
        plan = parse_submission(await request.body())
        return await run_in_threadpool(create_task, plan)
    except Refused as error:
        return JSONResponse({"error": str(error)}, status_code=422)


def read_diff(root: Path, run_id: str | None) -> dict:
    if not run_id or not re.fullmatch(r"[A-Za-z0-9][A-Za-z0-9._-]{0,63}", run_id):
        return {"diff_base64": None}
    folder = root / run_id
    try:
        if folder.is_symlink():
            return {"diff_base64": None}
        patches = []
        for path in folder.iterdir():
            match = re.fullmatch(r"diff(?:-([1-9][0-9]*))?\.patch", path.name)
            if match:
                patches.append((int(match[1] or 1), path))
        if not patches:
            return {"diff_base64": None}
        path = max(patches)[1]
        if path.is_symlink() or not path.is_file():
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
    if denied := authorize(request):
        return denied
    if not re.fullmatch(r"[0-9]{1,19}", task_id) or not 0 < int(task_id) < 2**63:
        return JSONResponse({"error": "Task not found"}, status_code=404)
    with psycopg.connect(get_settings().database_url) as connection:
        task = connection.execute(
            "SELECT id, status, reason FROM homelab_tasks WHERE id = %s AND slice LIKE 'eval:%%'",
            (int(task_id),),
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
    diff = read_diff(get_settings().task_runs_dir, last[6] if last else None)
    return JSONResponse(
        jsonable_encoder(
            {"task": task[0], "status": task[1], "reason": task[2], "attempts": attempts, **diff}
        )
    )
