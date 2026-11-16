"""Application-scoped run API; the existing host runner consumes its task bridge."""

from __future__ import annotations

import base64
import json
from uuid import UUID

import procrastinate
import psycopg
from fastapi import APIRouter, Request
from fastapi.encoders import jsonable_encoder
from fastapi.responses import JSONResponse
from pydantic import ValidationError
from starlette.concurrency import run_in_threadpool

from homelab.api.run_contract import Message, RunRequest
from homelab.api.run_keys import ApplicationKey, RunError, authorize, load_keys
from homelab.settings import get_settings
from homelab.tasks import runs as store
from homelab.tasks.executors import Executor, load_executors
from homelab.tasks.store import JOB, QUEUE

router = APIRouter()
submit_app = procrastinate.App(connector=procrastinate.PsycopgConnector())
_table = None
BODY_LIMIT = 4 * 1024 * 1024


def connect():
    return psycopg.connect(get_settings().database_url)


def executors():
    return _table if _table is not None else load_executors()


def startup():
    global _table
    _table = load_executors()
    load_keys()


def configure_job(**options):
    return submit_app.configure_task(JOB, queue=QUEUE, **options)


def selected(body: RunRequest) -> Executor:
    if body.eval:
        name = body.eval.configuration
        return Executor(configuration=name if name.startswith("pi/") else "pi/" + name)
    return executors()[body.role][body.level][0]


def create(identity: ApplicationKey, body: RunRequest, **kwargs):
    with connect() as connection:
        return store.create(
            connection,
            identity.application,
            body,
            selected(body),
            configure_job=configure_job,
            repositories=identity.repositories,
            **kwargs,
        )


def parse(model, raw: bytes):
    if len(raw) > BODY_LIMIT:
        raise RunError(413, "body_too_large")
    try:
        return model.model_validate_json(raw)
    except ValidationError:
        raise RunError(422, "invalid_request") from None


async def request_bytes(request: Request):
    data = bytearray()
    async for chunk in request.stream():
        data.extend(chunk)
        if len(data) > BODY_LIMIT:
            raise RunError(413, "body_too_large")
    return bytes(data)


def identifier(value: str) -> UUID:
    try:
        return UUID(value)
    except ValueError:
        raise RunError(404, "run_not_found") from None


def failure(error: RunError):
    content = {"error": error.code}
    if error.status_reason is not None:
        content["status_reason"] = error.status_reason
    if error.message is not None:
        content["message"] = error.message
    return JSONResponse(content, status_code=error.status)


def response(content, status_code=200):
    return JSONResponse(jsonable_encoder(content), status_code=status_code)


@router.post("/v1/runs")
async def post_run(request: Request):
    try:
        identity = authorize(request, "runs:write")
        body = parse(RunRequest, await request_bytes(request))
        if body.eval and "runs:eval" not in identity.scopes:
            raise RunError(403, "scope_required")
        record, created = await run_in_threadpool(create, identity, body)
        return response({"run_id": record["id"]}, 201 if created else 200)
    except RunError as error:
        return failure(error)


def attempts(connection, task_id: int) -> tuple[list[dict], dict, str | None]:
    rows = connection.execute(
        """SELECT a.number, a.status, a.reason, a.started_at, a.ended_at,
                  a.configuration_hash, a.run_id, a.check_exit_code,
                  sum(t.steps), sum(t.fresh_tokens), sum(t.cached_tokens), sum(t.output_tokens),
                  CASE WHEN count(t.cost_usd) = count(t.id) THEN sum(t.cost_usd) END,
                  sum(t.seconds)
           FROM homelab_task_attempts a LEFT JOIN homelab_task_traces t
             ON t.task_id = a.task_id AND t.attempt = a.number
           WHERE a.task_id = %s GROUP BY a.task_id, a.number ORDER BY a.number""",
        (task_id,),
    ).fetchall()
    result = []
    for row in rows:
        result.append(
            {
                "number": row[0],
                "status": row[1],
                "outcome": row[2],
                "started_at": row[3],
                "ended_at": row[4],
                "configuration_hash": row[5],
                "check_exit_code": row[7],
                "metrics": {
                    "requests": row[8],
                    "tokens": {"fresh": row[9], "cached": row[10], "output": row[11]},
                    "usd": row[12],
                    "minutes": row[13] / 60 if row[13] is not None else None,
                },
            }
        )
    metrics = {
        "requests": sum(row[8] or 0 for row in rows),
        "tokens": {
            name: sum(row[index] or 0 for row in rows)
            for name, index in (("fresh", 9), ("cached", 10), ("output", 11))
        },
        "usd": sum(row[12] for row in rows)
        if rows and all(row[12] is not None for row in rows)
        else None,
        "minutes": sum(row[13] or 0 for row in rows) / 60,
    }
    last = next((row for row in reversed(rows) if row[4] is not None), None)
    return result, metrics, last[6] if last else None


def record(connection, saved: dict) -> dict:
    from homelab.api.eval_tasks import read_diff

    document = saved["request"]
    history, metrics, last_run = attempts(connection, saved["task_id"])
    result = {
        "contract": 1,
        "run_id": saved["id"],
        "request_id": saved["request_id"],
        "application": saved["application"],
        "attribution": document["attribution"],
        **{name: document[name] for name in ("repository", "branch", "mode", "role", "level")},
        "status": saved["status"],
        "status_reason": saved["status_reason"],
        "executor": saved["executor"],
        "configuration_hash": saved["configuration_hash"],
        "attempts": history,
        "commits": saved["commits"],
        "metrics": metrics,
        "resumable": False,
        "final_message": saved["final_message"],
        "resume_at": saved["resume_at"],
        **{name: saved[name] for name in ("created_at", "updated_at", "started_at", "finished_at")},
    }
    if document["eval"]:
        result.update(read_diff(get_settings().eval_diffs_dir, last_run))
    return result


def read(application: str, run_id: UUID):
    with connect() as connection:
        with connection.transaction():
            store.lock(connection)
            saved = store.row(connection, application, run_id)
            if saved is None:
                raise RunError(404, "run_not_found")
            return record(connection, saved)


@router.get("/v1/runs/{run_id}")
def get_run(run_id: str, request: Request):
    try:
        identity = authorize(request, "runs:read")
        return response(read(identity.application, identifier(run_id)))
    except RunError as error:
        return failure(error)


def encode_cursor(application: str, revision: int) -> str:
    return base64.urlsafe_b64encode(json.dumps([1, application, revision]).encode()).decode()


def decode_cursor(application: str, cursor: str) -> int:
    try:
        version, owner, revision = json.loads(
            base64.b64decode(cursor, altchars=b"-_", validate=True)
        )
        if version != 1 or owner != application or type(revision) is not int or revision < 0:
            raise ValueError
        return revision
    except (ValueError, TypeError):
        raise RunError(400, "invalid_cursor") from None


def listing(application: str, *, changed_after: str | None, request_id: str | None):
    revision = decode_cursor(application, changed_after) if changed_after else 0
    with connect() as connection:
        # Share the bridge writer lock while choosing the watermark and rows.
        with connection.transaction():
            store.lock(connection)
            rows = connection.execute(
                """SELECT id, revision FROM homelab_runs WHERE application = %s AND revision > %s
                   AND (%s::text IS NULL OR request_id = %s) ORDER BY revision, id LIMIT 100""",
                (application, revision, request_id, request_id),
            ).fetchall()
            documents = [
                record(connection, store.row(connection, application, run_id)) for run_id, _ in rows
            ]
            watermark = rows[-1][1] if rows else revision
    return {"runs": documents, "cursor": encode_cursor(application, watermark)}


@router.get("/v1/runs")
def get_runs(request: Request):
    try:
        identity = authorize(request, "runs:read")
        if set(request.query_params) - {"changed_after", "request_id"}:
            raise RunError(400, "invalid_query")
        return response(
            listing(
                identity.application,
                changed_after=request.query_params.get("changed_after"),
                request_id=request.query_params.get("request_id"),
            )
        )
    except RunError as error:
        return failure(error)


@router.post("/v1/runs/{run_id}/messages")
async def post_message(run_id: str, request: Request):
    try:
        identity = authorize(request, "runs:write")
        # Ownership precedes body parsing so another application's run always gets 404.
        await run_in_threadpool(read, identity.application, identifier(run_id))
        parse(Message, await request_bytes(request))
        raise RunError(409, "not_resumable")
    except RunError as error:
        return failure(error)


def cancelling(application: str, run_id: UUID):
    with connect() as connection:
        return record(connection, store.cancel(connection, application, run_id))


@router.post("/v1/runs/{run_id}/cancel")
def post_cancel(run_id: str, request: Request):
    try:
        identity = authorize(request, "runs:write")
        return response(cancelling(identity.application, identifier(run_id)))
    except RunError as error:
        return failure(error)
