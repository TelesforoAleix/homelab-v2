# Roadmap

## Now — Slice 1: "Ask my Brain"

Done when: `/ask` on Telegram answers with cited notes; `uv run eval` prints hit@5; the stack
returns after reboot + unlock; v1's helper, harness and workbench units are stopped.

- [x] 1. Repo bootstrap — pyproject, Dockerfile, compose, `homelab.service`, docs
- [ ] 2. `models/` — route table and client factory *(done with 1; verified against the gateway in 6)*
- [ ] 3. `knowledge/ingest` — Brain Markdown → chunks with provenance → pgvector; incremental
- [ ] 4. `POST /v1/knowledge/query` — top-k chunks, no model call
- [ ] 5. `eval/` — known-answer questions, hit@5. **Read the number before step 6**
- [ ] 6. `POST /v1/knowledge/answer` — RAG with citations
- [ ] 7. Telegram `/ask` → `answer`
- [ ] 8. Deploy to the node; retire the three v1 units

## Next, in likely order

local reranker · hybrid search (pgvector + Postgres FTS) · nightly ingest schedule · first batch
job with a local chat model · first LangGraph graph (agentic RAG, interrupt/resume) · Second Brain
web UI (own repo) · Factory as a client · knowledge service as an MCP server · the Mac as an
inference node over Tailscale

## Parked

Factory (until it is a client) · durable Runs beyond Procrastinate · auth (until a client is
off-node) · TPM auto-unlock of the volume · anything Kubernetes-shaped
