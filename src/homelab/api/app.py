"""The Home Lab API. Loopback-only in slice 1; clients on this node call it at 127.0.0.1:8000."""

from __future__ import annotations

import logging
from contextlib import asynccontextmanager
from time import monotonic

from fastapi import FastAPI
from pydantic import BaseModel, Field, StrictInt, field_validator

from homelab import __version__
from homelab.jobs.app import app as jobs_app
from homelab.jobs.tasks import ingest_brain
from homelab.knowledge.index import build_stores
from homelab.knowledge.retrieve import Chunk, _retrieve_with_embedding
from homelab.models import load_routes
from homelab.settings import get_settings

logger = logging.getLogger(__name__)
_application_handler_name = "homelab-application"


def configure_logging() -> None:
    application_logger = logging.getLogger("homelab")
    application_logger.setLevel(get_settings().log_level)
    if not any(
        handler.name == _application_handler_name for handler in application_logger.handlers
    ):
        handler = logging.StreamHandler()
        handler.set_name(_application_handler_name)
        application_logger.addHandler(handler)
    application_logger.propagate = False


class KnowledgeQuery(BaseModel):
    question: str = Field(min_length=1)
    top_k: StrictInt = Field(default=5, ge=1)

    @field_validator("question")
    @classmethod
    def non_blank_question(cls, value: str) -> str:
        if not value.strip():
            raise ValueError("question must not be blank")
        return value


class QueryChunk(BaseModel):
    text: str
    score: float | None
    path: str
    title: str
    header_path: str
    revision: str | None
    content_hash: str


class KnowledgeQueryResponse(BaseModel):
    chunks: list[QueryChunk]


@asynccontextmanager
async def lifespan(_app: FastAPI):
    configure_logging()
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


def query_brain(question: str, top_k: int) -> list[Chunk]:
    settings = get_settings()
    embed_model = load_routes(settings=settings).embedding_model("embed")
    embedding = embed_model.get_query_embedding(question)
    vector_store, _ = build_stores(settings, len(embedding))
    return _retrieve_with_embedding(
        question, top_k, vector_store=vector_store, embed_model=embed_model, embedding=embedding
    )


@app.post("/v1/knowledge/query", response_model=KnowledgeQueryResponse)
def query_knowledge(body: KnowledgeQuery) -> KnowledgeQueryResponse:
    started = monotonic()
    chunks = query_brain(body.question, body.top_k)
    logger.info(
        "knowledge query question_length=%d top_k=%d chunks=%d elapsed_seconds=%.3f",
        len(body.question),
        body.top_k,
        len(chunks),
        monotonic() - started,
    )
    return KnowledgeQueryResponse(
        chunks=[QueryChunk.model_validate(chunk, from_attributes=True) for chunk in chunks]
    )
