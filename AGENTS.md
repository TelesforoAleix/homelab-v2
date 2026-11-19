# Working in this repository

Home Lab v2 is a self-hosted applied-AI platform: a model gateway, knowledge retrieval, durable
jobs, contained agent execution and the host files of the node it runs on, plus the tools on
the owner's Mac. Factory and other applications build on its contracts.

## CI commands

CI (`.github/workflows/ci.yml`) runs these; run the Python ones before every commit:

```bash
uv sync --group benchmark --group telegram-bot
uv run ruff check .
uv run ruff format --check .
uv run pytest
docker build -t homelab:ci .
swift build --package-path mac/MenuBar -c release
```

CI also runs `shellcheck` on `node/usr/local/sbin/*.sh`, validates the Docker and containerd
configuration, checks the bot requirements on Python 3.14, smoke-tests the pinned LiteLLM
image offline, scans for secrets with gitleaks, and builds the fence images.

## Hard rules

- This repository describes what exists. Work arrives as self-contained task prompts; do not
  look for plans, roadmaps or research here or elsewhere.
- Work in slices that end with something runnable.
- Libraries own machinery (LlamaIndex, LangGraph, Procrastinate). Do not hand-roll a retriever,
  a graph runtime, a queue or a tool protocol.
- Applications ask for a purpose from the route table, `config/litellm.yaml`; never name a
  provider or model in code.
- The eight rules in `ARCHITECTURE.md` are not optional. In particular: no secrets in git or
  images; loopback only; do not log content by default.
- `uv run pytest` before every commit.
- The node is reached as `ssh homelab-agent`, which has root through sudo; `ssh homelab` is the
  owner's session. Deploy by running `git pull --ff-only` by hand in `/srv/homelab/homelab-v2`,
  then `sudo systemctl restart homelab.service` to build images.
- Proceed without asking unless your plan includes a step that cannot be undone or carries a
  real security risk: deleting data or volumes, wiping or repartitioning a disk, touching a
  secret or the volume's encryption, a change that could leave the node unreachable, rewriting
  `main`'s history or loosening its protection, or a security concern the task prompt does not
  address. For those steps only, stop before taking them, say what each risks and how it would
  be recovered, and wait for the owner's go. Report your plan and what you did in the handover.
- Files under `node/` are installed at the same path on the node, from the committed tree. Show
  the `diff` against what is installed before copying, and run the validator a change has:
  `visudo -c`, `sshd -t`, `systemd-analyze verify`. Modes and steps are in
  [docs/host-files.md](docs/host-files.md).
- A PR changing `node/` updates the ["Rebuilding the node" section](docs/operations.md#rebuilding-the-node)
  in the same PR.
- This repository is public: no host addresses, tailnet names, private paths, secret values or
  private repository links in any file, PR or log.

## Document map

When you do the first thing, read the document, and keep it current in the same PR.

- Start any work, or change these rules → [AGENTS.md](AGENTS.md)
- Change a boundary, add a part or a client, or move code between parts → [ARCHITECTURE.md](ARCHITECTURE.md)
- Change anything another system calls or reads → [docs/contracts.md](docs/contracts.md)
- Take a step on the node: unlock, deploy, health, rollback, dumps, rebuild, copy → [docs/operations.md](docs/operations.md)
- Change or install a file under `node/` → [docs/host-files.md](docs/host-files.md)
- Change a fact in a core document, or its cited code → [docs/claims.md](docs/claims.md) and [docs/claims.json](docs/claims.json)
- Make a choice that was not obvious → append to [DECISIONS.md](DECISIONS.md); never edit old entries
- Change a purpose, a gateway hook or a media model → [docs/gateway.md](docs/gateway.md)
- Change the run API's wire format or run keys → [docs/run-api.md](docs/run-api.md)
- Change the `runs` service, the task store or the rollback host runner → [docs/runs.md](docs/runs.md)
- Change the launcher or the CLI fence → [docs/launcher.md](docs/launcher.md)
- Change the decisions API → [docs/decisions-api.md](docs/decisions-api.md)
- Change the application deployer or its manifests → [docs/deployer.md](docs/deployer.md)
- Change the Telegram bot, its inbox or its order endpoint → [docs/telegram-bot.md](docs/telegram-bot.md)
- Change knowledge ingestion, retrieval or `homelab eval` → [docs/knowledge.md](docs/knowledge.md)
- Change the volumes, the originals copy or the code context server → [docs/node.md](docs/node.md)
- Change anything under `mac/` or run a Mac step → [docs/mac.md](docs/mac.md)
- Change a Pi agent configuration → [configurations/README.md](configurations/README.md)
- Change what the visitor-facing summary says runs today → [README.md](README.md) and [docs/platform.md](docs/platform.md)

## Update rule

A change that alters a fact updates the document that owns it, in the same PR. A change to a
contract in `docs/contracts.md` is declared in the PR request's `contract_changes`.

Every document opens with what it covers and when to read it. Record each fact once, in its
owning document, and point to it elsewhere; do not restate what code, configuration or
`--help` already shows. Documents describe the current state; history belongs to git and
`DECISIONS.md`. `uv run pytest` includes `tests/test_docs_links.py`, which checks this map,
every relative link and anchor, and the paths and lines `docs/claims.json` cites.
