# Working in this repository

Read `README.md` and `ARCHITECTURE.md` first; `DECISIONS.md` when a choice looks odd.

- This repository describes what exists. Work arrives as self-contained task prompts; do not look
  for plans, roadmaps or research here or elsewhere.
- Work in slices that end with something runnable.
- `README`/`ARCHITECTURE` change when behaviour changes. Append to `DECISIONS.md` when a choice
  was not obvious. Write nothing else: no briefs, handovers, ADR files or phase numbers.
- Libraries own machinery (LlamaIndex, LangGraph, Procrastinate). Do not hand-roll a retriever,
  a graph runtime, a queue or a tool protocol.
- Applications ask for a purpose from `config/routes.yaml`; never name a provider or model in code.
- The eight rules in `ARCHITECTURE.md` are not optional. In particular: no secrets in git or
  images; loopback only; do not log content by default.
- `uv run pytest` before every commit.
- The node is reached as `ssh homelab-agent`, which has root through sudo; `ssh homelab` is the
  owner's session. Deployment is `sudo systemctl restart homelab.service`; the unit pulls
  `main` and builds images.
- Proceed without asking unless your plan includes a step that cannot be undone or carries a
  real security risk: deleting data or volumes, wiping or repartitioning a disk, touching a
  secret or the volume's encryption, a change that could leave the node unreachable, rewriting
  `main`'s history or loosening its protection, or a security concern the task prompt does not
  address. For those steps only, stop before taking them, say what each risks and how it would
  be recovered, and wait for the owner's go. Report your plan and what you did in the handover.
- Files under `node/` are installed at the same path on the node, from the committed tree. Show
  the `diff` against what is installed before copying, and run the validator a change has:
  `visudo -c`, `sshd -t`, `systemd-analyze verify`.
