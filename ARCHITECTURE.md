# Architecture

## Boundary

```
                              │  HTTP, 127.0.0.1:8000
┌─────────────────────────────▼───────────────────────────────────────────────┐
│  HOME LAB API                                                                │
│  models/     purpose → provider+model; the only holder of provider keys      │
│  knowledge/  Markdown → structured chunks → embeddings → pgvector            │
│  jobs/       durable tasks (Procrastinate)                                   │
└──────┬──────────────────────────┬─────────────────────────┬──────────────────┘
       │ OpenAI-compatible        │                          │
  Vercel AI Gateway         llama-server (this node;      Postgres + pgvector
  (hosted models)           local embeddings)             (jobs)
```

Home Lab owns provider keys, the route table, knowledge provenance, job durability and the API
contract. LlamaIndex owns parsing, chunking, embedding, storage and retrieval; Procrastinate owns
the job machinery.

## Deployment

One `compose.yaml`: `api`, `worker`, `postgres` (pgvector image), `llama-embed` (llama.cpp
server). Only `api` publishes a port, on loopback. `node/etc/systemd/system/homelab.service`
wraps the stack so it starts after the encrypted volume is unlocked (`homelab-data.target`) and
stops with it. Brain is bind-mounted read-only; Postgres data and model files live on the volume.

Volume users declare `ConditionPathIsMountPoint=/srv/homelab`, `After=homelab-data.target`,
`PartOf=homelab-data.target` and `WantedBy=homelab-data.target`. They do not require that target
or place `WorkingDirectory=`, `RootDirectory=` or `StateDirectory=` on the volume: those
directories create implicit mount dependencies before conditions are checked, turning a locked
volume into a failure alert. The stack instead passes Compose the absolute `-f` path; its
project name and relative paths come from `compose.yaml`.

`node/` mirrors host paths and records the volume unlock script and target, failure notifier,
boot watchdog, stack unit and agent sudo grant. The notifier's `homelab` alias alerts for the
v2 stack alongside the five existing aliases. Host files are installed as `root:root` from the
node's clone of merged `main`, after backups and visible diffs, with mode `0644` for units and
drop-ins, `0755` for scripts and `0440` for sudoers. Unit changes require `daemon-reload` and
validation on the node. The agent has root through sudo; the owner approves execution plans
and risks. The service restart pulls `main` and rebuilds containers, while host-file installation
is explicit. Bot host configuration and secret credential contents are outside this tree.

The embedding model is the only resident local model. The `local` queue has concurrency 1.

## Routes

`config/routes.yaml` binds the `chat` and `embed` purposes to providers and models. Unknown
purposes are refused, never defaulted. Both providers expose OpenAI-compatible APIs.

## Jobs

The Procrastinate app persists jobs in Postgres. Its `ping` task runs on the default queue;
`ingest_brain` runs on the concurrency-1 `local` queue and is not scheduled.

## Knowledge

Included Markdown from Brain is loaded with source provenance, split by Markdown structure and
sentence boundaries, embedded by the configured `embed` route, and stored in pgvector. The
Postgres document store tracks source hashes for incremental upserts and deletions. Brain remains
read-only and frontmatter is not embedded. The loopback API retrieves top-k scored chunks with
provenance through LlamaIndex's vector retriever.

## Rules

1. Secrets never in git or images — Compose secrets or systemd `LoadCredential=`.
2. Only `api` publishes a port, on loopback.
3. Brain is mounted read-only; everything derived lives on the encrypted volume.
4. Containers run as non-root with `cap_drop: ALL`.
5. Content is not logged by default; ids, sizes, timings and costs are.
6. Spend control is the gateway key's budget and dashboard.
7. Jobs: persist before ack; idempotent or non-retryable.
8. Indexing is local; only the question and retrieved chunks leave the node.
