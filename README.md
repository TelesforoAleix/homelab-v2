# Home Lab v2

My personal AI system: model access, knowledge retrieval with sources, and durable background jobs.

> [!WARNING]
> **In development.** Model access, knowledge retrieval with cited answers and durable jobs run on the node today; applications such as med-ask build on it.

## Overview

Every side project needed model access without keys scattered across repositories, search over
my own knowledge with answers that show their sources, and work that keeps running in the background.
HomeLab builds these once on a small always-on server that my projects plug into.
This second version keeps the lessons from the learning-first v1 and leans on mature tools.

## Architecture

<picture><source media="(prefers-color-scheme: dark)" srcset="docs/diagrams/architecture-dark.svg"><img alt="HomeLab architecture: built clients, API, Models, Knowledge, Jobs, Sources, Providers and Postgres; planned Second Brain UI, Coding agents, Tools, Job API and local batch jobs." src="docs/diagrams/architecture-light.svg"></picture>

Solid parts are built; dashed parts are planned.

- The diagram shows where HomeLab is heading: one API for every client. Today med-ask reaches
  only LiteLLM, over the `homelab-models` network.
- `embed-large` stays on the node for questions; `embed-large-bulk` uses the same vector space
  on the owner’s Mac through a login-managed llama.cpp router for bulk ingest. See the
  [Mac serving runbook](docs/operations.md#mac-bulk-embedding-serving).
- Mac login agents run a stamped, private [installed copy from main](docs/operations.md#install-and-update-the-mac-tools),
  independent of the working checkout. Update with `git pull --ff-only` and `mac/install-mac-tools`.
- The [Mac profile command](docs/operations.md#mac-usage-profiles) selects Working or Inference,
  reports observed state as JSON, and returns to Working after idle inference. Each profile
  sets services to off, on request, or on, with allowed models and a default from a private
  registry. It can download, verify and test-load new models and reports warning-only
  profile memory budgets that include mapped weights; see [profile model policy](docs/operations.md#per-profile-service-and-model-policy). After 10
  continuous minutes on battery, Inference pauses its model services and resumes on AC.
  Working leaves the owner’s AC and Battery settings alone. Inference requires AC power,
  changes only AC power mode/sleep and the global GPU limit, and restores their recorded
  values on return. Relaunched apps
  restore windows and tabs according to their own settings: Chrome’s “Continue where you left
  off” and macOS’s “Close windows when quitting an application” are owner choices.
- A SwiftUI [menu bar app](docs/operations.md#menu-bar-app-usage-and-night-window) drives the
  same command: one-click switching, per-service control, a Profiles editor for service states,
  allowed/default models and warning-only memory budgets, requests waiting for Inference,
  Claude/Codex usage, and an owner-set night window into Inference that is off by default.
- Knowledge endpoints: `POST /v1/knowledge/query` and `/v1/knowledge/answer`; jobs persist in Postgres.
- Hosted images use three `image-*` purposes through LiteLLM. Native image upscale and
  background video jobs share their $20 monthly gateway-key budget. `GET /v1/model/info`
  lists models by operation with prices and capabilities.
  Image responses report decimal USD in `x-homelab-cost-usd` when known, including charged
  failures. `GET /v1/images/cost` returns the bearer key's UTC month, `total_usd` string and
  `charged_calls` count for images and video together. The owner receives monthly 80% and 100% budget alerts through Telegram.
- [ARCHITECTURE.md](ARCHITECTURE.md) explains the boundaries and rules;
  [DECISIONS.md](DECISIONS.md) records the choices behind them.

A [read-only code context server](docs/operations.md#code-context-server) gives Mac coding agents
symbol navigation over SSH. It runs Serena without network access against separate,
periodically refreshed copies of configured repositories on the encrypted volume.

The Telegram bot runs as a sandboxed host service using python-telegram-bot in its own
hash-pinned virtual environment. It long-polls Telegram and remains available
before the data volumes are unlocked. After unlock, an optional read-only planning mirror
supplies `/pending`, `/recap [hours]`, `/slice <id>` and notices for new outbox events.
Allowlisted free-text questions use the `frontline` purpose through LiteLLM on
`127.0.0.1:4000`, grounded in privately configured planning files and pending work.
In-memory episodes keep follow-ups together; replying to an older answer reopens its episode.
The model can read one extra bounded clone file and propose one order; it cannot execute.
The bot validates proposals and shows Yes/No buttons. Only an allowlisted tap queues an order
on the encrypted volume. A [tailnet-only HTTP endpoint on TCP 8110](docs/operations.md#bot-order-endpoint)
hands queued orders to a future Mac runner; Tailscale policy gates it, with no key.
The inbox keeps ideas, links, unprocessed attachments and messages for repository orchestrators.
The model classifies free text and asks at most two short clarifying questions; “just save it”
keeps what is known. `/inbox [id]` lists or shows kept items. Public links have a guarded
fetch and isolated summary; files are limited to 20 MiB each and 2 GiB in total.
The inbox lives beside orders on the encrypted volume. Mac orchestrators use the root-owned
[`homelab-inbox` helper over SSH](docs/operations.md#inbox-helper-over-ssh) to list new items,
stream attachments and mark records read; it adds no network route.
Answers and new proposals require unlock and the model gateway; host commands remain available while locked.
Private configuration and a read-only deploy key stay on the node; see the
[bot runbook](docs/operations.md#run-it).

Docker state and rebuildable files live on the encrypted second SSD at `/srv/homelab-bulk`;
databases, originals and corpus stay on `/srv/homelab`. After boot the owner unlocks main,
which supplies bulk’s key, then Docker and the applications start. See
[Rebuilding the node](docs/operations.md#rebuilding-the-node) for setup and recovery.

## Quick start

```bash
# Development (no services)
uv sync
uv run pytest
```

Running the stack needs a provisioned node; see the [operator runbook](docs/operations.md#run-it).

## Project structure

| Path | What |
|---|---|
| `src/homelab/api/` | FastAPI API |
| `src/homelab/models/` | Purpose discovery and model clients |
| `src/homelab/knowledge/` | LlamaIndex ingestion and retrieval |
| `src/homelab/jobs/` | Procrastinate tasks and worker |
| `config/litellm.yaml` | Model purposes and routes |
| `node/` | Host files at their installed paths (table entries are merged) |
| `mac/` | Usage profiles, measurements, model router and action listener |

First version: [homelab-v1](https://github.com/TelesforoAleix/homelab-v1)

Used by: [med-ask](https://github.com/TelesforoAleix/med-ask)

[Operator runbook](docs/operations.md): running the stack, Mac services, node rebuilds and originals copies.

## Mac job queue

The installed `homelab-profile` runs resumable commands one at a time through launchd, in
any profile. Commands use an absolute executable and argv, without a shell:

```bash
homelab-profile queue add corpus --cwd /absolute/project -- /absolute/python /absolute/label.py
homelab-profile queue list
homelab-profile queue play
homelab-profile queue stop
homelab-profile queue top corpus
homelab-profile queue remove corpus
homelab-profile queue requeue corpus
```

Every command prints JSON; `status` includes the same queue data. `top` moves a waiting job
forward without interrupting a run. `stop` terminates the whole process group (SIGKILL after
10 seconds), turns play off and puts that job first, waiting. `play` resumes from whatever the
command saved. A nonzero exit retries once, then stays failed while the queue moves on;
`requeue` resets a failed or stopped job's attempts and appends it. Remove waiting, failed or finished
jobs to delete their local logs and progress files.

The platform bot tells the owner's private chat when a job fails after its retry (name,
exit code and last five log lines), and when the last waiting job ends: "Mac queue empty;
the Mac is free". Other completions, stops and battery pauses are silent. Sends run outside
the queue lock with a short timeout; failures are logged on the Mac and dropped without retry.

Nothing starts on battery. Ten continuous minutes on battery stop the running job while
keeping play on (`paused_on_battery`); ten continuous minutes back on AC resume it. The menu
reports when power is holding the queue and the remaining debounce time. A running
job keeps the Mac awake, but closing the lid still sleeps it. Scheduled jobs retain their
profile/AC/idle requirements and take the next slot before queued work. Queue jobs count as
activity for the automatic return to Working, without changing profiles themselves.

Logs (0600) stay under `~/.local/state/homelab-profile/` until removal. A command may overwrite
the file named by `HOMELAB_QUEUE_PROGRESS` with one short progress line. Status reports that
line, elapsed seconds, attempt, log path, process-group physical footprint (`physical_footprint_mb`)
and available memory (`free_memory_mb`) in MiB. Footprint includes charged Metal allocations
but excludes clean mapped model-file pages; available memory uses macOS `memory_pressure`'s
free percentage (including immediately reclaimable memory), rounded by macOS to whole percent.
Install updates from merged main using `mac/install-mac-tools`; the queue worker is the
`org.homelab.profile.queue` login agent.

The menu’s **Job queue** section sits below services: Play/Stop, battery pause, running job,
elapsed time, attempt, progress and footprint beside available memory. Waiting jobs stay in
order with **To front** and **Remove**; failed jobs offer **Requeue** and **Remove**, finished
jobs **Remove**. Moving to front leaves the running job alone; Stop immediately stops the
attempt, so unsaved items are redone on resume. Status refreshes every five seconds while the
menu is open. Add jobs through the command above.

## Image studio: operations and video jobs

Use the model gateway at `http://127.0.0.1:4000` with `Authorization: Bearer <client-key>`.
`GET /v1/model/info` returns `data: [{model_name, model_info}]` plus an `operations` object
mapping these stable names to model-purpose lists: `generate_image`, `edit_image`,
`upscale_image`, `image_to_video`, `upscale_video`, `edit_video`. Each media profile lists
`operations`, `default_for`, `price` (USD, with tiers or an explicitly marked starting price)
and `capabilities` (durations, resolutions, aspect ratios, audio, end frame, reference-image
and reference-video counts, mask). An empty operation list means unsupported. One to four
verified models may serve an operation; each supported operation has one default.

| Operation | Purposes (default first) | Published price in USD |
|---|---|---|
| `generate_image`, `edit_image` | `image-gpt-2.5-flare`, `image-gpt-2.5-sunburst`, `image-nano-banana-2.1` | GPT output estimate: low .00588, medium .0132, high .05268, xhigh .09366, max .21072 per image; reference estimate .0122. Nano: 1K .0336, 2K .0504, 4K .1134 |
| `upscale_image` | `image-upscale-wonder` | Starting at .08/image; full size tariff unavailable |
| `image_to_video` | `video-grok-lite`, `video-kling-turbo`, `video-grok` | Lite: .02/.03/.14 per second at 480p/720p/1080p. Kling: .042 std, .07 pro per second (720p selects std, 1080p pro). Grok: .05/.07 per second at 480p/720p |
| `upscale_video` | `video-upscale-proteus` | Starting at .08/video; full duration/resolution tariff unavailable |
| `edit_video` | `video-grok` | .05/.07 per second at 480p/720p |

Existing image generation/edit requests retain their shapes. Image upscale uses
`POST /v1/images/edits`, multipart fields `model=image-upscale-wonder`, exactly one `image`
(or `image[]`) PNG/JPEG/TIFF file up to 16 MiB, optional `size=WIDTHxHEIGHT` and
`response_format=b64_json`. It returns `{"data":[{"b64_json":"..."}]}`.

Start video work with `POST /v1/videos`, a JSON object with `model`, `operation`, `prompt`
and inline media. Send a stable `Idempotency-Key` header (1–128 letters, digits or `_.:-`)
when retrying a start: the same client key and idempotency value return the same job, even
if the retry body differs. Without it, each accepted POST creates a new paid job. For example:

```json
{"model":"video-grok-lite","operation":"image_to_video","prompt":"A slow camera move",
 "image":"data:image/png;base64,...","duration":1,"resolution":"854x480","generateAudio":false}
```

`image_to_video` requires either `image` or a nonempty `referenceImages` array up to the
profile's reference limit. `video-grok` accepts one to five reference images without a start
or end image. A start image with references requires explicit profile support; no current
profile supports that combination. No listed model accepts `endImage`. Kling inherits the
start image's aspect ratio and refuses `aspectRatio`; its resolution selects quality mode.
`upscale_video` requires `video: "data:video/mp4;base64,..."` and `resolution`; omit `prompt`
or send an empty string. Optional `source` supplies actual input `{width,height,duration,frameRate}`
and optional `frameCount`. `edit_video` requires `video` and a nonempty `prompt`; duration,
resolution, aspect ratio, reference images and audio generation controls are unavailable
for this model's editing mode. Generation also accepts `aspectRatio` and `generateAudio`;
omitted generation duration/resolution select the profile's smallest values. Other settings
are limited to the published profile. Media must be base64 data URIs; remote input URLs,
provider overrides and query parameters are refused. The JSON body limit is 48 MiB.

Start returns HTTP 202 immediately after storing the job metadata:

```json
{"id":"<uuid>","operation":"image_to_video","model":"video-grok-lite","status":"queued","cost_usd":null}
```

`GET /v1/videos/<uuid>` returns that same shape, with status `queued`, `running`, `completed`
or `failed` and decimal-string `cost_usd` once known. A different bearer key gets 404.
`GET /v1/videos/<uuid>/content` streams the completed video's bytes with its media type;
409 means unfinished/failed, 410 means expired. Only content type, content length and
`x-homelab-cost-usd` can appear on video responses. No provider URL or diagnostic is returned.
Fetch content promptly: results expire upstream; Home Lab's memory cache lasts at most one
hour and holds at most 16 results. Single-request content is lost on a gateway restart.

Small requests use Vercel's async API; larger inline inputs use a background single request.
At most four jobs run concurrently (429 when busy); jobs stop waiting after one hour.
Async jobs with a saved Vercel reference resume when the gateway next receives a video request.
Interrupted single requests become failed and are never automatically repeated: their inputs
were not persisted, and an interrupted request may already have been charged.

Video charges, including charged failures, add **once per job** to the same per-key UTC
monthly total returned by `GET /v1/images/cost`: `{"month":"YYYY-MM","total_usd":"decimal",
"charged_calls":1}`. Repeated status/content reads add no charge. Missing cost stays unknown;
video estimates never debit the total. The existing $20 Vercel key budget and 80%/100% alerts
cover images and video together. Keys, prompts and media never enter the jobs table or logs.
