# Knowledge

This document covers knowledge ingestion and retrieval: collections, the corpus and Brain
sources, the query and answer endpoints and `homelab eval`. Read it before changing
`src/homelab/knowledge/` or the knowledge endpoints in `src/homelab/api/app.py`.

## Collections and ingestion

`HOMELAB_ACTIVE_COLLECTION` selects `about_aleix` (default) or `brain` for the ingest job,
CLI and both readers. LlamaIndex stores each in its own logical `<collection>_chunks` and
`<collection>_docs` tables (physical `data_` prefix). Switching collections leaves the other
collection's data untouched. Public JSON corpus entries load directly as documents: question
then answer, stable id and path, question title, id-prefix pillar type and category metadata.
Bookkeeping, category and pillar type are hidden from embedding and LLM metadata. Non-public
entries are skipped. Invalid individual entries log only their position; structural corpus
errors abort before storage writes. The read-only corpus mount defaults to `/data/corpus`,
with `HOMELAB_CORPUS_FILE=/data/corpus/about-aleix/corpus.json`.

Included Markdown from Brain is loaded with source provenance, split by Markdown structure and
sentence boundaries, embedded by the configured `embed` route, and stored in pgvector. The
Postgres document store tracks source hashes for incremental upserts and deletions. Brain remains
read-only and frontmatter is not embedded. The loopback API retrieves top-k scored chunks with
provenance through LlamaIndex's vector retriever.

`POST /v1/knowledge/answer` shares `/query`'s question and `top_k` validation and retrieval.
One prompt numbers retrieved chunks `[1]` through `[k]` and instructs the `chat` route to answer
only from those chunks with `[n]` citations, or to refuse when they cannot answer. The question
and chunks are untrusted data, not instructions. Answers always refer to Aleix by name in the
third person. One LlamaIndex client call returns an internal JSON answer/refusal signal.
Sources expose number, title, path and score in first-citation order,
without duplicates. Missing or out-of-range citations and malformed replies fail closed to a
plain refusal: “That isn't covered in what Aleix has written here — you can ask him directly.”
A 60-second model timeout with no retries bounds the call. Only the question and chunk text
leave the node; logs contain question length, top-k, retrieved/cited counts, refusal
and elapsed time, never content. This contract works with any indexed corpus.

`homelab eval` measures corpus self-question hit@5 and MRR by stable provenance id, and
refusals through the answer endpoint's code path. It requires the about-Aleix collection and
prints only numbers and ids, without persisting an evaluation artifact. The optional
`HOMELAB_EVAL_FILE` defaults to `/data/corpus/about-aleix/eval.json` in the existing read-only
mount. Version-1 files add three independent sets: bank paraphrases, refusal checks and visitor
questions, the latter also rolled up by group without extra calls. Answer items measure retrieval
against any expected entry and require a non-refused answer citing an expected path for answer
correctness; refusal items require refusal. Retrieval and answer failures are recorded
independently by id and reason. The baseline JSON fields remain unchanged at the root; `sets`
holds additional scores and numeric `eval_file_found` reports presence. Missing files run only
the baseline. File validation errors exclude content, and all evaluations reuse the existing
retriever and answer path.
The baseline, each set and each visitor group include `first_person_count`, counting each
non-refused answer once if it contains whole-word `I`, `me`, `my` or `mine` (case-insensitive,
including quotes). Only counts are exposed, without text or match details.

## Endpoints and evaluation

`POST /v1/knowledge/query` and `POST /v1/knowledge/answer` accept `{"question": "…", "top_k": 5}`
(nonblank question; integer `top_k` at least 1, default 5). Query returns scored chunks with
provenance. Answer retrieves the same chunks and makes one call to the configured `chat` route.
It returns `answer`, `refused` and `sources`: each cited chunk's `number`, `title`, `path` and
`score`, in first-citation order. Insufficient evidence produces a plain refusal with no sources;
malformed model replies or missing/invalid citations also fail closed. The refusal text is
“That isn't covered in what Aleix has written here — you can ask him directly.” Answers always
refer to Aleix by name in the third person. Metrics record lengths, counts, refusal and timing,
without question, chunk or answer content.

Knowledge operations share `HOMELAB_ACTIVE_COLLECTION` (`about_aleix` by default, or `brain`).
Each collection has separate chunk and document tables; switching does not rebuild Brain.
The corpus is mounted read-only at `/data/corpus` from `HOMELAB_CORPUS_PATH`, defaulting to
`/srv/homelab/homelab-v2-data/originals/corpus`. `HOMELAB_CORPUS_FILE` defaults to `/data/corpus/about-aleix/corpus.json`.
Public JSON entries become documents whose titles are their questions and whose provenance
paths are stable entry ids. Other visibility values are skipped. Invalid individual entries
are skipped with position-only warnings; invalid JSON, a non-list root or duplicate public ids
abort ingestion before writing.

`homelab eval` reads the same corpus and asks every public entry's own question against
`about_aleix`. It prints only document count, hit@5, mean reciprocal rank (MRR), retrieval miss
ids, refusal count and refusal ids. Answers use the same path as the answer endpoint; no eval
file is written. When `HOMELAB_EVAL_FILE` (default
`/data/corpus/about-aleix/eval.json`) exists in the read-only corpus mount, it also scores
`bank_paraphrases`, `refusal_checks` and `visitor`, with visitor results per group. Answer items
measure hit@5 and MRR against any listed entry, and answer correctness requires a non-refusal
with a listed source path. Refusal items measure correct refusals. Failures carry ids and
`retrieval_miss`, `refused`, `wrong_citation` or `answered` reasons; multiple reasons can apply.
The baseline, each set and each visitor group also report `first_person_count`: non-refused
answers containing `I`, `me`, `my` or `mine` as whole words, ignoring case. Each answer counts
once, including quoted phrases; no text or match details are reported.
The baseline fields stay at the JSON root, independent scores are under `sets`, and numeric
`eval_file_found` is 0 when the file was not found (baseline only), 1 otherwise. Empty subsets
have zero rates. No question or answer text is printed. Run it inside the app container on the
node after ingestion.
