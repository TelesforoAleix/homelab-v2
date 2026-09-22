"""The Home Lab API. Loopback-only in slice 1; clients on this node call it at 127.0.0.1:8000."""

from __future__ import annotations

from contextlib import asynccontextmanager

from fastapi import FastAPI

from homelab import __version__
from homelab.jobs.app import app as jobs_app
from homelab.jobs.tasks import ingest_brain
from homelab.models import load_routes
from homelab.settings import get_settings


@asynccontextmanager
async def lifespan(_app: FastAPI):
    async with jobs_app.open_async():
        yield


app = FastAPI(title="Home Lab", version=__version__, lifespan=lifespan)


@app.get("/health")
def health() -> dict:
    settings = get_settings()
    routes = load_routes(settings=settings)
    return {
        "status": "ok",
        "version": __version__,
        "routes": routes.purposes(),
        "gateway_key_present": settings.resolve_gateway_api_key() is not None,
    }


async def enqueue_brain_ingest() -> int:
    return await ingest_brain.defer_async()


@app.post("/v1/knowledge/ingest")
async def trigger_brain_ingest() -> dict[str, int]:
    return {"job_id": await enqueue_brain_ingest()}
