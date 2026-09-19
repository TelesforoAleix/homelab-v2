# Working in this repository

Read `README.md`, `ARCHITECTURE.md` and `ROADMAP.md` first; `DECISIONS.md` when a choice looks odd.

- Work in slices that end with something runnable. Update `ROADMAP.md` checkboxes as you go.
- `README`/`ARCHITECTURE` change when behaviour changes. Append to `DECISIONS.md` when a choice
  was not obvious. Write nothing else: no briefs, handovers, ADR files or phase numbers.
- Libraries own machinery (LlamaIndex, LangGraph, Procrastinate). Do not hand-roll a retriever,
  a graph runtime, a queue or a tool protocol.
- Applications ask for a purpose from `config/routes.yaml`; never name a provider or model in code.
- The eight rules in `ARCHITECTURE.md` are not optional. In particular: no secrets in git or
  images; loopback only; do not log content by default.
- `uv run pytest` before every commit. Retrieval changes also run `uv run eval`.
- The node is reached as `ssh homelab-agent` for deployment; `ssh homelab` is the owner's session.
  Deployment is `git pull && docker compose up -d --build` in `/srv/homelab/homelab-v2`.
