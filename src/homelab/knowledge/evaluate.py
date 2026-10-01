"""Corpus self-question baseline; report only numbers and ids."""

from __future__ import annotations

import json
from collections.abc import Callable
from typing import Any

from llama_index.core import Document

from homelab.knowledge.sources import load_corpus_documents
from homelab.settings import get_settings


def evaluate(
    documents: list[Document], *, retriever: Callable, answerer: Callable
) -> dict[str, Any]:
    misses = []
    refusals = []
    reciprocal_ranks = []
    for document in documents:
        question = document.metadata["title"]
        chunks = retriever(question, 5)
        rank = next(
            (rank for rank, chunk in enumerate(chunks[:5], 1) if chunk.path == document.id_),
            None,
        )
        reciprocal_ranks.append(1 / rank if rank else 0)
        if rank is None:
            misses.append(document.id_)
        if answerer(question, 5).refused:
            refusals.append(document.id_)
    count = len(documents)
    return {
        "documents": count,
        "hit@5": (count - len(misses)) / count if count else 0,
        "MRR": sum(reciprocal_ranks) / count if count else 0,
        "miss_ids": misses,
        "refusal_count": len(refusals),
        "refusal_ids": refusals,
    }


def run_eval() -> int:
    from homelab.api.app import KnowledgeQuery, answer_knowledge, query_brain

    settings = get_settings()
    if settings.active_collection != "about_aleix":
        raise ValueError("eval requires active_collection=about_aleix")
    result = evaluate(
        load_corpus_documents(settings.corpus_file),
        retriever=query_brain,
        answerer=lambda question, top_k: answer_knowledge(
            KnowledgeQuery(question=question, top_k=top_k)
        ),
    )
    print(json.dumps(result))
    return 0
