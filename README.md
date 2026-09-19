# Home Lab v2

A small applied-AI platform that applications build on: **model access** (one place that holds
provider keys and maps purposes to models), **knowledge** (ingest messy sources, retrieve with
provenance) and **durable jobs** (work that survives a reboot and can wait). Applications —
a Telegram bot, a Second Brain UI, an autonomous dev team — are clients of its API, never part of it.

It runs on one inexpensive always-on node (a used Lenovo M700 Tiny) with Docker Compose, uses
[Vercel AI Gateway](https://vercel.com/ai-gateway) for hosted models and `llama.cpp` for local
embeddings, reranking and quality-first batch work. A company could take it, point it at its own
providers, sources and infrastructure, and build its own applications against the same API.

This is the second attempt. [`homelab-v1`](https://github.com/TelesforoAleix/homelab-v1) is the
archived, learning-first build that taught the layers by hand; v2 keeps its security posture and
its server, uses mature libraries for the machinery, and optimises for building useful things.

## Status

The FastAPI service exposes health and configured model routes. Procrastinate provides a durable
Postgres-backed worker, and Docker Compose defines the loopback-only stack.

## Run it

On the node (Docker, an unlocked data volume at `/srv/homelab`, a clone of this repo there):

```bash
mkdir -p secrets && printf '%s' "$GATEWAY_KEY" > secrets/gateway_api_key
cp .env.example .env            # paths and the database password
docker compose up -d --build
curl -s 127.0.0.1:8000/health
```

`config/homelab.service` starts the stack after the volume is unlocked and stops it with the
volume; install it once with `sudo cp config/homelab.service /etc/systemd/system/ && sudo systemctl enable homelab`.

On a laptop, for development:

```bash
uv sync && uv run pytest          # the Python side, no services needed
docker compose up -d postgres llama-embed   # the services, if Docker is installed
uv run uvicorn homelab.api.app:app --reload
```

## Layout

| Path | What |
|---|---|
| `src/homelab/api/` | FastAPI — the one published port |
| `src/homelab/models/` | the route table: purpose → provider + model, and framework clients built from it |
| `src/homelab/jobs/` | Procrastinate tasks and the worker |
| `config/routes.yaml` | which model serves which purpose |

[ARCHITECTURE.md](ARCHITECTURE.md) has the boundaries and the rules. [DECISIONS.md](DECISIONS.md)
records choices that weren't obvious. MIT licence.
