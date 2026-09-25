import logging
from contextlib import asynccontextmanager

from fastapi.testclient import TestClient
from llama_index.core.embeddings import MockEmbedding
from pydantic import PrivateAttr

from homelab.api.app import app, query_brain
from homelab.jobs.app import app as jobs_app
from homelab.knowledge.retrieve import retrieve
from homelab.settings import Settings, get_settings
from tests.test_retrieve import fixture_index


def test_application_logging_is_configured_at_startup(monkeypatch):
    @asynccontextmanager
    async def fake_open():
        yield

    application_logger = logging.getLogger("homelab")
    query_logger = logging.getLogger("homelab.api.app")
    monkeypatch.setattr(application_logger, "handlers", [])
    monkeypatch.setattr(application_logger, "level", logging.NOTSET)
    monkeypatch.setattr(application_logger, "propagate", True)
    monkeypatch.setattr(jobs_app, "open_async", fake_open)
    level = ["INFO"]
    monkeypatch.setattr(
        "homelab.api.app.get_settings", lambda: Settings(_env_file=None, log_level=level[0])
    )

    with TestClient(app):
        assert query_logger.isEnabledFor(logging.INFO)
        assert any(
            isinstance(handler, logging.StreamHandler) for handler in application_logger.handlers
        )
        assert application_logger.propagate is False

    level[0] = "WARNING"
    with TestClient(app):
        assert not query_logger.isEnabledFor(logging.INFO)
        assert len(application_logger.handlers) == 1


def test_health_reports_routes_and_no_key(tmp_path, monkeypatch):
    routes = tmp_path / "routes.yaml"
    routes.write_text(
        "providers: {gateway: {}, local: {}}\nroutes: {chat: {provider: gateway, model: m}}\n"
    )
    get_settings.cache_clear()
    app.dependency_overrides.clear()
    monkeypatch.setenv("HOMELAB_ROUTES_FILE", str(routes))
    monkeypatch.delenv("HOMELAB_GATEWAY_API_KEY", raising=False)
    monkeypatch.setattr("homelab.api.app.get_settings", lambda: Settings(_env_file=None))

    body = TestClient(app).get("/health").json()
    assert body["status"] == "ok"
    assert body["routes"] == ["chat"]
    assert body["gateway_key_present"] is False


def test_ingest_trigger_returns_job_id(monkeypatch):
    async def fake_enqueue():
        return 42

    monkeypatch.setattr("homelab.api.app.enqueue_brain_ingest", fake_enqueue)
    response = TestClient(app).post("/v1/knowledge/ingest")
    assert response.status_code == 200
    assert response.json() == {"job_id": 42}


def test_knowledge_query_shape_default_and_content_safe_logging(monkeypatch, caplog):
    vector_store, embed_model = fixture_index()
    monkeypatch.setattr(
        "homelab.api.app.query_brain",
        lambda question, top_k: retrieve(
            question, top_k, vector_store=vector_store, embed_model=embed_model
        ),
    )
    question = "A unique neutral fixture question 9876"

    with caplog.at_level(logging.DEBUG):
        response = TestClient(app).post("/v1/knowledge/query", json={"question": question})

    assert response.status_code == 200
    chunks = response.json()["chunks"]
    assert len(chunks) == 5
    assert all(
        set(chunk) == {"text", "score", "path", "title", "header_path", "revision", "content_hash"}
        for chunk in chunks
    )
    assert all(chunk["text"] and chunk["path"] and chunk["title"] for chunk in chunks)
    assert all(isinstance(chunk["score"], float) for chunk in chunks)
    assert question not in caplog.text
    assert all(chunk["text"] not in caplog.text for chunk in chunks)
    assert f"question_length={len(question)} top_k=5 chunks=5" in caplog.text


def test_knowledge_query_respects_top_k(monkeypatch):
    vector_store, embed_model = fixture_index()
    monkeypatch.setattr(
        "homelab.api.app.query_brain",
        lambda question, top_k: retrieve(
            question, top_k, vector_store=vector_store, embed_model=embed_model
        ),
    )

    response = TestClient(app).post(
        "/v1/knowledge/query", json={"question": "Neutral fixture question", "top_k": 2}
    )

    assert response.status_code == 200
    assert len(response.json()["chunks"]) == 2


def test_query_brain_embeds_question_once_to_size_store(monkeypatch):
    vector_store, _ = fixture_index()
    dimensions = []

    class CountingEmbedding(MockEmbedding):
        _queries: int = PrivateAttr(default=0)

        def _get_query_embedding(self, query):
            self._queries += 1
            return super()._get_query_embedding(query)

    embedding = CountingEmbedding(embed_dim=8)

    class FakeRoutes:
        def embedding_model(self, purpose):
            assert purpose == "embed"
            return embedding

    monkeypatch.setattr("homelab.api.app.load_routes", lambda settings: FakeRoutes())

    def fake_build_stores(settings, embed_dim):
        dimensions.append(embed_dim)
        return vector_store, object()

    monkeypatch.setattr("homelab.api.app.build_stores", fake_build_stores)

    assert len(query_brain("Neutral fixture question", 2)) == 2
    assert dimensions == [8]
    assert embedding._queries == 1


def test_knowledge_query_rejects_invalid_bodies():
    client = TestClient(app)
    invalid_bodies = [
        {},
        {"question": ""},
        {"question": "   "},
        {"question": 123},
        {"question": "Neutral", "top_k": 2.5},
        {"question": "Neutral", "top_k": "2"},
        {"question": "Neutral", "top_k": 0},
    ]

    for body in invalid_bodies:
        assert client.post("/v1/knowledge/query", json=body).status_code == 422
