"""Private video transport and content-free job metadata. No media is written to disk."""

from __future__ import annotations

import asyncio
import base64
import hashlib
import ipaddress
import json
import logging
import os
import re
from datetime import UTC, datetime
from decimal import Decimal, InvalidOperation
from time import monotonic
from uuid import NAMESPACE_URL, UUID, uuid4, uuid5

import httpx
from starlette.responses import JSONResponse, StreamingResponse

MAX_BODY_BYTES = 48 * 1024 * 1024
ASYNC_BODY_BYTES = 300 * 1024 - 8 * 1024
MAX_ACTIVE = 4
JOB_SECONDS = 3600
OPERATIONS = (
    "generate_image",
    "edit_image",
    "upscale_image",
    "image_to_video",
    "upscale_video",
    "edit_video",
)
logger = logging.getLogger("homelab.models")


def billed_cost(body):
    if not isinstance(body, dict):
        return None
    metadata = body.get("providerMetadata")
    if not isinstance(metadata, dict):
        return None
    gateway = metadata.get("gateway", {})
    if not isinstance(gateway, dict):
        return None
    for field in ("gatewayCost", "cost"):
        value = gateway.get(field)
        if value is None or isinstance(value, (bool, dict, list)):
            continue
        try:
            amount = Decimal(str(value))
            if amount.is_finite() and 0 <= amount < Decimal("1e18"):
                return amount
        except InvalidOperation:
            pass
    return None


def error(status, message):
    return JSONResponse(
        {
            "error": {
                "message": message,
                "type": "invalid_request_error",
                "param": None,
                "code": None,
            }
        },
        status_code=status,
    )


def public_job(row):
    return {
        "id": str(row["id"]),
        "operation": row["operation"],
        "model": row["model"],
        "status": row["status"],
        "cost_usd": format(row["cost_usd"], "f") if row["cost_usd"] is not None else None,
    }


def media_file(value, kind):
    if not isinstance(value, str):
        raise ValueError
    match = re.fullmatch(
        r"data:((?:image/(?:png|jpeg|webp|tiff)|video/mp4));base64,([A-Za-z0-9+/=]+)", value
    )
    if not match or not match[1].startswith(kind + "/"):
        raise ValueError
    decoded = base64.b64decode(match[2], validate=True)
    if not decoded:
        raise ValueError
    return {"type": "file", "mediaType": match[1], "data": match[2]}


async def read_body(request):
    size = 0
    parts = []
    async for part in request.stream():
        size += len(part)
        if size > MAX_BODY_BYTES:
            raise OverflowError
        parts.append(part)
    return b"".join(parts)


class PostgresStore:
    async def connect(self):
        import psycopg
        from psycopg.rows import dict_row

        return await psycopg.AsyncConnection.connect(
            os.environ["HOMELAB_DATABASE_URL"], connect_timeout=5, row_factory=dict_row
        )

    async def create(self, job_id, key_hash, operation, model):
        async with await self.connect() as connection:
            cursor = await connection.execute(
                """INSERT INTO homelab_video_jobs (id, client_key_hash, operation, model, status)
                   VALUES (%s, %s, %s, %s, 'queued') ON CONFLICT DO NOTHING RETURNING *""",
                (job_id, key_hash, operation, model),
            )
            row = await cursor.fetchone()
            if row is not None:
                return {**row, "_new": True}
        row = await self.get(job_id, key_hash)
        if row is None:
            raise ValueError("Job ID conflict")
        return {**row, "_new": False}

    async def get(self, job_id, key_hash):
        async with await self.connect() as connection:
            cursor = await connection.execute(
                "SELECT * FROM homelab_video_jobs WHERE id = %s AND client_key_hash = %s",
                (job_id, key_hash),
            )
            return await cursor.fetchone()

    async def recover(self):
        async with await self.connect() as connection:
            cursor = await connection.execute(
                "SELECT * FROM homelab_video_jobs WHERE status IN ('queued', 'running')"
            )
            return await cursor.fetchall()

    async def update(self, job_id, *, status=None, reference=None, cost=None):
        from psycopg.types.json import Jsonb

        async with await self.connect() as connection:
            cursor = await connection.execute(
                "SELECT * FROM homelab_video_jobs WHERE id = %s FOR UPDATE", (job_id,)
            )
            row = await cursor.fetchone()
            # The job lock and aggregate debit share one transaction. Reads and recovery
            # cannot charge a completed or charged-failure job a second time.
            if cost is not None and row["cost_usd"] is None:
                await connection.execute(
                    """INSERT INTO homelab_image_costs
                       (client_key_hash, month, total_usd, charged_calls) VALUES (%s, %s, %s, 1)
                       ON CONFLICT (client_key_hash, month) DO UPDATE
                       SET total_usd = homelab_image_costs.total_usd + EXCLUDED.total_usd,
                           charged_calls = homelab_image_costs.charged_calls + 1""",
                    (row["client_key_hash"], datetime.now(UTC).date().replace(day=1), cost),
                )
                row["cost_usd"] = cost
            cursor = await connection.execute(
                """UPDATE homelab_video_jobs SET status = %s, reference = %s, cost_usd = %s,
                   updated_at = now() WHERE id = %s RETURNING *""",
                (
                    status or row["status"],
                    Jsonb(reference if reference is not None else row["reference"]),
                    row["cost_usd"],
                    job_id,
                ),
            )
            return await cursor.fetchone()


class VideoGateway:
    def __init__(self, config, store, api_key, *, transport=None, poll_seconds=5):
        self.models = {r["model_name"]: r for r in config["media_model_list"]}
        self.base_url = config["media_gateway_url"].rstrip("/")
        self.store, self.api_key = store, api_key
        self.transport, self.poll_seconds = transport, poll_seconds
        self.tasks = {}
        self.results = {}
        self.recovered = False
        self.pending_charge = {}
        self.lock = asyncio.Lock()

    def client(self, timeout=JOB_SECONDS + 60):
        return httpx.AsyncClient(
            trust_env=False, timeout=timeout, transport=self.transport, follow_redirects=False
        )

    def headers(self, model, job_id):
        return {
            "authorization": "Bearer " + self.api_key,
            "ai-gateway-protocol-version": "0.0.1",
            "ai-gateway-auth-method": "api-key",
            "ai-model-id": self.models[model]["gateway_model"],
            "ai-video-model-specification-version": "4",
            "idempotency-key": str(job_id),
            "content-type": "application/json",
        }

    def spawn(self, job_id, coroutine):
        task = asyncio.create_task(coroutine)
        self.tasks[str(job_id)] = task
        task.add_done_callback(lambda _: self.tasks.pop(str(job_id), None))

    async def recover(self):
        async with self.lock:
            if self.recovered:
                return
            for row in await self.store.recover():
                if row["reference"] is not None:
                    self.spawn(
                        row["id"],
                        self.run(str(row["id"]), row["model"], reference=row["reference"]),
                    )
                else:
                    # Inline inputs deliberately do not persist. Never replay an ambiguous
                    # paid single request after a restart, or invent a cost for it.
                    await self.store.update(row["id"], status="failed")
            self.recovered = True

    def validate(self, body):
        if not isinstance(body, dict) or set(body) - {
            "model",
            "operation",
            "prompt",
            "image",
            "endImage",
            "referenceImages",
            "video",
            "duration",
            "aspectRatio",
            "resolution",
            "generateAudio",
            "source",
        }:
            raise ValueError
        model = body.get("model")
        if not isinstance(model, str) or model not in self.models:
            raise ValueError
        row = self.models[model]
        info = row["model_info"]
        operation = body.get("operation")
        if operation not in info["operations"] or operation not in OPERATIONS[3:]:
            raise ValueError
        prompt = body.get("prompt", "")
        if not isinstance(prompt, str) or len(prompt) > info.get("max_prompt_chars", 2500):
            raise ValueError
        if operation != "upscale_video" and not prompt.strip():
            raise ValueError
        caps = info["capabilities"]
        caps = {**caps, **caps.get(operation, {})}
        wire = {"prompt": prompt, "n": 1}
        if operation == "image_to_video":
            wire["image"] = media_file(body.get("image"), "image")
            if body.get("video") is not None or body.get("source") is not None:
                raise ValueError
            if "endImage" in body:
                if not caps["end_frame"]:
                    raise ValueError
                wire["frameImages"] = [
                    {"frameType": "first_frame", "image": wire.pop("image")},
                    {"frameType": "last_frame", "image": media_file(body["endImage"], "image")},
                ]
            refs = body.get("referenceImages", [])
            if not isinstance(refs, list) or len(refs) > caps["reference_images"]:
                raise ValueError
            if refs:
                if not caps.get("start_with_references") or "frameImages" in wire:
                    raise ValueError
                wire["inputReferences"] = [
                    wire.pop("image"),
                    *(media_file(r, "image") for r in refs),
                ]
        else:
            if any(k in body for k in ("image", "endImage", "referenceImages")):
                raise ValueError
            source_video = media_file(body.get("video"), "video")
            if operation == "upscale_video":
                wire["inputReferences"] = [source_video]
            else:
                wire["providerOptions"] = {
                    info["provider_options_key"]: {
                        "videoUrl": "data:"
                        + source_video["mediaType"]
                        + ";base64,"
                        + source_video["data"]
                    }
                }
        for field, cap in [
            ("duration", "durations"),
            ("aspectRatio", "aspect_ratios"),
            ("resolution", "resolutions"),
        ]:
            if field in body:
                value = body[field]
                if isinstance(value, bool) or value not in caps[cap]:
                    raise ValueError
                wire[field] = value
        if operation == "image_to_video":
            wire.setdefault("duration", caps["durations"][0])
            wire.setdefault("resolution", caps["resolutions"][0])
        if operation == "upscale_video" and "resolution" not in wire:
            raise ValueError
        if "generateAudio" in body:
            value = body["generateAudio"]
            if not isinstance(value, bool) or (value and not caps["audio"]):
                raise ValueError
            wire["generateAudio"] = value
        source = body.get("source")
        if source is not None:
            if operation != "upscale_video" or not isinstance(source, dict):
                raise ValueError
            if (
                set(source) - {"width", "height", "duration", "frameRate", "frameCount"}
                or not {"width", "height", "duration", "frameRate"} <= source.keys()
            ):
                raise ValueError
            if any(type(v) not in (int, float) or not 0 < v < 1e7 for v in source.values()):
                raise ValueError
            wire.setdefault("providerOptions", {})[info["provider_options_key"]] = {
                "source": source
            }
        if info.get("source_required") and source is None:
            raise ValueError
        # Server-owned provider controls only; no arbitrary provider options from clients.
        if info.get("provider_defaults"):
            wire.setdefault("providerOptions", {}).setdefault(
                info["provider_options_key"], {}
            ).update(info["provider_defaults"])
        return model, operation, wire

    async def handle(self, request):
        scheme, _, key = request.headers.get("authorization", "").partition(" ")
        if scheme.lower() != "bearer" or not key.strip():
            return error(401, "Bearer client key required")
        if request.query_params:
            return error(400, "Query parameters are not offered")
        key_hash = hashlib.sha256(key.encode()).hexdigest()
        for job_id, (state, cost) in list(self.pending_charge.items()):
            try:
                await self.store.update(job_id, status=state, cost=cost)
                del self.pending_charge[job_id]
            except Exception:
                return error(503, "Video cost could not be recorded")
        path = request.url.path
        if path == "/v1/videos" and request.method == "POST":
            try:
                idempotency = request.headers.get("idempotency-key")
                if idempotency is not None and not re.fullmatch(
                    r"[A-Za-z0-9_.:-]{1,128}", idempotency
                ):
                    raise ValueError
                raw = await read_body(request)
                model, operation, wire = self.validate(json.loads(raw))
            except OverflowError:
                return error(413, "Video request exceeds 48 MiB")
            except (ValueError, TypeError, UnicodeError):
                return error(400, "Invalid video request or unsupported capability")
            try:
                await self.recover()
                async with self.lock:
                    job_id = (
                        str(uuid5(NAMESPACE_URL, key_hash + ":" + idempotency))
                        if idempotency
                        else str(uuid4())
                    )
                    if idempotency:
                        existing = await self.store.get(job_id, key_hash)
                        if existing is not None:
                            return JSONResponse(public_job(existing), status_code=202)
                    if len(self.tasks) >= MAX_ACTIVE:
                        return error(429, "Video jobs are busy; try later")
                    row = await self.store.create(job_id, key_hash, operation, model)
                    if row.get("_new", True):
                        self.spawn(job_id, self.run(job_id, model, wire=wire))
                return JSONResponse(public_job(row), status_code=202)
            except Exception:
                return error(503, "Video jobs unavailable")
        match = re.fullmatch(r"/v1/videos/([0-9a-f-]{36})(/content)?", path)
        if not match or request.method != "GET":
            return error(404, "Video job not found")
        try:
            job_id = str(UUID(match[1]))
            await self.recover()
            row = await self.store.get(job_id, key_hash)
            if row is None:
                return error(404, "Video job not found")
            if match[2]:
                return await self.content(row)
            response = JSONResponse(public_job(row))
            if row["cost_usd"] is not None:
                response.headers["x-homelab-cost-usd"] = format(row["cost_usd"], "f")
            return response
        except ValueError:
            return error(404, "Video job not found")
        except Exception:
            return error(503, "Video jobs unavailable")

    async def run(self, job_id, model, *, wire=None, reference=None):
        started = monotonic()
        status = "failed"
        cost = None
        try:
            await self.store.update(job_id, status="running")
            async with asyncio.timeout(JOB_SECONDS), self.client() as client:
                headers = self.headers(model, job_id)
                if reference is None:
                    raw = json.dumps(wire, separators=(",", ":")).encode()
                    if len(raw) <= ASYNC_BODY_BYTES:
                        result = await client.post(
                            self.base_url + "/video-model/start", headers=headers, content=raw
                        )
                        body = result.json()
                        cost = billed_cost(body)
                        if result.status_code >= 400:
                            raise ValueError
                        reference = body["operation"]
                        await self.store.update(job_id, reference=reference)
                    else:
                        body = await self.single(client, headers, raw)
                if reference is not None:
                    while True:
                        result = await client.post(
                            self.base_url + "/video-model/status",
                            headers=headers,
                            json={"operation": reference},
                        )
                        body = result.json()
                        cost = billed_cost(body) if billed_cost(body) is not None else cost
                        if result.status_code >= 400:
                            raise ValueError
                        if body.get("status") != "pending":
                            break
                        await asyncio.sleep(self.poll_seconds)
                cost = billed_cost(body) if billed_cost(body) is not None else cost
                if body.get("status") == "error" or body.get("type") == "error":
                    raise ValueError
                videos = body.get("videos")
                if not isinstance(videos, list) or not videos:
                    raise ValueError
                video = videos[0]
                if video.get("type") not in ("url", "base64") or video.get("mediaType") not in (
                    "video/mp4",
                    "video/webm",
                    "video/quicktime",
                ):
                    raise ValueError
                self.prune()
                if len(self.results) >= 16:
                    del self.results[next(iter(self.results))]
                self.results[job_id] = (monotonic(), video)
                status = "completed"
        except (Exception, asyncio.CancelledError):
            pass
        finally:
            try:
                await self.store.update(job_id, status=status, cost=cost)
            except Exception:
                # Retain the charge in memory for a later read to retry the transaction.
                self.pending_charge[job_id] = (status, cost)
                logger.error("video_cost_record_failed job=%s", job_id)
            logger.info(
                "video_job id=%s model=%s status=%s elapsed_seconds=%.3f cost_usd=%s",
                job_id,
                model,
                status,
                monotonic() - started,
                cost,
            )

    async def single(self, client, headers, raw):
        headers = {**headers, "accept": "text/event-stream", "content-type": "application/json"}
        async with client.stream(
            "POST", self.base_url + "/video-model", headers=headers, content=raw
        ) as response:
            if response.status_code >= 400:
                # Cost metadata on a charged HTTP failure is still consumed privately.
                return json.loads(await response.aread())
            data = []
            async for line in response.aiter_lines():
                if line.startswith("data:"):
                    data.append(line[5:].lstrip())
                elif not line and data:
                    body = json.loads("\n".join(data))
                    if body.get("type") in ("result", "error"):
                        return body
                    data = []
            if data:
                return json.loads("\n".join(data))
            raise ValueError

    def prune(self):
        for job_id, (created, _) in list(self.results.items()):
            if monotonic() - created > JOB_SECONDS:
                del self.results[job_id]

    async def content(self, row):
        if row["status"] != "completed":
            return error(409, "Video is not completed")
        self.prune()
        job_id = str(row["id"])
        item = self.results.get(job_id)
        if item is None and row["reference"] is not None:
            async with self.client(timeout=30) as client:
                result = await client.post(
                    self.base_url + "/video-model/status",
                    headers=self.headers(row["model"], job_id),
                    json={"operation": row["reference"]},
                )
                body = result.json()
                if result.status_code == 200 and body.get("status") == "completed":
                    item = (monotonic(), body["videos"][0])
        if item is None:
            return error(410, "Video content expired; do not repeat the paid request automatically")
        video = item[1]
        headers = {}
        if row["cost_usd"] is not None:
            headers["x-homelab-cost-usd"] = format(row["cost_usd"], "f")
        if video["type"] == "base64":
            raw = base64.b64decode(video["data"], validate=True)

            async def chunks():
                for offset in range(0, len(raw), 64 * 1024):
                    yield raw[offset : offset + 64 * 1024]

            headers["content-length"] = str(len(raw))
            return StreamingResponse(chunks(), headers=headers, media_type=video["mediaType"])
        # Fetch only provider-supplied HTTPS results. No client URL, bearer key, redirect,
        # private address, cookies, diagnostics or provider headers leave this boundary.
        url = httpx.URL(video["url"])
        if url.scheme != "https" or not url.host or url.username or url.password:
            return error(502, "Video content unavailable")
        try:
            if not ipaddress.ip_address(url.host).is_global:
                return error(502, "Video content unavailable")
        except ValueError:
            if url.host == "localhost" or url.host.endswith((".local", ".internal")):
                return error(502, "Video content unavailable")
        client = self.client(timeout=120)
        try:
            upstream = await client.send(
                client.build_request("GET", url, headers={"accept-encoding": "identity"}),
                stream=True,
            )
            if upstream.status_code != 200:
                await upstream.aclose()
                await client.aclose()
                return error(410, "Video content expired or unavailable")
        except Exception:
            await client.aclose()
            return error(502, "Video content unavailable")
        if (
            upstream.headers.get("content-length", "").isdigit()
            and upstream.headers.get("content-encoding", "identity") == "identity"
        ):
            headers["content-length"] = upstream.headers["content-length"]

        async def stream():
            try:
                async for chunk in upstream.aiter_bytes():
                    yield chunk
            finally:
                await upstream.aclose()
                await client.aclose()

        return StreamingResponse(stream(), media_type=video["mediaType"], headers=headers)
