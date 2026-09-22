"""Load Markdown notes from Brain as LlamaIndex documents."""

from __future__ import annotations

import hashlib
from datetime import UTC, date, datetime
from pathlib import Path
from typing import Any

import yaml
from llama_index.core import Document

PROVENANCE_KEYS = ("path", "title", "header_path", "type", "created", "content_hash", "revision")
_HIDDEN_METADATA_KEYS = ["path", "type", "created", "content_hash", "revision"]


def _revision(brain_dir: Path) -> str | None:
    git_dir = brain_dir / ".git"
    head_path = git_dir / "HEAD"
    try:
        head = head_path.read_text().strip()
    except OSError:
        return None

    if not head.startswith("ref: "):
        return head[:7] if head else None

    ref = head.removeprefix("ref: ")
    try:
        value = (git_dir / ref).read_text().strip()
        return value[:7] if value else None
    except OSError:
        pass

    try:
        packed_refs = (git_dir / "packed-refs").read_text().splitlines()
    except OSError:
        return None
    for line in packed_refs:
        if not line or line.startswith(("#", "^")):
            continue
        value, packed_ref = line.split(" ", 1)
        if packed_ref == ref:
            return value[:7]
    return None


def _frontmatter(text: str) -> tuple[dict[str, Any], str]:
    lines = text.splitlines(keepends=True)
    if not lines or lines[0].strip() != "---":
        return {}, text
    for index, line in enumerate(lines[1:], start=1):
        if line.strip() == "---":
            metadata = yaml.safe_load("".join(lines[1:index])) or {}
            if not isinstance(metadata, dict):
                raise ValueError("Markdown frontmatter must be a mapping")
            return metadata, "".join(lines[index + 1 :]).lstrip("\n")
    return {}, text


def _iso_created(value: Any, path: Path) -> str:
    if isinstance(value, datetime):
        return value.isoformat()
    if isinstance(value, date):
        return value.isoformat()
    if value is not None:
        parsed = datetime.fromisoformat(str(value).replace("Z", "+00:00"))
        return parsed.isoformat()
    return datetime.fromtimestamp(path.stat().st_mtime, tz=UTC).isoformat()


def _title(text: str, path: Path) -> str:
    for line in text.splitlines():
        if line.startswith("# "):
            return line[2:].strip()
    return path.stem.replace("-", " ").replace("_", " ").strip().title()


def load_brain_documents(brain_dir: Path, include: list[str]) -> list[Document]:
    """Load included Markdown paths, stripping frontmatter and attaching provenance."""
    brain_dir = Path(brain_dir)
    revision = _revision(brain_dir)
    paths = {
        path
        for directory in include
        for path in (brain_dir / directory).rglob("*.md")
        if path.is_file()
    }
    documents: list[Document] = []
    for path in sorted(paths):
        raw = path.read_bytes()
        text = raw.decode("utf-8")
        frontmatter, body = _frontmatter(text)
        relative_path = path.relative_to(brain_dir).as_posix()
        metadata = {
            "path": relative_path,
            "title": _title(body, path),
            "type": frontmatter.get("type"),
            "created": _iso_created(frontmatter.get("created"), path),
            "content_hash": hashlib.sha256(raw).hexdigest(),
            "revision": revision,
        }
        documents.append(
            Document(
                text=body,
                id_=relative_path,
                metadata=metadata,
                excluded_embed_metadata_keys=_HIDDEN_METADATA_KEYS,
                excluded_llm_metadata_keys=_HIDDEN_METADATA_KEYS,
            )
        )
    return documents
