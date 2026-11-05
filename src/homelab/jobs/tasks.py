"""Procrastinate tasks registered by the worker."""

from __future__ import annotations

import logging
from dataclasses import asdict
from time import monotonic

from homelab.jobs.app import app
from homelab.knowledge.index import build_stores, ingest, probe_embedding_dimension
from homelab.knowledge.sources import load_documents
from homelab.models import load_routes
from homelab.settings import get_settings
from homelab.tasks.store import JOB, QUEUE

logger = logging.getLogger(__name__)


@app.task(queue=QUEUE, name=JOB)
def run_coding_task(task_id: int) -> None:
    """Registered so a deferred job is valid. The host's homelab-task-runner claims these jobs;
    the worker does not serve this queue and has no Docker access, so it can never run one."""
    raise NotImplementedError("homelab-task-runner runs this job, not the worker")


@app.task(queue="default", name="ping")
def ping(message: str = "pong") -> str:
    """The smallest task: proves the queue, the schema and the worker."""
    return message


@app.task(queue="local", name="ingest_brain")
def ingest_brain() -> dict[str, int]:
    started = monotonic()
    settings = get_settings()
    documents = load_documents(settings)
    embed_model = load_routes(settings=settings).embedding_model("embed")
    embed_dim = probe_embedding_dimension(embed_model)
    vector_store, docstore = build_stores(settings, embed_dim)
    result = ingest(
        documents,
        vector_store=vector_store,
        docstore=docstore,
        embed_model=embed_model,
    )
    values = asdict(result)
    logger.info(
        "knowledge ingestion completed collection=%s documents=%d nodes=%d skipped=%d deleted=%d "
        "elapsed_seconds=%.3f",
        settings.active_collection,
        result.documents_seen,
        result.nodes_written,
        result.skipped,
        result.deleted,
        monotonic() - started,
    )
    return values
