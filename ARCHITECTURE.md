# Architecture

## Boundary

```
                 Loopback API (127.0.0.1:8000)
                     │ knowledge and jobs
                api / worker / CLI
                     │ OpenAI-compatible HTTP, purpose names
Other Compose clients ── homelab-models ── LiteLLM (no published port)
                                            │ default network
                              ┌─────────────┴──────────────┐
                       Vercel AI Gateway            llama-embed
                         (hosted chat)           (local embeddings)

api / worker ── Postgres + pgvector (private default network)
```

Home Lab owns provider keys, the route table, knowledge provenance, job durability and the API
contract. LiteLLM owns purpose routing and the OpenAI-compatible model endpoint; only its
container holds the gateway key. LlamaIndex owns parsing, chunking, embedding, storage and
retrieval; Procrastinate owns the job machinery.

## Deployment

One `compose.yaml`: `api`, `worker`, `postgres` (pgvector image), `llama-embed` (llama.cpp
server) and `litellm`. Only `api` publishes a port, on loopback.
`node/etc/systemd/system/homelab.service` wraps the stack so it starts after the encrypted
volume is unlocked (`homelab-data.target`) and
stops with it. Brain and the corpus are bind-mounted read-only; Postgres data and model files
live on the volume.

Volume users declare `ConditionPathIsMountPoint=/srv/homelab`, `After=homelab-data.target`,
`PartOf=homelab-data.target` and `WantedBy=homelab-data.target`. They do not require that target
or place `WorkingDirectory=`, `RootDirectory=` or `StateDirectory=` on the volume: those
directories create implicit mount dependencies before conditions are checked, turning a locked
volume into a failure alert. The stack instead passes Compose the absolute `-f` path; its
project name and relative paths come from `compose.yaml`.

`node/` mirrors host paths and records the volume unlock script and target, failure notifier,
boot watchdog, stack unit and agent sudo grant. The notifier accepts only `bot`, `watchdog` and
`homelab`, mapping them to the bot, watchdog and v2 stack units. Retired v1 programs, units,
configuration, state, accounts and checkouts have been removed from the node; Brain and v2's
volume data remain. Host files are installed as `root:root` from the
node's clone of merged `main`, after backups and visible diffs, with mode `0644` for units and
drop-ins, `0755` for scripts and `0440` for sudoers. Unit changes require `daemon-reload` and
validation on the node. Bot Python files and the polkit rule also use `0644`. The agent has root
through sudo; the owner approves execution plans
and risks. The service restart pulls `main` and rebuilds containers, while host-file installation
is explicit. Bot code, its unit, credential and failure drop-ins, and the narrow polkit grant
are recorded here. Private allowlists and secret credential contents stay outside this tree.

Base SSH, ufw, Docker, sysctl, console timeout and Wi-Fi configuration also live in `node/`;
their installed modes are recorded in `AGENTS.md`. Recovery is the
[Rebuilding the node](README.md#rebuilding-the-node) procedure: no node backup, a public code
clone, Brain and corpus copies from the owner's Mac, re-ingestion and re-issued secrets.
The rebuilt node needs no GitHub credential. Only the Samsung system disk is rebuilt; the
deliberately unused Micron disk stays untouched.

The Telegram client remains a stdlib-only host service, running as `homelab-bot` with its
existing sandbox and TPM-sealed token. It long-polls Telegram with no listening port. The main
allowlist gates dispatch; the privileged allowlist is a subset and gates `/restart`, which also
checks its unit allowlist. Polkit independently permits only restarting `chrony.service`.
`/ask` posts to the loopback answer API, returns only text and never dispatches model output.
Host metrics, restart and help do not depend on the knowledge service or unlocked volume.
Answers include numbered source titles, refusals omit sources, and replies are split within
Telegram's length limit. The router also supplies startup `setMyCommands` and `setMyDescription`;
registration failures are nonfatal.
User IDs remain audit metadata in the node's journal; `/ask` logs its argument length only.
Handler errors log the registered command and exception type, never exception text.

The embedding model is the only resident local model. The `local` queue has concurrency 1.

## Routes

`config/litellm.yaml` is the single route table. It offers `chat`, `chat:high` and `chat:xhigh`
through Vercel AI Gateway's `deepseek/deepseek-v4.1-flash`, at low, medium and high reasoning
effort respectively, and `embed` through the unchanged local `llama-nemotron-embed-1b-v2`.
Names are `purpose:tier`; a bare purpose is standard, and embeddings have no tiers. Clients
choose tiers themselves. There is no wildcard, pass-through, default model or fallback.
Unknown names, deployment model ids and unavailable tiers receive LiteLLM's default
OpenAI-shaped 4xx and never reach a provider. The pre-call hook checks against the same config
and enforces each alias's configured effort; middleware blocks provider/routing overrides.

Every application, including this repository's API, worker, CLI and eval, knows only
`HOMELAB_MODELS_BASE_URL` (default `http://litellm:4000/v1`), a client key from the environment,
and purposes discovered from `GET /v1/models`. LlamaIndex's OpenAI-compatible clients send
those purposes as `model`. Chat responses report the alias. A post-call hook sets embedding
responses' `model` to the configured real model; clients record it at ingest. The pinned
release's raw-model metadata switch prevents LiteLLM overwriting that value with `embed`.
Moving embeddings through this proxy does not change the embedding model or indexed data.

Other Compose projects attach trusted clients to `homelab-models`. Only LiteLLM joins that
named network, alongside the private default network it uses to reach `llama-embed`.
Postgres, API and `llama-embed` do not join the client network. LiteLLM publishes no host port.
The key sent by a client is accepted and ignored; there is no master key, database, UI or
virtual key configuration. HTTP middleware exposes only the three `/v1` model endpoints and
liveness; management endpoints and LiteLLM-specific request fields are refused. Provider
headers are removed and upstream errors are reduced to status and a generic message. `/v1`
only grows through new purposes and optional fields; incompatible changes go under `/v2`.

The official BerriAI image is pinned by digest, runs as `10001:10001`, drops all capabilities
and uses `no-new-privileges`. Only LiteLLM mounts `/run/secrets/gateway_api_key`, still
`root:10001` `0440`. Its entrypoint reads the file into the process environment at startup;
no gateway key appears in Compose, env files or config. Telemetry, third-party callbacks,
admin UI and remote model-price fetching are disabled; tokenizer assets are bundled and
remote image URLs are refused. The only external model destination is the gateway; the local
embedding destination is unchanged. Tests run the real pinned image without external
networking, synthetic credentials and loopback upstreams, checking for attempted external
connections as well as the endpoint contract.

The journald audit line contains purpose, configured real model, HTTP status, total elapsed
seconds (through the end of a stream) and upstream-attempt status. Unknown aliases are logged
as `unknown` so arbitrary client strings cannot become content in the journal. Default vendor
and access logging is disabled because errors can contain bodies, prompts or credentials.
No hosted observability callbacks are configured. `/health` keeps `status`, `version` and
`routes`, and replaces `gateway_key_present` with `models_base_url`.

## Jobs

The Procrastinate app persists jobs in Postgres. Its `ping` task runs on the default queue;
`ingest_brain` retains its durable task name but ingests the active collection on the
concurrency-1 `local` queue and is not scheduled.

## Knowledge

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

## Rules

1. Secrets never in git or images — Compose secrets or systemd `LoadCredential=`.
2. Only `api` publishes a port, on loopback.
3. Brain is mounted read-only; everything derived lives on the encrypted volume.
4. Containers run as non-root with `cap_drop: ALL`.
5. Content is not logged by default; ids, sizes, timings and costs are.
6. Spend control is the gateway key's budget and dashboard.
7. Jobs: persist before ack; idempotent or non-retryable.
8. Indexing is local; only the question and retrieved chunks leave the node.
