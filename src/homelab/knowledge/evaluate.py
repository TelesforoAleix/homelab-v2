"""Corpus baseline and independent eval sets; report only numbers and ids."""

from __future__ import annotations

import json
from collections.abc import Callable
from math import ceil
from pathlib import Path
from statistics import median
from time import monotonic
from typing import Any

from llama_index.core import Document

from homelab.knowledge.sources import load_corpus_documents
from homelab.settings import get_settings


def _latency(durations: list[float]) -> dict[str, int | float]:
    ordered = sorted(durations)
    return {
        "count": len(ordered),
        "median": median(ordered) if ordered else 0,
        "p95": ordered[ceil(0.95 * len(ordered)) - 1] if ordered else 0,
        "max": ordered[-1] if ordered else 0,
    }


def _timed_answer(answerer: Callable, question: str, durations: list[float]):
    started = monotonic()
    try:
        return answerer(question, 5)
    finally:
        durations.append(monotonic() - started)


def evaluate(
    documents: list[Document], *, retriever: Callable, answerer: Callable
) -> dict[str, Any]:
    misses = []
    refusals = []
    reciprocal_ranks = []
    failures = []
    durations = []
    hits = 0
    for document in documents:
        question = document.metadata["title"]
        rank = 0
        try:
            chunks = retriever(question, 5)
            rank = next(
                (n for n, chunk in enumerate(chunks[:5], 1) if chunk.path == document.id_),
                0,
            )
            if not rank:
                misses.append(document.id_)
            if _timed_answer(answerer, question, durations).refused:
                refusals.append(document.id_)
        except Exception as error:
            failures.append(
                {
                    "id": document.id_,
                    "reason": "error",
                    "error_type": type(error).__name__,
                }
            )
        reciprocal_ranks.append(1 / rank if rank else 0)
        hits += bool(rank)
    count = len(documents)
    return {
        "documents": count,
        "hit@5": hits / count if count else 0,
        "MRR": sum(reciprocal_ranks) / count if count else 0,
        "miss_ids": misses,
        "refusal_count": len(refusals),
        "refusal_ids": refusals,
        "error_count": len(failures),
        "failures": failures,
        "answer_latency_seconds": _latency(durations),
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

    def failure(item, *, refusal=False):
        result = {"id": item["id"]}
        if refusal:
            result["reason"] = "error" if item["error_type"] else "answered"
        else:
            result["reasons"] = item["reasons"]
        if item["error_type"]:
            result["error_type"] = item["error_type"]
        return result

    def metrics(subset):
        return {
            "partial_count": sum(item["partial"] for item in subset),
            "error_count": sum(bool(item["error_type"]) for item in subset),
            "answer_latency_seconds": _latency(
                [item["duration"] for item in subset if item["duration"] is not None]
            ),
        }

    return {
        **metrics(items),
        "answer": {
            **metrics(answers),
            "count": count,
            "hit@5": sum(item["rank"] > 0 for item in answers) / count if count else 0,
            "MRR": sum(1 / item["rank"] if item["rank"] else 0 for item in answers) / count
            if count
            else 0,
            "answer_correct_rate": sum(item["correct"] for item in answers) / count if count else 0,
            "failures": [failure(item) for item in answers if item["reasons"]],
        },
        "refuse": {
            **metrics(refusals),
            "count": refusal_count,
            "correct_refusal_rate": sum(item["correct"] for item in refusals) / refusal_count
            if refusal_count
            else 0,
            "failures": [failure(item, refusal=True) for item in refusals if not item["correct"]],
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
            correct = False
            partial = False
            error_type = None
            durations = []
            try:
                if item["expect"] == "answer":
                    chunks = retriever(item["question"], 5)
                    rank = next(
                        (
                            n
                            for n, chunk in enumerate(chunks[:5], 1)
                            if chunk.path in item["entries"]
                        ),
                        0,
                    )
                    if not rank:
                        reasons.append("retrieval_miss")
                response = _timed_answer(answerer, item["question"], durations)
                partial = response.partial
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
            except Exception as error:
                reasons.append("error")
                error_type = type(error).__name__
            scores.append(
                {
                    "id": item["id"],
                    "expect": item["expect"],
                    "group": item.get("group"),
                    "rank": rank,
                    "correct": correct,
                    "partial": partial,
                    "reasons": reasons,
                    "error_type": error_type,
                    "duration": durations[0] if durations else None,
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
