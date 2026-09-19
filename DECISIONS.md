# Decisions

Append-only. One paragraph per choice that was not obvious, newest last. Reversals are new entries.

**2026-09-18 — v2 is a clean repository, v1 is archived.** Ten days of v1 produced ~6k lines of
stdlib Python and ~44k lines of process documentation. The infrastructure was sound; the process
and the stdlib-only constraint were the cost. v2 keeps the server and the security posture, uses
mature libraries for machinery, and records decisions here instead of in ADRs.

**2026-09-18 — First slice is the Second Brain.** Questions over the owner's Markdown notes with
cited sources, from Telegram. It exercises models, knowledge and provenance, follows the RAG
certificate being taken, and is used daily.

**2026-09-18 — API-first model access; the v1 helper retires.** Vercel AI Gateway holds the
provider relationship, budget and dashboard. Local inference (llama.cpp on the node; later a GPU
node or the Mac) is one more OpenAI-compatible provider. The helper's credential isolation, fail-
closed governor and count caps were built for subscription CLIs and are replaced by one key in one
process plus the gateway's budget.

**2026-09-18 — Docker Compose wrapped in one systemd unit.** llama.cpp is an image rather than a
host build, services get a private network, and the same file runs on any Docker host. venv +
systemd would have been lighter for a single Python process on a single node forever, which this
is not.

**2026-09-18 — Postgres for jobs, vectors and app state; Procrastinate for jobs; LangGraph for
workflows.** v1's custom Run store (23.1A) is left in v1; its rules — persist before ack, never
blindly repeat effectful work — carry over. LangGraph enters with the first workflow that needs
interrupt/resume.

**2026-09-18 — LlamaIndex for ingestion and retrieval, LangGraph for orchestration.** Messy
sources are expected. The two never touch: LangGraph nodes call the knowledge service as a
function. LangChain retrieval classes and LlamaIndex agents/Workflows stay out.

**2026-09-18 — Public, minimal docs, not a teaching project.** README, this file, ARCHITECTURE and
CLAUDE.md. No guides, briefs or handovers; docs change when behaviour changes.

**2026-09-19 — The Telegram bot is the first client and stays a host service.** Its code moves
here; its unit and TPM-sealed token do not change. It moves into Compose when it is redesigned.

**2026-09-19 — Planning, research and history live outside this repository.** This repository
describes only what exists. Work arrives as self-contained task prompts; plans, research notes and
historical process artifacts are kept elsewhere and are not named here.
