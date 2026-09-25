from __future__ import annotations

from pathlib import Path

from llama_index.core.embeddings import MockEmbedding
from llama_index.core.storage.docstore import SimpleDocumentStore
from llama_index.core.vector_stores import SimpleVectorStore
from pydantic import PrivateAttr

from homelab.knowledge.index import ingest
from homelab.knowledge.retrieve import retrieve
from homelab.knowledge.sources import PROVENANCE_KEYS, load_brain_documents

FIXTURE_BRAIN = Path(__file__).parent / "fixtures" / "brain"
INCLUDE = ["01-knowledge", "02-ideas", "05-logs"]


class InMemoryTextVectorStore(SimpleVectorStore):
    """Expose stored fixture nodes as a text-storing vector store for LlamaIndex."""

    stores_text: bool = True
    _nodes: dict = PrivateAttr(default_factory=dict)

    def add(self, nodes, **kwargs):
        self._nodes.update({node.node_id: node for node in nodes})
        return super().add(nodes, **kwargs)

    def query(self, query, **kwargs):
        # PGVectorStore treats an empty node-id list as no restriction.
        if query.node_ids == []:
            query.node_ids = None
        result = super().query(query, **kwargs)
        result.nodes = [self._nodes[node_id] for node_id in result.ids or []]
        return result


def fixture_index():
    vector_store = InMemoryTextVectorStore()
    embed_model = MockEmbedding(embed_dim=8)
    ingest(
        load_brain_documents(FIXTURE_BRAIN, INCLUDE),
        vector_store=vector_store,
        docstore=SimpleDocumentStore(),
        embed_model=embed_model,
    )
    return vector_store, embed_model


def test_retrieve_returns_scored_chunks_with_all_provenance():
    vector_store, embed_model = fixture_index()

    chunks = retrieve(
        "A neutral fixture question", 3, vector_store=vector_store, embed_model=embed_model
    )

    assert len(chunks) == 3
    assert all(set(PROVENANCE_KEYS) <= vars(chunk).keys() for chunk in chunks)
    assert all(chunk.text and isinstance(chunk.score, float) for chunk in chunks)
    assert all(not chunk.text.startswith("---") for chunk in chunks)


def test_retrieve_defaults_to_five_chunks():
    vector_store, embed_model = fixture_index()

    chunks = retrieve(
        "Another neutral fixture question", vector_store=vector_store, embed_model=embed_model
    )

    assert len(chunks) == 5
