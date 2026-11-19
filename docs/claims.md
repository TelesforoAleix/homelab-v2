# Keeping documentation claims current

This procedure covers `docs/claims.json`, the machine-readable list of the facts the core
documents state and the lines that prove them. Follow it in every PR that changes a core
document (`AGENTS.md`, `ARCHITECTURE.md`, `docs/contracts.md`, `docs/operations.md`,
`docs/host-files.md`, this file) or moves the code a claim cites.

## The format

The file follows Factory's
[claims schema, version 1](https://github.com/TelesforoAleix/factory/blob/main/schema/claims.schema.json):

- `parts`: every part named in the [ARCHITECTURE.md parts table](../ARCHITECTURE.md#parts),
  with the table row as `doc_lines`.
- `claims`: one factual statement each, with its document lines and the narrowest source
  lines that prove it. Cite code and configuration, not other documents.
- `relations`: each connection between two parts, or from a part to another repository's part
  as `owner/name:part`, with the provider's and the user's lines.
- `contracts`: every entry in [contracts.md](contracts.md), with its version and its lines.

Line ranges are 1-based and inclusive. Ids are short, stable and unique within their kind;
keep an id when its claim moves, and retire it only when the fact is gone.

## Steps

1. Edit the document. Lines move, so update every `doc_lines` range below the edit.
2. For a new or changed fact, add or update its claim. Cite the few lines that settle it: a
   setting, a constant, a route decorator, a validator. A citation that needs a whole module
   to read is too broad.
3. When code a claim cites moves, update its `lines`; when the code changes the fact, change
   the document first, then the claim.
4. Run `uv run pytest tests/test_docs_links.py`. It checks the document map, every relative
   link and anchor in the mapped documents, and that every cited path exists and every range
   lies inside its file. It cannot tell whether the lines support the claim.
5. Read each changed claim's cited lines yourself and confirm they say what the claim says.

## After merge

Factory runs `cplane docs-verify` on the merged commit. It asks a classifier three times
whether each claim's cited lines support it; an item passes only when all three answers are
`supported` with confidence of at least 0.9. Flagged items are fixed in a follow-up slice:
either the document is wrong, or the citation is too broad or points at the wrong lines.
