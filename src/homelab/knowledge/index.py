"""Construct and run the LlamaIndex ingestion pipeline."""

from __future__ import annotations

from dataclasses import dataclass
from typing import Any

from llama_index.core import Document
from llama_index.core.ingestion import DocstoreStrategy, IngestionPipeline
from llama_index.core.node_parser import MarkdownNodeParser, SentenceSplitter
from llama_index.storage.docstore.postgres import PostgresDocumentStore
from llama_index.storage.kvstore.postgres import PostgresKVStore
from llama_index.vector_stores.postgres import PGVectorStore
from sqlalchemy.engine import make_url

from homelab.settings import Settings


@dataclass(frozen=True)
class IngestionResult:
    documents_seen: int
    nodes_written: int
    skipped: int
    deleted: int


def probe_embedding_dimension(embed_model: Any) -> int:
    """Probe an embedding client rather than duplicating model dimensions in configuration."""
    embedding = embed_model.get_text_embedding("dimension probe")
    if not embedding:
        raise ValueError("embedding model returned an empty vector")
    return len(embedding)


def build_stores(settings: Settings, embed_dim: int):
    """Build the PostgreSQL vector and ingestion doc stores."""
    url = make_url(settings.database_url)
    vector_store = PGVectorStore.from_params(
        connection_string=url.set(drivername="postgresql+psycopg2"),
        async_connection_string=url.set(drivername="postgresql+asyncpg"),
        table_name="brain_chunks",
        embed_dim=embed_dim,
    )
    kvstore = PostgresKVStore.from_params(
        host=url.host,
        port=str(url.port or 5432),
        database=url.database,
        user=url.username,
        password=url.password,
        table_name="brain_docs",
    )
    docstore = PostgresDocumentStore(kvstore)
    return vector_store, docstore


def build_pipeline(*, vector_store: Any, docstore: Any, embed_model: Any) -> IngestionPipeline:
    return IngestionPipeline(
        transformations=[
            MarkdownNodeParser(),
            SentenceSplitter(chunk_size=512, chunk_overlap=64),
            embed_model,
        ],
        vector_store=vector_store,
        docstore=docstore,
        docstore_strategy=DocstoreStrategy.UPSERTS_AND_DELETE,
    )


def ingest(
    documents: list[Document], *, vector_store: Any, docstore: Any, embed_model: Any
) -> IngestionResult:
    """Incrementally ingest documents and report the observable changes."""
    existing_hashes = docstore.get_all_document_hashes()
    existing_by_id = {doc_id: value_hash for value_hash, doc_id in existing_hashes.items()}
    current_ids = {document.id_ for document in documents}
    skipped = sum(existing_by_id.get(document.id_) == document.hash for document in documents)
    deleted = len(set(existing_by_id) - current_ids)

    nodes = build_pipeline(
        vector_store=vector_store,
        docstore=docstore,
        embed_model=embed_model,
    ).run(documents=documents)
    return IngestionResult(
        documents_seen=len(documents),
        nodes_written=len(nodes),
        skipped=skipped,
        deleted=deleted,
    )
