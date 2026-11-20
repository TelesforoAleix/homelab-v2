# Contracts

This document lists every interface another system depends on: APIs, socket protocols, file
formats, shared tables, environment variables and commands called from elsewhere. For each it
names the version, the provider, the users, the source that defines it and how to change it
safely. Read it before changing anything a contract's source names, and declare any change to
a contract here in the PR request's `contract_changes`. Wire details stay in the linked
documents; this page does not copy them.

Changing any contract follows the same order unless its entry says more: extend compatibly
(new optional fields, new purposes, new routes), keep the old behaviour until every user has
moved, and put incompatible changes under a new version that runs beside the old one.

## run-api

- **Version:** 1 (`"contract": 1` in every request and message).
- **Provider:** `api` in this repository. **Users:** Factory's runner and eval runner.
- **Source:** `src/homelab/contracts/run_api.py` (wire models), `src/homelab/api/runs.py` (routes),
  `src/homelab/api/run_keys.py` (keys and scopes), `config/executors.yaml` (executor catalogue).
  Schema: `contracts/run-api/openapi.json`. Wire format and checks: [run-api.md](run-api.md).
- **Change safely:** add optional fields with defaults that keep today's behaviour; a retry of
  an existing request must still compare equal, so never change how omitted fields default.
  Scope names, status reasons and error codes are part of the contract. Key-file changes keep
  the `{application, sha256, scopes, repositories}` shape.
- **Readers:** request readers ignore unknown fields and record their paths, so a newer sender
  keeps working after a provider rollback; senders still send none. `GET /v1/contracts/usage`
  (`runs:read`) reports accepted shapes and unknown field paths, recorded by
  `src/homelab/api/contract_usage.py`; see [runs.md](runs.md#contract-usage).

## decisions-api

- **Version:** 1 (`POST /v1/decisions`, run key with `decisions:write`).
- **Provider:** `api`. **Users:** Factory (its `cplane docs-verify` checks claims with it).
- **Source:** `src/homelab/api/decisions.py`, `src/homelab/api/decisions.sql`. Wire format and
  limits: [decisions-api.md](decisions-api.md).
- **Change safely:** clients never send a provider or model, so the adapter can change behind
  the contract. Keep the request shape, the aggregation rules and the error codes; a new
  answer field is optional for clients.

## model-gateway

- **Version:** `/v1`; incompatible changes go under `/v2`.
- **Provider:** `gateway`. **Users:** med-ask, the image studio, the Telegram bot, and this
  repository's `api`, `worker` and `cli`.
- **Source:** `config/litellm.yaml` (purposes), `config/litellm_hooks.py` (policy and refusals),
  the `homelab-models` network in `compose.yaml`. Usage: [gateway.md](gateway.md#using-the-gateway).
- **Change safely:** add purposes and optional request fields; never rename or remove a purpose
  a client uses, and keep embedding purposes on the same model and vector space, since a
  changed embedding model needs a new index in every client.

## media

- **Version:** `/v1`, extended by the `operations` mapping of `GET /v1/model/info`.
- **Provider:** `gateway` (image routes and video middleware) and `api` (`GET /v1/images/cost`).
  **Users:** the image studio.
- **Source:** `media_model_list` and the image entries in `config/litellm.yaml`,
  `src/homelab/api/video_jobs.py`, `src/homelab/api/image_costs.py`. Fields, models and
  prices: [platform.md](platform.md#image-studio-operations-and-video-jobs).
- **Change safely:** keep the six operation names and the job status shape; a model may be
  added to or removed from an operation, but each supported operation keeps one default.

## knowledge-api

- **Version:** unversioned `/v1/knowledge/*` routes.
- **Provider:** `api`. **Users:** the Telegram bot's `/ask`.
- **Source:** `src/homelab/api/app.py` (`KnowledgeQuery`, the answer and query routes).
  Behaviour: [knowledge.md](knowledge.md#endpoints-and-evaluation).
- **Change safely:** keep `answer`, `refused` and `sources`; the bot shows numbered source
  titles and treats a refusal as an answer without sources.

## client-environment

- **Version:** 1.
- **Provider:** this repository's settings. **Users:** every gateway client container.
- **Source:** `HOMELAB_MODELS_BASE_URL` and `HOMELAB_MODELS_API_KEY` in `src/homelab/settings.py`
  and `compose.yaml`.
- **Change safely:** a client knows only a base URL, a client key and purposes it discovers;
  never add a variable that names a provider or a model.

## launcher-socket

- **Version:** 1 (one JSON line per connection on `/run/homelab-launcher.sock`).
- **Provider:** `launcher`. **Users:** Factory's runner (uid 21010) and sessions (uid 21011),
  and `runs` (uid 21020).
- **Source:** `node/usr/local/sbin/homelab-launcher`, `node/etc/homelab-launcher/callers.json`,
  `node/etc/homelab-launcher/profiles.json`. Operations and fields:
  [operations.md](operations.md#install-the-fixed-fence-launcher).
- **Change safely:** add optional request fields that default to today's behaviour, as
  `read_only`, `stdin` and `stream` did. A caller-list change is a guard change that needs the
  owner's review; restart the launcher, then every socket client, after installing it.

## deployer

- **Version:** 1 (socket request line and manifest fields).
- **Provider:** `deployer`. **Users:** Factory's runner (uid 21010, `install` only) and the owner.
- **Source:** `node/usr/local/sbin/homelab-deployer`, `node/etc/homelab-deployer/callers.json`,
  `node/etc/homelab-deployer/automatic`, `node/etc/homelab-deployer/factory.json`. Manifest and
  requests: [deployer.md](deployer.md).
- **Change safely:** the manifest rejects unknown fields, so add a field to the helper before
  any manifest uses it. Removing `factory` from `automatic` stops Factory's automatic installs.

## change-classes

- **Version:** 1 (`<class> <pattern>` lines).
- **Provider:** this repository's `.github/change-classes`. **Users:** Factory, which reads the
  file at the merge base: its runner before a merge and its deployer before an install.
- **Source:** `.github/change-classes`.
- **Change safely:** raising a pattern's class is always safe; lowering one weakens review and
  needs the owner's approval, since the file is itself class 4.

## bot-orders

- **Version:** 1 (exactly two HTTP routes on TCP 8110).
- **Provider:** `telegram-bot`. **Users:** Factory's order collector, which polls `GET /orders`
  and acknowledges each order, over Tailscale or from `homelab_default`.
- **Source:** `node/opt/homelab-telegram-bot/order_listener.py`, `node/opt/homelab-telegram-bot/orders.py`.
  Routes and payloads: [telegram-bot.md](telegram-bot.md#bot-order-endpoint).
- **Change safely:** the six order fields are complete; a new action needs a new confirmation
  path in the bot and the collector's own precondition check before it is offered.

## inbox-helper

- **Version:** 1 (`homelab-inbox list|file|done`).
- **Provider:** `telegram-bot`'s inbox and the root helper `node/usr/local/sbin/homelab-inbox`.
  **Users:** repository orchestrators on the Mac, over `ssh homelab-agent sudo -n`.
- **Source:** `node/usr/local/sbin/homelab-inbox`, `node/opt/homelab-telegram-bot/inbox.py`.
  Usage and record fields: [telegram-bot.md](telegram-bot.md#inbox-helper-over-ssh).
- **Change safely:** keep the exit codes and the `item.json` fields; add fields rather than
  rename them, since readers treat records as data.

## queue-notice

- **Version:** 1 (`homelab-queue-notice [--task]`, text on stdin).
- **Provider:** `notifier` (`node/usr/local/sbin/homelab-queue-notice`). **Users:** the Mac job
  queue (`mac/profile_queue.py`) and the host runner's failure notices.
- **Source:** `node/usr/local/sbin/homelab-queue-notice`.
- **Change safely:** keep the stdin-only text and the prefixes; notice text must never reach
  an argument, a unit property or the journal.

## mac-router

- **Version:** 1 (OpenAI-compatible `/v1` on TCP 8090, plus two static JSON files).
- **Provider:** `mac-router` and `mac-profiles`. **Users:** the gateway (`vision`,
  `embed-large-bulk` and their `-<id>` names) and the worker's offer read.
- **Source:** `mac/homelab-router`, `mac/offer.py` (`homelab-offer.json`), the pause flag file
  `homelab-profile.json` written by `mac/homelab-router`, and `src/homelab/jobs/providers.py`
  (the reader). Behaviour: [mac.md](mac.md#model-router).
- **Change safely:** the native aliases `vision` and `Qwen3-Embedding-4B` must keep matching
  the gateway's upstream names; the offer reader rejects unknown fields, so add a field to
  `src/homelab/jobs/providers.py` before the Mac publishes it.

## mac-listener

- **Version:** 1 (`POST /backup` with an empty body on TCP 8091).
- **Provider:** `mac-listener` (`mac/homelab-listener`). **Users:** the Telegram bot's `/backup`.
- **Source:** `mac/homelab-listener`, `node/opt/homelab-telegram-bot/executors.py`.
  Behaviour: [mac.md](mac.md#mac-action-listener).
- **Change safely:** the listener has exactly one action and no arguments; a new action is a
  new contract version and a security review.

## profile-cli

- **Version:** 2 (the `profiles.json` version), JSON on stdout for every command.
- **Provider:** `mac-profiles` (`mac/homelab-profile`). **Users:** the menu bar app and the
  owner's queued commands (`HOMELAB_QUEUE_PROGRESS`).
- **Source:** `mac/homelab-profile`, `mac/profile_queue.py`, `mac/profile_models.py`,
  `mac/MenuBar/Sources/HomeLabMenuBar/Reports.swift` (the reader). Commands:
  [mac.md](mac.md#usage-profiles).
- **Change safely:** the menu app holds no profile rules and decodes the JSON; keep existing
  fields (`allowed`, `running`, `state`) when adding new ones.

## context-server

- **Version:** 1 (MCP over stdio through `ssh -T homelab-agent sudo -n homelab-context <name>`).
- **Provider:** `context-server`. **Users:** coding clients on the Mac.
- **Source:** `node/usr/local/sbin/homelab-context`, `node/opt/homelab-context/serena_config.yml`.
  Behaviour: [node.md](node.md#code-context-server).
- **Change safely:** the tool list is a fixed allowlist; adding a tool, above all one that
  edits or runs commands, is a security review.

## postgres-service

- **Version:** 1 (the Postgres 17 cluster at a fixed address on the default network).
- **Provider:** `postgres`. **Users:** Factory, which keeps its own database in the cluster, and
  this repository's host services and containers.
- **Source:** the `postgres` service and `default` network in `compose.yaml`,
  `node/usr/local/sbin/homelab-dump`.
- **Change safely:** keep the fixed address and the database user; the daily dump covers every
  non-template database, so a new database needs no change to be backed up.
