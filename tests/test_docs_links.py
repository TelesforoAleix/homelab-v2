"""The documents stay findable: the AGENTS.md map, relative links, anchors and claims citations."""

from __future__ import annotations

import json
import re
from pathlib import Path
from urllib.parse import unquote

import pytest

ROOT = Path(__file__).resolve().parent.parent
CORE = ["AGENTS.md", "ARCHITECTURE.md", "README.md", "docs/contracts.md", "docs/operations.md"]
LINK = re.compile(r"(?<!!)\[[^\]]*\]\(([^)\s]+)(?:\s+\"[^\"]*\")?\)")
FENCE = re.compile(r"^\s*(```|~~~)")


def map_paths() -> list[str]:
    text = (ROOT / "AGENTS.md").read_text()
    section = text.split("## Document map", 1)[1].split("\n## ", 1)[0]
    paths = re.findall(r"\]\(([^)#\s]+)", section)
    assert paths, "the document map names no files"
    return sorted(set(paths))


def documents() -> list[str]:
    mapped = [path for path in map_paths() if path.endswith(".md")]
    return sorted(set(CORE + mapped))


def outside_code(text: str) -> str:
    """Drop fenced blocks and inline code so examples are not read as links or headings."""
    lines, fenced = [], False
    for line in text.splitlines():
        if FENCE.match(line):
            fenced = not fenced
            lines.append("")
            continue
        lines.append("" if fenced else re.sub(r"`[^`]*`", "", line))
    return "\n".join(lines)


def anchors(path: Path) -> set[str]:
    """GitHub's heading slugs, with -1, -2 suffixes for repeated headings."""
    seen: dict[str, int] = {}
    result = set()
    for line in outside_code(path.read_text()).splitlines():
        match = re.match(r"^#{1,6}\s+(.*?)\s*#*\s*$", line)
        if not match:
            continue
        heading = re.sub(r"<[^>]+>", "", match.group(1))
        slug = re.sub(r"[^\w\- ]", "", heading.lower()).replace(" ", "-")
        count = seen.get(slug, 0)
        seen[slug] = count + 1
        result.add(slug if count == 0 else f"{slug}-{count}")
    return result


def test_document_map_files_exist():
    missing = [path for path in map_paths() if not (ROOT / path).exists()]
    assert not missing, f"AGENTS.md maps missing files: {missing}"


@pytest.mark.parametrize("document", documents())
def test_relative_links_and_anchors_resolve(document):
    source = ROOT / document
    broken = []
    for target in LINK.findall(outside_code(source.read_text())):
        if re.match(r"^[a-z][a-z0-9+.-]*:", target):
            continue
        file_part, _, anchor = target.partition("#")
        destination = (source.parent / unquote(file_part)).resolve() if file_part else source
        if not destination.exists():
            broken.append(target)
        elif anchor and destination.suffix == ".md" and anchor not in anchors(destination):
            broken.append(target)
    assert not broken, f"{document} has broken links: {broken}"


def test_claims_file_is_valid_and_cites_existing_lines():
    claims = json.loads((ROOT / "docs/claims.json").read_text())
    assert claims["version"] == 1
    assert re.fullmatch(r"[A-Za-z0-9_.-]+/[A-Za-z0-9_.-]+", claims["repository"])
    for kind, key in [("parts", "name"), ("claims", "id"), ("relations", "id")]:
        names = [item[key] for item in claims[kind]]
        assert len(names) == len(set(names)), f"repeated {kind} {key}"
    contracts = [item["name"] for item in claims["contracts"]]
    assert len(contracts) == len(set(contracts)), "repeated contract name"

    def check(path: str, lines: list[int]):
        file = ROOT / path
        assert file.is_file(), f"cited path does not exist: {path}"
        start, end = lines
        length = len(file.read_text().splitlines())
        assert 1 <= start <= end <= length, f"{path}:{start}-{end} is outside 1-{length}"

    for part in claims["parts"]:
        check(part["doc"], part["doc_lines"])
    for claim in claims["claims"]:
        check(claim["doc"], claim["doc_lines"])
        assert claim["sources"], f"claim {claim['id']} cites no source"
        for source in claim["sources"]:
            check(source["path"], source["lines"])
    for relation in claims["relations"]:
        cited = relation["provider_sources"] + relation["user_sources"]
        assert cited, f"relation {relation['id']} cites no lines"
        for source in cited:
            check(source["path"], source["lines"])
        assert relation["contract"] in contracts + [None], relation["id"]
    for contract in claims["contracts"]:
        assert contract["doc"] == "docs/contracts.md"
        check(contract["doc"], contract["doc_lines"])
