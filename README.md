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

The node runs v2. The retired v1 programs, units, configuration, state, accounts and checkouts
have been removed; Brain and v2's volume data remain. Failure notifications accept only `bot`,
`watchdog` and `homelab`.

The FastAPI service exposes health, an ingestion trigger, top-k retrieval with provenance and
answers grounded in retrieved knowledge with numbered citations.
`homelab ingest` indexes the active collection incrementally: public entries from the
about-Aleix JSON corpus by default, or included Brain Markdown. Procrastinate provides a
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
sudo visudo -cf /srv/homelab/homelab-v2/node/etc/sudoers.d/homelab-agent-v2 && sudo install -m 0440 -o root -g root /srv/homelab/homelab-v2/node/etc/sudoers.d/homelab-agent-v2 /etc/sudoers.d/homelab-agent-v2
sudo usermod -aG systemd-journal homelab-agent
sudo install -m 0644 -o root -g root /srv/homelab/homelab-v2/node/etc/systemd/system/homelab.service /etc/systemd/system/homelab.service
sudo systemctl daemon-reload && sudo systemctl enable --now homelab.service
```

Host files live under `node/`: `node/<path>` is installed at `/<path>`, owned by `root:root`.
Units and drop-ins use mode `0644`, scripts under `usr/local/sbin/` use `0755`, and sudoers
uses `0440`. Install from the node's clone after it pulls merged `main`: back up the installed
files, show each diff, copy changed files with `sudo install`, and run their validators
(`visudo -c` for sudoers and `systemd-analyze verify` for units). Reload systemd after unit
changes. The unlock, notifier, watchdog and Telegram bot files are recorded here. Bot Python
files and the polkit rule use `0644` too. Credential drop-ins name TPM-sealed
credentials already provisioned on the node; credential contents are never committed.

The agent uses `ssh homelab-agent`, which has root through sudo. The owner approves the plan
and its risks before execution. Deploy with `sudo systemctl restart homelab.service`; it pulls
`main` and builds the images. Host-file changes also require the explicit installation above;
restarting the stack does not install them.

The Telegram bot stays a stdlib-only host service running as `homelab-bot`. Its code lives at
`node/opt/homelab-telegram-bot/`, installed at `/opt/homelab-telegram-bot/`. `/ask <question>`
calls the loopback answer endpoint and replies with the answer and numbered source titles;
refusals have no sources. Long replies are split into Telegram-sized messages. When the volume
is locked or the API is unavailable, `/ask` reports that the knowledge service is down.
`/status`, `/disk`, `/uptime`, `/restart`, `/help` and `/start` keep working independently.
`/spend` and `/model` are no longer commands. Startup registers the router's command list and
description with Telegram; registration failure is logged and polling continues.

Private bot configuration stays on the node under `/etc/homelab-telegram-bot/`: `allowlist`
and `privileged-allowlist` contain numeric Telegram user IDs, one per line; `restart-allowlist`
contains unit names, one per line (a missing `.service` suffix is added). All three ignore blank
lines and `#` comments, including trailing comments, and use owner/group `root:homelab-bot` with
mode `0640`. The main allowlist must be nonempty; privileged users must be a subset of it.
A missing or empty privileged or restart allowlist grants no capability. Paths can be overridden
by `HOMELAB_BOT_ALLOWLIST`, `HOMELAB_BOT_PRIVILEGED_ALLOWLIST` and
`HOMELAB_BOT_RESTART_ALLOWLIST`. Their contents never enter git, tests or logs. The token stays
in TPM-sealed `token.cred`; the bot reads only systemd's `$CREDENTIALS_DIRECTORY/bot-token`.

`POST /v1/knowledge/query` and `POST /v1/knowledge/answer` accept `{"question": "…", "top_k": 5}`
(nonblank question; integer `top_k` at least 1, default 5). Query returns scored chunks with
provenance. Answer retrieves the same chunks and makes one call to the configured `chat` route.
It returns `answer`, `refused` and `sources`: each cited chunk's `number`, `title`, `path` and
`score`, in first-citation order. Insufficient evidence produces a plain refusal with no sources;
malformed model replies or missing/invalid citations also fail closed. The refusal text is
“That isn't covered in what Aleix has written here — you can ask him directly.” Answers always
refer to Aleix by name in the third person. Metrics record lengths, counts, refusal and timing,
without question, chunk or answer content.

Knowledge operations share `HOMELAB_ACTIVE_COLLECTION` (`about_aleix` by default, or `brain`).
Each collection has separate chunk and document tables; switching does not rebuild Brain.
The corpus is mounted read-only from `${HOMELAB_CORPUS_PATH:-/srv/homelab/corpus}` at
`/data/corpus`. `HOMELAB_CORPUS_FILE` defaults to `/data/corpus/about-aleix/corpus.json`.
Public JSON entries become documents whose titles are their questions and whose provenance
paths are stable entry ids. Other visibility values are skipped. Invalid individual entries
are skipped with position-only warnings; invalid JSON, a non-list root or duplicate public ids
abort ingestion before writing.

`homelab eval` reads the same corpus and asks every public entry's own question against
`about_aleix`. It prints only document count, hit@5, mean reciprocal rank (MRR), retrieval miss
ids, refusal count and refusal ids. Answers use the same path as the answer endpoint; no eval
file is written. When `HOMELAB_EVAL_FILE` (default
`/data/corpus/about-aleix/eval.json`) exists in the read-only corpus mount, it also scores
`bank_paraphrases`, `refusal_checks` and `visitor`, with visitor results per group. Answer items
measure hit@5 and MRR against any listed entry, and answer correctness requires a non-refusal
with a listed source path. Refusal items measure correct refusals. Failures carry ids and
`retrieval_miss`, `refused`, `wrong_citation` or `answered` reasons; multiple reasons can apply.
The baseline, each set and each visitor group also report `first_person_count`: non-refused
answers containing `I`, `me`, `my` or `mine` as whole words, ignoring case. Each answer counts
once, including quoted phrases; no text or match details are reported.
The baseline fields stay at the JSON root, independent scores are under `sets`, and numeric
`eval_file_found` is 0 when the file was not found (baseline only), 1 otherwise. Empty subsets
have zero rates. No question or answer text is printed. Run it inside the app container on the
node after ingestion.

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
| `src/homelab/knowledge/` | JSON/Markdown ingestion and retrieval with provenance through LlamaIndex |
| `src/homelab/jobs/` | Procrastinate tasks and the worker |
| `config/routes.yaml` | which model serves which purpose |
| `node/` | host operational files, mirroring their installed paths |

[ARCHITECTURE.md](ARCHITECTURE.md) has the boundaries and the rules. [DECISIONS.md](DECISIONS.md)
records choices that weren't obvious. MIT licence.
