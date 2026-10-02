"""Corpus baseline and independent eval sets; report only numbers and ids."""

from __future__ import annotations

import json
from collections.abc import Callable
from pathlib import Path
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


def load_eval_sets(path: Path) -> dict[str, list[dict[str, Any]]] | None:
    """Validate before calls; errors never include personal content."""
    try:
        payload = json.loads(path.read_text())
    except FileNotFoundError:
        return None
    except (ValueError, UnicodeError):
        raise ValueError("invalid eval JSON") from None
    names = ("bank_paraphrases", "refusal_checks", "visitor")
    groups = ("hiring", "tech", "founder", "curious", "scope")
    if (
        not isinstance(payload, dict)
        or type(payload.get("version")) is not int
        or payload["version"] != 1
        or not isinstance(payload.get("sets"), dict)
        or set(payload["sets"]) != set(names)
    ):
        raise ValueError("invalid eval structure")
    sets = payload["sets"]
    for name in names:
        items = sets[name]
        if not isinstance(items, list):
            raise ValueError("invalid eval set")
        ids = set()
        for item in items:
            if (
                not isinstance(item, dict)
                or not isinstance(item.get("id"), str)
                or not item["id"].strip()
                or not isinstance(item.get("question"), str)
                or not item["question"].strip()
                or item.get("expect") not in ("answer", "refuse")
                or not isinstance(item.get("entries"), list)
                or any(not isinstance(e, str) or not e.strip() for e in item["entries"])
                or (item["expect"] == "answer" and not item["entries"])
                or (item["expect"] == "refuse" and item["entries"])
                or (name == "visitor" and item.get("group") not in groups)
                or item["id"] in ids
            ):
                raise ValueError("invalid eval item")
            ids.add(item["id"])
    return sets


def _summarize(items: list[dict[str, Any]]) -> dict[str, Any]:
    answers = [item for item in items if item["expect"] == "answer"]
    refusals = [item for item in items if item["expect"] == "refuse"]
    count = len(answers)
    refusal_count = len(refusals)
    return {
        "answer": {
            "count": count,
            "hit@5": sum(item["rank"] > 0 for item in answers) / count if count else 0,
            "MRR": sum(1 / item["rank"] if item["rank"] else 0 for item in answers) / count
            if count
            else 0,
            "answer_correct_rate": sum(item["correct"] for item in answers) / count if count else 0,
            "failures": [
                {"id": item["id"], "reasons": item["reasons"]}
                for item in answers
                if item["reasons"]
            ],
        },
        "refuse": {
            "count": refusal_count,
            "correct_refusal_rate": sum(item["correct"] for item in refusals) / refusal_count
            if refusal_count
            else 0,
            "failures": [
                {"id": item["id"], "reason": "answered"} for item in refusals if not item["correct"]
            ],
        },
    }


def evaluate_sets(
    sets: dict[str, list[dict[str, Any]]], *, retriever: Callable, answerer: Callable
) -> dict[str, Any]:
    reports = {}
    for name, items in sets.items():
        scores = []
        for item in items:
            reasons = []
            rank = 0
            if item["expect"] == "answer":
                chunks = retriever(item["question"], 5)
                rank = next(
                    (n for n, chunk in enumerate(chunks[:5], 1) if chunk.path in item["entries"]),
                    0,
                )
                if not rank:
                    reasons.append("retrieval_miss")
            response = answerer(item["question"], 5)
            if item["expect"] == "answer":
                correct = not response.refused and any(
                    source.path in item["entries"] for source in response.sources
                )
                if response.refused:
                    reasons.append("refused")
                elif not correct:
                    reasons.append("wrong_citation")
            else:
                correct = response.refused
            scores.append(
                {
                    "id": item["id"],
                    "expect": item["expect"],
                    "group": item.get("group"),
                    "rank": rank,
                    "correct": correct,
                    "reasons": reasons,
                }
            )
        report = _summarize(scores)
        if name == "visitor":
            report["groups"] = {
                group: _summarize([score for score in scores if score["group"] == group])
                for group in ("hiring", "tech", "founder", "curious", "scope")
            }
        reports[name] = report
    return reports


def run_eval() -> int:
    from homelab.api.app import KnowledgeQuery, answer_knowledge, query_brain

    settings = get_settings()
    if settings.active_collection != "about_aleix":
        raise ValueError("eval requires active_collection=about_aleix")
    sets = load_eval_sets(settings.eval_file)

    def answerer(question, top_k):
        return answer_knowledge(KnowledgeQuery(question=question, top_k=top_k))

    result = evaluate(
        load_corpus_documents(settings.corpus_file),
        retriever=query_brain,
        answerer=answerer,
    )
    result["eval_file_found"] = int(sets is not None)
    if sets is not None:
        result["sets"] = evaluate_sets(sets, retriever=query_brain, answerer=answerer)
    print(json.dumps(result))
    return 0
