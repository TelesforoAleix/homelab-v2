"""Command-line entry point for Home Lab administration."""

from __future__ import annotations

import argparse
from dataclasses import asdict
from pathlib import Path

from homelab.knowledge.index import build_stores, ingest, probe_embedding_dimension
from homelab.knowledge.sources import load_brain_documents
from homelab.models import load_routes
from homelab.settings import get_settings


def _routes() -> int:
    routes = load_routes()
    for purpose in routes.purposes():
        route = routes.resolve(purpose)
        print(f"{purpose}\t{route.provider}\t{route.model}\t{route.base_url}")
    return 0


def _ingest(brain_dir: Path | None, dry_run: bool) -> int:
    settings = get_settings()
    documents = load_brain_documents(brain_dir or settings.brain_dir, settings.brain_include)
    if dry_run:
        print(f"documents_seen={len(documents)} dry_run=true")
        return 0

    embed_model = load_routes(settings=settings).embedding_model("embed")
    embed_dim = probe_embedding_dimension(embed_model)
    vector_store, docstore = build_stores(settings, embed_dim)
    result = ingest(
        documents,
        vector_store=vector_store,
        docstore=docstore,
        embed_model=embed_model,
    )
    print(" ".join(f"{key}={value}" for key, value in asdict(result).items()))
    return 0


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(prog="homelab")
    subparsers = parser.add_subparsers(dest="command", required=True)
    ingest_parser = subparsers.add_parser("ingest")
    ingest_parser.add_argument("--brain-dir", type=Path)
    ingest_parser.add_argument("--dry-run", action="store_true")
    subparsers.add_parser("routes")
    return parser


def main(argv: list[str] | None = None) -> int:
    args = build_parser().parse_args(argv)
    if args.command == "routes":
        return _routes()
    return _ingest(args.brain_dir, args.dry_run)
