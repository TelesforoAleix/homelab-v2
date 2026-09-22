from __future__ import annotations

import shutil
from datetime import datetime
from pathlib import Path

import pytest
from llama_index.core.embeddings import MockEmbedding
from llama_index.core.storage.docstore import SimpleDocumentStore
from llama_index.core.vector_stores import SimpleVectorStore
from pydantic import PrivateAttr
from sqlalchemy.engine import make_url

from homelab.knowledge import index
from homelab.knowledge.index import build_stores, ingest, probe_embedding_dimension
from homelab.knowledge.sources import PROVENANCE_KEYS, load_brain_documents
from homelab.settings import Settings

FIXTURE_BRAIN = Path(__file__).parent / "fixtures" / "brain"
INCLUDE = ["01-knowledge", "02-ideas", "05-logs"]


class RecordingEmbedding(MockEmbedding):
    _texts: list[str] = PrivateAttr(default_factory=list)

    @property
    def texts(self) -> list[str]:
        return self._texts

    def _get_text_embedding(self, text: str) -> list[float]:
        self._texts.append(text)
        return super()._get_text_embedding(text)


def _copy_brain(tmp_path: Path) -> Path:
    target = tmp_path / "brain"
    shutil.copytree(FIXTURE_BRAIN, target)
    return target


def _stores():
    return SimpleVectorStore(), SimpleDocumentStore()


def test_sources_load_included_markdown_with_provenance_and_no_frontmatter():
    documents = load_brain_documents(FIXTURE_BRAIN, INCLUDE)

    assert len(documents) == 4
    assert all(not document.metadata["path"].startswith("00-inbox/") for document in documents)
    alpha = next(
        document for document in documents if document.metadata["title"] == "Alpha Reference"
    )
    assert not alpha.text.startswith("---")
    assert "source_url" not in alpha.metadata
    assert alpha.metadata["path"] == "01-knowledge/alpha.md"
    assert alpha.metadata["type"] == "reference"
    assert alpha.metadata["revision"] is None
    assert len(alpha.metadata["content_hash"]) == 64
    datetime.fromisoformat(alpha.metadata["created"])

    plain = next(document for document in documents if document.metadata["title"] == "Plain Note")
    datetime.fromisoformat(plain.metadata["created"])


def test_revision_is_read_without_git_binary(tmp_path: Path):
    brain = _copy_brain(tmp_path)
    (brain / ".git" / "refs" / "heads").mkdir(parents=True)
    (brain / ".git" / "HEAD").write_text("ref: refs/heads/main\n")
    (brain / ".git" / "refs" / "heads" / "main").write_text("1234567890abcdef\n")

    documents = load_brain_documents(brain, INCLUDE)
    assert {document.metadata["revision"] for document in documents} == {"1234567"}


def test_ingest_is_incremental_and_preserves_chunk_provenance(tmp_path: Path):
    brain = _copy_brain(tmp_path)
    vector_store, docstore = _stores()
    embedding = RecordingEmbedding(embed_dim=8)

    documents = load_brain_documents(brain, INCLUDE)
    first = ingest(
        documents,
        vector_store=vector_store,
        docstore=docstore,
        embed_model=embedding,
    )
    assert first.documents_seen == 4
    assert first.nodes_written > first.documents_seen
    assert first.skipped == 0
    assert first.deleted == 0

    for metadata in vector_store._data.metadata_dict.values():
        assert set(PROVENANCE_KEYS) <= metadata.keys()
    assert any("Alpha Reference" in text for text in embedding.texts)
    assert any("/Nested Idea/Topic/" in text for text in embedding.texts)
    assert all("content_hash" not in text for text in embedding.texts)

    second = ingest(
        load_brain_documents(brain, INCLUDE),
        vector_store=vector_store,
        docstore=docstore,
        embed_model=embedding,
    )
    assert second.nodes_written == 0
    assert second.skipped == 4

    alpha = brain / "01-knowledge" / "alpha.md"
    alpha.write_text(alpha.read_text() + "\nA neutral revision.\n")
    edited = ingest(
        load_brain_documents(brain, INCLUDE),
        vector_store=vector_store,
        docstore=docstore,
        embed_model=embedding,
    )
    assert edited.nodes_written > 0
    assert edited.skipped == 3

    deleted_path = "02-ideas/nested.md"
    (brain / deleted_path).unlink()
    deleted = ingest(
        load_brain_documents(brain, INCLUDE),
        vector_store=vector_store,
        docstore=docstore,
        embed_model=embedding,
    )
    assert deleted.deleted == 1
    assert all(
        metadata.get("ref_doc_id") != deleted_path
        for metadata in vector_store._data.metadata_dict.values()
    )


def test_probe_returns_mock_embedding_dimension():
    assert probe_embedding_dimension(MockEmbedding(embed_dim=8)) == 8


def test_ingest_preflights_vector_store_before_embedding():
    class UnavailableVectorStore:
        def add(self, nodes):
            raise ConnectionError("unavailable")

    embedding = RecordingEmbedding(embed_dim=8)

    with pytest.raises(ConnectionError, match="unavailable"):
        ingest(
            load_brain_documents(FIXTURE_BRAIN, INCLUDE),
            vector_store=UnavailableVectorStore(),
            docstore=SimpleDocumentStore(),
            embed_model=embedding,
        )

    assert embedding.texts == []


def test_build_stores_configures_sync_and_async_vector_connections(monkeypatch):
    calls = {}
    vector_store = object()
    kvstore = object()
    docstore = object()

    class FakeVectorStore:
        @classmethod
        def from_params(cls, **kwargs):
            calls["vector"] = kwargs
            return vector_store

    class FakeKVStore:
        @classmethod
        def from_params(cls, **kwargs):
            calls["kv"] = kwargs
            return kvstore

    monkeypatch.setattr(index, "PGVectorStore", FakeVectorStore)
    monkeypatch.setattr(index, "PostgresKVStore", FakeKVStore)
    monkeypatch.setattr(index, "PostgresDocumentStore", lambda store: docstore)

    stores = build_stores(
        Settings(database_url="postgresql://user:password@database:5432/homelab"),
        embed_dim=8,
    )

    assert stores == (vector_store, docstore)
    sync_url = make_url(calls["vector"]["connection_string"])
    async_url = make_url(calls["vector"]["async_connection_string"])
    assert sync_url.drivername == "postgresql+psycopg2"
    assert async_url.drivername == "postgresql+asyncpg"
    assert async_url.password == "password"
    assert async_url.port == 5432


def test_brain_include_is_comma_separated(monkeypatch):
    monkeypatch.setenv("HOMELAB_BRAIN_INCLUDE", "01-knowledge, 02-ideas")
    assert Settings(_env_file=None).brain_include == ["01-knowledge", "02-ideas"]
