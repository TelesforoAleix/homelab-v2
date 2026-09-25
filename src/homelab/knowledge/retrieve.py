"""Retrieve indexed Brain chunks through LlamaIndex's vector retriever."""

from __future__ import annotations

import logging
from dataclasses import dataclass
from typing import Any

from llama_index.core import VectorStoreIndex
from llama_index.core.schema import MetadataMode, QueryBundle

# LlamaIndex's vector-result debug logger includes retrieved node text.
logging.getLogger("llama_index.core.indices.utils").setLevel(logging.INFO)


@dataclass(frozen=True)
class Chunk:
    text: str
    score: float | None
    path: str
    title: str
    header_path: str
    type: str | None
    created: str
    content_hash: str
    revision: str | None


def retrieve(question: str, top_k: int = 5, *, vector_store: Any, embed_model: Any) -> list[Chunk]:
    """Embed one question and return the vector store's top scored chunks."""
    if not question.strip():
        raise ValueError("question must not be empty")
    if top_k < 1:
        raise ValueError("top_k must be positive")
    embedding = embed_model.get_query_embedding(question)
    return _retrieve_with_embedding(
        question, top_k, vector_store=vector_store, embed_model=embed_model, embedding=embedding
    )


def _retrieve_with_embedding(
    question: str, top_k: int, *, vector_store: Any, embed_model: Any, embedding: list[float]
) -> list[Chunk]:
    """Use a question embedding already obtained while constructing the store."""

    retriever = VectorStoreIndex.from_vector_store(
        vector_store, embed_model=embed_model
    ).as_retriever(similarity_top_k=top_k)
    chunks = []
    for item in retriever.retrieve(QueryBundle(query_str=question, embedding=embedding)):
        metadata = item.node.metadata
        chunks.append(
            Chunk(
                text=item.node.get_content(metadata_mode=MetadataMode.NONE),
                score=item.score,
                path=metadata["path"],
                title=metadata["title"],
                header_path=metadata["header_path"],
                type=metadata["type"],
                created=metadata["created"],
                content_hash=metadata["content_hash"],
                revision=metadata["revision"],
            )
        )
    return chunks
