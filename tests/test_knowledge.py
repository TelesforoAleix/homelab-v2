from __future__ import annotations

import shutil
from datetime import datetime
from pathlib import Path

from llama_index.core.embeddings import MockEmbedding
from llama_index.core.storage.docstore import SimpleDocumentStore
from llama_index.core.vector_stores import SimpleVectorStore
from pydantic import PrivateAttr

from homelab.knowledge.index import ingest, probe_embedding_dimension
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


def test_brain_include_is_comma_separated(monkeypatch):
    monkeypatch.setenv("HOMELAB_BRAIN_INCLUDE", "01-knowledge, 02-ideas")
    assert Settings(_env_file=None).brain_include == ["01-knowledge", "02-ideas"]
