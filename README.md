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

The FastAPI service exposes health, an ingestion trigger and top-k Brain retrieval with provenance.
`homelab ingest` also indexes included Brain Markdown incrementally. Procrastinate provides a
durable Postgres-backed worker, and Docker Compose defines the loopback-only stack.

## Run it

On the node, unlock `/srv/homelab`, connect with `ssh homelab`, and run this owner setup once.
Enter the gateway key and a random database password only at their hidden prompts.

```bash
sudo git clone https://github.com/TelesforoAleix/homelab-v2.git /srv/homelab/homelab-v2
sudo install -d -m 0700 /srv/homelab/homelab-v2/secrets
sudo bash -c 'umask 077; read -r -s -p "gateway key: " K; printf "%s" "$K" > /srv/homelab/homelab-v2/secrets/gateway_api_key; echo'
sudo chown 10001:10001 /srv/homelab/homelab-v2/secrets/gateway_api_key && sudo chmod 0400 /srv/homelab/homelab-v2/secrets/gateway_api_key
sudo bash -c 'umask 077; read -r -s -p "database password: " P; echo; test -n "$P" && test "$P" != "<RANDOM-PASSWORD>" || exit 1; printf "%s\n" "HOMELAB_DB_PASSWORD=$P" "HOMELAB_BRAIN_PATH=/srv/homelab/brain" "HOMELAB_POSTGRES_PATH=/srv/homelab/postgres" "HOMELAB_MODELS_PATH=/srv/homelab/models" > /srv/homelab/homelab-v2/.env'
sudo install -d /srv/homelab/postgres /srv/homelab/models
sudo visudo -cf /srv/homelab/homelab-v2/config/sudoers.d/homelab-agent-v2 && sudo install -m 0440 /srv/homelab/homelab-v2/config/sudoers.d/homelab-agent-v2 /etc/sudoers.d/homelab-agent-v2
sudo usermod -aG systemd-journal homelab-agent
sudo cp /srv/homelab/homelab-v2/config/homelab.service /etc/systemd/system/homelab.service
sudo systemctl daemon-reload && sudo systemctl enable --now homelab.service
```

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
| `src/homelab/knowledge/` | Markdown ingestion and retrieval with provenance through LlamaIndex |
| `src/homelab/jobs/` | Procrastinate tasks and the worker |
| `config/routes.yaml` | which model serves which purpose |

[ARCHITECTURE.md](ARCHITECTURE.md) has the boundaries and the rules. [DECISIONS.md](DECISIONS.md)
records choices that weren't obvious. MIT licence.
