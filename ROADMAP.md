# Roadmap

## Now — Slice 1: "Ask my Brain"

Done when: `/ask` on Telegram answers with cited notes; `uv run eval` prints hit@5; the stack
returns after reboot + unlock; v1's helper, harness and workbench units are stopped.

| Step | Builds | Runs on | Observable |
|---|---|---|---|
| [x] 1 | Repo bootstrap — pyproject, Dockerfile, compose, `homelab.service`, docs, route table | Mac | `uv run pytest` |
| [ ] 3a | `knowledge/ingest` — Brain Markdown → chunks with provenance → vector store; incremental | Mac, in-memory doubles | tests |
| [ ] 3b | First deploy of the stack to the node, next to v1; ingest the real Brain | Node | `homelab ingest` counts; rows in Postgres |
| [ ] 4 | `POST /v1/knowledge/query` — top-k chunks + provenance, no model call | Node | `curl` |
| [ ] 5 | `eval/` — 15–20 known-answer questions; `homelab eval` prints hit@5. **Read the number before 6** | Node | a number |
| [ ] 6 | `POST /v1/knowledge/answer` — RAG with citations via `chat` | Node; first gateway call | `curl` |
| [ ] 7 | Telegram `/ask` → `answer` | Node | your phone |
| [ ] 8 | `homelab.service` proven across reboot + unlock; the three v1 units stopped | Node | `systemctl`, a reboot |

Each step is built and unit-tested on the Mac, committed, deployed with `git pull && docker compose
up -d --build` on the node, and verified there.

### Transition from v1 on the node

- **Coexistence (now → step 7).** v1 keeps serving. v2 runs beside it: its own containers, port
  8000 on loopback, new directories on the volume (`homelab-v2`, `postgres`, `models`). Nothing v1
  owns is touched.
- **The switch (step 7).** The bot's `/ask` is pointed at the v2 API — the only change to a v1
  component; reversible by pointing it back. The helper and harness lose their last client.
- **Retirement (step 8, after a few days of v2 answering).** `systemctl disable --now` on
  `homelab-model-helper.socket`, `homelab-harness.service`, `homelab-workbench.service`. Their
  files stay as rollback until a later cleanup. Nothing on the encrypted volume is deleted.

## Next, in likely order

local reranker · hybrid search (pgvector + Postgres FTS) · nightly ingest schedule · first batch
job with a local chat model · first LangGraph graph (agentic RAG, interrupt/resume) · Second Brain
web UI (own repo) · Factory as a client · knowledge service as an MCP server · the Mac as an
inference node over Tailscale

## Parked

Factory (until it is a client) · durable Runs beyond Procrastinate · auth (until a client is
off-node) · TPM auto-unlock of the volume · anything Kubernetes-shaped
