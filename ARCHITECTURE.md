# Architecture

## Boundary

```
                CLIENTS  (own their users, UI, conversation state, domain)
   Telegram bot        Second Brain UI        Factory        anything with an HTTP client
        └─────────────────────┬──────────────────────┴──────────────┘
                              │  HTTP, 127.0.0.1:8000  (Tailscale + per-client keys when a client lives off-node)
┌─────────────────────────────▼───────────────────────────────────────────────┐
│  HOME LAB API                                                                │
│  models/     purpose → provider+model; the only holder of provider keys      │
│  knowledge/  ingest → chunk → embed → index; retrieve with provenance        │
│  jobs/       durable tasks, schedules, retries, chaining (Procrastinate)     │
│  workflows   LangGraph graphs when a task needs interrupt/resume (later)     │
└──────┬──────────────────────────┬─────────────────────────┬──────────────────┘
       │ OpenAI-compatible        │                          │
  Vercel AI Gateway         llama-server (this node;      Postgres + pgvector
  (hosted models)           later: a GPU node, the Mac)   (jobs · app tables · vectors)
                                                                 ▲
                                                 Brain (Markdown, read-only mount) and other sources
```

Home Lab owns: provider keys and the route table, the knowledge pipeline and the provenance shape,
job durability, the API contract. Libraries own the machinery: LlamaIndex for ingestion and
retrieval, LangGraph for multi-step workflows, Procrastinate for jobs. Clients own everything about
their users. Brain owns *what is known*; Home Lab owns *how it is processed and retrieved*.

## Deployment

One `compose.yaml`: `api`, `worker`, `postgres` (pgvector image), `llama-embed` (llama.cpp
server). Only `api` publishes a port, on loopback. `config/homelab.service` wraps the stack so it
starts after the encrypted volume is unlocked (`homelab-data.target`) and stops with it. Brain is
bind-mounted read-only; Postgres data and model files live on the volume.

Concurrency on the node: the embedding model is the only resident local model; anything else loads
on demand for a batch job, one at a time (`local` queue, concurrency 1). Context default 8K.

## Routes

`config/routes.yaml`: applications ask for a *purpose* (`chat`, `embed`, later `rerank`,
`chat-strong`, `batch`); the file binds each to a provider and a model. Unknown purposes are
refused, never defaulted. Every provider is an OpenAI-compatible base URL, so a new inference node
is one entry. The `embed` route is pinned by consequence: the index is built with it, changing it
means reindexing.

## Knowledge

Markdown in Brain is the truth; every index is a rebuildable derived view. Retrieval starts
structured (headings, frontmatter), then vector, then hybrid, then anything graph-shaped — and each
step is taken only when the known-answer set in `eval/` says the previous one fails. Retrieval
results carry provenance: source path, heading path, revision, score. What leaves the node is the
question and the retrieved chunks; indexing is local.

## Jobs

A job is persisted before it is acknowledged. Tasks are idempotent or non-retryable; a task in
flight when the node dies is retried or flagged, never silently completed. Results are stored and
delivered by callback or fetch. Telegram holds undelivered messages for 24 h, so an outage loses
nothing that was sent to the bot.

## Rules

1. Secrets never in git or images — Compose secrets or systemd `LoadCredential=`.
2. Only `api` publishes a port, on loopback until a client lives off-node.
3. Brain is mounted read-only; everything derived lives on the encrypted volume.
4. Containers run as non-root with `cap_drop: ALL`.
5. Content is not logged by default; ids, sizes, timings and costs are.
6. Spend control is the gateway key's budget and dashboard.
7. Jobs: persist before ack; idempotent or non-retryable.
8. Indexing is local; only the question and retrieved chunks leave the node.
