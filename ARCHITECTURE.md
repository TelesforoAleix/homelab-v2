# Architecture

This document names the parts of Home Lab, what each part and library owns, how requests and
data move between them, where each part runs, and which other repositories depend on which
contract. Read it before changing a boundary, adding a part or a client, or deciding where new
code belongs. It describes the current state only; the reasons behind it are in
[DECISIONS.md](DECISIONS.md) and each part's detail is in the document its row names.

## Boundary

```
 Factory, other clients ── loopback API 127.0.0.1:8000 ── api ── Postgres + pgvector
 (run, decisions, knowledge)                              │      (default network only)
                                                         │ purposes, OpenAI-compatible HTTP
 Compose clients ── homelab-models ─┐                    ▼
 host bot ── 127.0.0.1:4000 ────────┼──────────────── LiteLLM gateway ── Vercel AI Gateway
 task containers ── homelab-tasks ──┘   (only holder      │  │            (hosted chat, images,
   (task token only)                     of gateway keys) │  │             video)
                                    llama-embed(-large) ──┘  └── Mac llama.cpp router (Tailscale)

 runs (no Docker socket) ── /run/homelab-launcher.sock ── launcher (root) ── task and CLI containers
 Factory runner and sessions ──────────┘                                   │
 task prepare ── homelab-packages ── package-proxy ── package indexes (HTTPS only)
```

Home Lab owns provider keys, the route table, knowledge provenance, job durability, execution
containment and the API contracts. Only the LiteLLM container holds the gateway keys; only the
`api` and LiteLLM containers publish ports, both on loopback.

## Parts

| Part | What it is | Runs | Detail |
|---|---|---|---|
| `api` | FastAPI app: knowledge, run, decisions, image-cost, management and health endpoints (`src/homelab/api/`) | Compose service on the node, `127.0.0.1:8000` | [run-api.md](docs/run-api.md), [decisions-api.md](docs/decisions-api.md) |
| `worker` | Procrastinate worker for the `local` and `default` queues (`src/homelab/jobs/`) | Compose service on the node | [Jobs](#jobs) |
| `postgres` | Postgres 17 with pgvector: knowledge, jobs, tasks, runs, accounting | Compose service on the node | [Data](#data) |
| `gateway` | LiteLLM with Home Lab's hooks; the route table is `config/litellm.yaml` | Compose service on the node, `127.0.0.1:4000` | [gateway.md](docs/gateway.md) |
| `embedding-servers` | `llama-embed` (Nemotron, `embed`) and `llama-embed-large` (Qwen3-Embedding-4B, `embed-large`) | Compose services on the node, CPU only | [gateway.md](docs/gateway.md) |
| `package-proxy` | tinyproxy allowing HTTPS to the package indexes for a task's prepare step | Compose service on the node | [runs.md](docs/runs.md#coding-tasks) |
| `knowledge` | LlamaIndex ingestion and retrieval over Brain and the corpus (`src/homelab/knowledge/`) | Inside `api`, `worker` and the CLI | [knowledge.md](docs/knowledge.md) |
| `cli` | The `homelab` command: `ingest`, `eval`, `routes`, `providers` (`src/homelab/cli.py`) | Inside the platform image | [knowledge.md](docs/knowledge.md) |
| `task-store` | Task, attempt, trace and run tables with their submit, claim and finish commands (`src/homelab/tasks/`) | Inside `api`, `runs` and the platform image | [runs.md](docs/runs.md#task-store) |
| `runs` | The execution service: tickets, slices, cuts, reviews and checks (`src/homelab/runs/`) | Compose service under the `runs` profile, uid 21020 | [runs.md](docs/runs.md) |
| `launcher` | Root socket helper that starts every agent container from fixed profiles | Host service on the node | [operations.md](docs/operations.md#install-the-fixed-fence-launcher) |
| `fence` | The sealed CLI image, its allow-list proxy and probe that the launcher builds on | Host files and containers on the node | [operations.md](docs/operations.md#install-the-cli-fence) |
| `deployer` | Root socket helper that installs application images from `main` with rollback | Host service on the node | [deployer.md](docs/deployer.md) |
| `host-runner` | `homelab-task-run` and `homelab-task-runner`, the stopped rollback execution path | Host scripts and a disabled unit | [runs.md](docs/runs.md#coding-tasks-in-a-container) |
| `telegram-bot` | python-telegram-bot service: commands, planning reads, inbox, order endpoint | Host service on the node, as `homelab-bot` | [telegram-bot.md](docs/telegram-bot.md) |
| `data-volumes` | The encrypted main and bulk volumes, their unlock script and `homelab-data.target` | Node host | [node.md](docs/node.md#data-volumes) |
| `notifier` | `homelab-notify.sh` and the units and helpers that send through it: failure alerts, watchdog, image-cost and queue notices | Node host | [node.md](docs/node.md#host-files-and-recovery) |
| `backups` | Daily database dumps, restore checks and the weekly encrypted originals copy | Node host | [operations.md](docs/operations.md#database-dumps-and-restore-checks) |
| `context-server` | On-demand Serena MCP container over SSH, with no network | Node host | [node.md](docs/node.md#code-context-server) |
| `mac-router` | Native llama.cpp router serving `vision` and `embed-large-bulk` | Owner's Mac, launchd | [mac.md](docs/mac.md#model-router) |
| `mac-profiles` | `homelab-profile`: Working and Inference profiles, model registry, calendar jobs and the job queue | Owner's Mac, launchd | [mac.md](docs/mac.md#usage-profiles) |
| `mac-listener` | Action listener on TCP 8091 that runs the originals copy | Owner's Mac, launchd | [mac.md](docs/mac.md#mac-action-listener) |
| `menu-bar` | SwiftUI menu bar client of `homelab-profile` (`mac/MenuBar`) | Owner's Mac | [mac.md](docs/mac.md#menu-bar-app-usage-and-night-window) |

## What each library owns

- **LiteLLM** owns purpose routing and the OpenAI-compatible model endpoint. Home Lab's hooks
  add the task-network policy, media routes, cost headers and accounting; they do not route.
- **LlamaIndex** owns parsing, chunking, embedding calls, storage and retrieval.
- **Procrastinate** owns job storage, queues, retries and locks for both the worker and `runs`.
- **python-telegram-bot** owns Telegram transport, polling and command registration.
- **llama.cpp** owns local model loading, idle sleep and native presets, on the node and the Mac.

## Request and data flow

- **Model call.** A client sends a purpose name as `model` to `/v1` on the gateway. The gateway
  checks the purpose and the caller's network, calls Vercel AI Gateway, a node embedding
  server or the Mac router, and writes one content-free audit line. Clients learn purposes from
  `GET /v1/models`; no application names a provider or model.
- **Knowledge answer.** `POST /v1/knowledge/answer` embeds the question through `embed`,
  retrieves chunks from pgvector and asks `chat` to answer only from them, with citations.
- **Run.** `POST /v1/runs` validates the request against the caller's run key, writes the run
  and its task in one transaction and defers a Procrastinate job. `runs` claims the job, asks
  the launcher for each container (clone, prepare, the Pi or CLI worker, check, commit), then
  pushes through the GitHub App. Clients read progress with `GET /v1/runs`.
- **Decision.** `POST /v1/decisions` sends shared text and questions to the provider adapter,
  aggregates the repeats and records content-free accounting under a monthly cap.
- **Media.** Image calls go through LiteLLM; video jobs are Home Lab middleware in the gateway.
  Both add charges to `homelab_image_costs`, which `GET /v1/images/cost` reads per bearer key.
- **Mac offer.** The worker's periodic job reads the Mac's published offer every minute and
  stores it in `homelab_providers`; `homelab providers` shows it.
- **Deploy.** The platform is deployed by hand (`git pull --ff-only`, then restarting
  `homelab.service`). Factory and other applications are installed by the deployer.

## Jobs

Procrastinate persists jobs in Postgres before they are acknowledged. The worker serves the
`default` queue (`ping`, the provider offer read) and the `local` queue with concurrency 1
(`ingest_brain`, one local-model job at a time). `run_coding_task` is registered on the
`tasks` queue so its jobs are valid; `runs` consumes that queue, never the worker. Whole-run
work uses the separate `runs` queue.

## Data

Postgres holds the knowledge tables of each collection, Procrastinate's schema, the task and
run tables, `homelab_image_costs`, `homelab_video_jobs`, `homelab_decision_calls` and
`homelab_providers`. The API applies its accounting migrations at startup; the worker applies
the task schema before it starts. Factory keeps its own database in the same cluster, so the
daily dump covers both. Derived data is rebuilt from sources; irreplaceable inputs live under
`/srv/homelab/<project>-data/originals/` and are copied weekly.

## Where it runs

- **The node** (a small Linux server) runs the Compose stack under `homelab.service`, which
  starts after the encrypted volume is unlocked and stops with it, plus host services: the
  launcher, the deployer, the Telegram bot, the watchdog, the notifier and the timers.
  `node/` holds these host files at their installed paths.
- **The owner's Mac** runs the model router, usage profiles, job queue, action listener and
  menu bar app from a private installed copy of `mac/`. The node reaches it over Tailscale.
- The owner reaches the node only over the private network: `ssh homelab` is the owner's
  session and `ssh homelab-agent` the agent's.

## Other repositories

| Repository | Direction | Contract |
|---|---|---|
| [Factory](https://github.com/TelesforoAleix/factory) | calls the run API and the decisions API with its run key | [run-api](docs/contracts.md#run-api), [decisions-api](docs/contracts.md#decisions-api) |
| Factory | its runner and sessions containers call the launcher socket | [launcher-socket](docs/contracts.md#launcher-socket) |
| Factory | its runner asks the deployer to install Factory after each merge | [deployer](docs/contracts.md#deployer) |
| Factory | reads this repository's `.github/change-classes` before a merge | [change-classes](docs/contracts.md#change-classes) |
| Factory | collects queued Telegram orders from the bot's order endpoint | [bot-orders](docs/contracts.md#bot-orders) |
| [med-ask](https://github.com/TelesforoAleix/med-ask) | calls the gateway over `homelab-models` | [model-gateway](docs/contracts.md#model-gateway) |
| image studio | calls the gateway's image and video routes on loopback | [media](docs/contracts.md#media) |
| [mac-dictation](https://github.com/TelesforoAleix/mac-dictation) | owns its `dictation-backup` account on the node | none here |

## Rules

1. Secrets never in git or images — Compose secrets or systemd `LoadCredential=`;
   the decisions provider key is supplied only through the API's private environment.
2. Only `api` and LiteLLM publish container ports, on loopback. The host bot's order
   listener is the explicit exception: runtime Tailscale IPv4, TCP 8110, reachable from
   the tailnet under Tailscale policy and from `homelab_default` (`172.18.0.0/16`) under
   a ufw input rule. The Mac host listener on 8091 remains gated by Tailscale policy.
3. Brain is mounted read-only; everything derived lives on the encrypted volume.
4. Containers run as non-root with `cap_drop: ALL`.
5. Content is not logged by default; ids, sizes, timings and costs are.
6. Spend control is the gateway key's budget and dashboard; typed decisions have their own
   API-enforced monthly cap and content-free accounting.
7. Jobs: persist before ack; idempotent or non-retryable.
8. Indexing is local (node, or the owner’s Mac for explicit bulk embedding calls); knowledge
   answering sends only the question and retrieved chunks off-node.
   Explicit vision calls send page images to the owner’s Mac or the selected hosted vision rung.
