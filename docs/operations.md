# Operations

## Run it

For a fresh node, follow [Rebuilding the node](#rebuilding-the-node). On the running node,
connect with `ssh homelab` and unlock with `sudo data-volume.sh unlock` after each boot.

Host files live under `node/`: `node/<path>` installs at `/<path>`, owned by `root:root`.
[AGENTS.md](../AGENTS.md#host-file-modes) records directory and file modes. For updates, use the
node's clone of merged `main`: back up installed files, show each diff, copy changed files with
`sudo install`, and run `visudo -c`, `sshd -t` and `systemd-analyze --generators=yes verify`
where applicable. Reload systemd after unit changes. Credential drop-ins name TPM-sealed
credentials; credential contents are never committed. A PR changing `node/` updates the
rebuild section in the same PR. The base files preserve their installed bytes, including
historical v1 comments; those references do not require v1 scripts or a node GitHub key.

The agent uses `ssh homelab-agent`, which has root through sudo. Proceed within the owner's
approved task; the exceptions requiring approval are in `AGENTS.md`. Deploy with
`sudo systemctl restart homelab.service`; it pulls public `main` over HTTPS and builds images.
Host-file installation is explicit; restarting the stack does not install those files.

The Telegram bot stays a stdlib-only host service running as `homelab-bot`. Its code lives at
`node/opt/homelab-telegram-bot/`, installed at `/opt/homelab-telegram-bot/`. `/ask <question>`
calls the loopback answer endpoint and replies with the answer and numbered source titles;
refusals have no sources. Long replies are split into Telegram-sized messages. When the volume
is locked or the API is unavailable, `/ask` reports that the knowledge service is down.
`/status`, `/disk`, `/uptime`, `/restart`, `/backup`, `/help` and `/start` keep working independently.
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
The corpus is mounted read-only at `/data/corpus` from `HOMELAB_CORPUS_PATH`, defaulting to
`/srv/homelab/homelab-v2-data/originals/corpus`. `HOMELAB_CORPUS_FILE` defaults to `/data/corpus/about-aleix/corpus.json`.
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

Models are served by LiteLLM using `config/litellm.yaml`, the single route table. The API,
worker, ingestion CLI and eval discover purposes from its `/v1/models` endpoint and use those
names through the existing LlamaIndex clients. They hold only `HOMELAB_MODELS_BASE_URL`
(default `http://litellm:4000/v1`) and `HOMELAB_MODELS_API_KEY` (default `homelab`, accepted
and ignored today). The gateway secret is mounted only into LiteLLM, which reads it at startup;
its existing `root:10001` ownership and `0440` mode stay unchanged.

On the Mac, development needs no services:

```bash
uv sync
uv run pytest
```

On a Docker host, provision `.env` and both gateway secrets as described in step 10, then run:

```bash
docker compose up -d --build
docker compose exec api homelab routes
docker compose exec api homelab eval
```

`/health` returns `status`, `version`, `routes` and `models_base_url`; the former
`gateway_key_present` field retires because the API no longer holds the gateway key. No host
port is published for LiteLLM. Its official image is pinned by digest in Compose and updated
deliberately. Telemetry, remote model-price updates, hosted callbacks and the admin UI are off;
tokenizers are bundled. Its external model calls go only to Vercel AI Gateway; embeddings go
to `llama-embed` or `llama-embed-large` on the private network. The journal records one line
per endpoint call with purpose, real model, HTTP status, elapsed seconds and whether an upstream call was attempted.
Content, embeddings and keys are excluded, including on errors and streaming calls.

### Use the model endpoint

Attach a client container from another Compose project to the external Docker network
`homelab-models`, created by this stack. Only LiteLLM joins it; Postgres, API and both
embedding servers remain on the stack's private network. Give the client its base URL `http://litellm:4000/v1`
and an arbitrary API key through its environment, then use OpenAI-shaped `GET /v1/models`,
`GET /v1/model/info`, `POST /v1/chat/completions`, `POST /v1/embeddings`,
`POST /v1/images/edits` and `POST /v1/images/generations`. The existing ten names are `chat`
(standard), `chat:high`, `chat:xhigh`, `embed`, `embed-large`, `embed-large-bulk`, `grade`,
`translate`, `vision` and `vision:xhigh`.
`grade` makes yes/no relevance judgements per candidate passage, using the same model and low effort as `chat`.
`translate` translates passages on request, using the same model and low effort as `chat`.
`vision` reads page images with Qwen3-VL-8B on the owner’s Mac while it serves;
`vision:xhigh` uses the gateway for pages that need the hosted rung. There is no `vision:high`.
Send images as `data:` URLs. When the Mac is stopped, `vision` returns a generic OpenAI-shaped
4xx or 5xx with a 3-second connection timeout; clients keep their pages queued. Requests allow
300 seconds for transcription, with no retries, fallback or incident alert. The client chooses a tier; no escalation
or fallback happens automatically. Chat responses report the purpose; embedding responses
report the real embedding model, which clients must record at ingest. An embedding model
change requires a new index. Unknown names, provider model ids and unavailable tiers receive
a 4xx without an upstream call. The accepted client key provides no access control today:
attach only trusted clients to this network. Provider/routing overrides and remote image URLs
are refused; configuration and management endpoints are not exposed. New purposes and optional
request fields can be added to `/v1`; breaking changes require `/v2`.

Three paid image purposes also appear: `image-nano-banana-2.1`,
`image-gpt-2.5-flare` and `image-gpt-2.5-sunburst`.
Discover their capabilities with `GET /v1/model/info`: its `data` array contains only
`model_name` and `model_info` for every purpose. Deployment parameters, upstream addresses
and key references are never returned. Each image profile includes its endpoint, response
form, reference count, display name, terms, settings and USD price estimates.
All offered profiles contain measured response forms and times. Reference maxima are
documented limits; the proof
checks two references per model rather than claiming a full-count load test.

Direct gateway proofs on the node, 2026-10-14, initially used two synthetic 256px PNG
references and one call per model. Nano Banana 2 then received two owner-authorized
diagnostic calls. These measure the gateway, not the deployed proxy.

| Purpose | Endpoint | HTTP | Response | Call seconds | Images | Reported USD |
|---|---|---|---|---:|---:|---:|
| Nano Banana 2 (excluded) | edits | 400 | invalid_request_error | 0.969 / 0.233 / 0.223 | 0 | not reported |
| `image-gpt-2.5-flare` | edits | 200 | b64_json | 10.074 | 1 | not reported |
| `image-gpt-2.5-sunburst` | edits | 200 | b64_json | 8.718 | 1 | not reported |
| `image-nano-banana-2.1` | chat | 200 | message_images | 14.147 | 2 | 0.0377865 |
| `image-flux-3` | edits | 200 | b64_json | 27.750 | 1 | not reported |

Nano Banana 2 was tried three times in total, always with two references: the original
`size=512x512` request, one exact repeat retaining a sanitized error, then one request
omitting `size`. Both diagnostic calls returned this sanitized error:

```json
{"message":"Model 'google/gemini-3.1-flash-image' is a language model, not an image model. Use the language generation API instead.","type":"invalid_request_error"}
```

The [model page](https://vercel.com/ai-gateway/models/gemini-3.1-flash-image) documents language
APIs, while the general edits guide lists it as supported. The live gateway's classification
refusal remained unchanged without a size, so Nano Banana 2 is not reachable through
`/images/edits` and has no entry here. It was not moved to chat or replaced; the owner
decides any later route. This is not a shared size-mapping failure. The remaining profile
mappings stay unchanged; no additional paid calls were made for those models.

GPT calls used `quality=low`, `size=1024x1024`; Nano Banana 2.1 used 1K; FLUX used
`size=768x768`. FLUX 3 was subsequently removed after proxy validation; see below. `seconds_per_image` divides call time by returned image
count (Nano Banana 2.1 returned two). These single samples are estimates of future latency.
The reported cost subtotal is $0.0377865; other calls did not report a cost in the inspected
response fields. Listed output estimates for the two GPT calls and FLUX add $0.03226,
excluding reference/prompt tokens. The exact total spend requires the Vercel dashboard.

The subsequent pinned LiteLLM 1.103.3 response-parser check found that FLUX 3's gateway
response has `usage.input_tokens_details: null`, which `ImageResponse` rejects. A local
stub captured the same model, prompt, `n=1`, `size=768x768`, `response_format=b64_json`
and two identical PNG `image[]` parts as the direct proof. Only filenames, user-agent and
multipart framing differed. Matching the LiteLLM request shape still returned an image
that failed response parsing; the client-facing upstream error remains sanitized.
FLUX 3 is not offered until LiteLLM accepts that response.

The authorized FLUX.2 pro alternative was checked with the same LiteLLM request shape,
`size=1024x1024` and two synthetic references. It also returned an image, but its response
omitted `usage.input_tokens_details`, which the pinned parser rejects. It has no entry.
No adapter, response rewriting or substitution is used.

| Correction proof | HTTP | Response | Seconds | Pinned parser | Reported USD |
|---|---|---|---:|---|---:|
| FLUX 3, matching multipart shape | 200 | b64_json | 20.158 | rejects null input token details | not reported |
| FLUX.2 pro, matching multipart shape | 200 | b64_json | 7.327 | rejects missing input token details | not reported |

A model with a combined size field publishes only `sizes`, a list of
`{value, aspect_ratio, resolution, price}`. Models with separate fields publish
`aspect_ratios` and either `resolutions` or `qualities`, each naming a distinct request
field. A profile never carries both forms. The GPT entries already use `size` and
`quality`; Nano Banana 2.1 already uses separate nested aspect-ratio and image-size fields.
Their profiles are unchanged.

For the two `images/edits` profiles, send an OpenAI multipart request to
`POST /v1/images/edits` with the purpose as `model`, a `prompt`, `response_format=b64_json`,
and repeated `image[]` file parts. Without references, send a JSON request to
`POST /v1/images/generations`. Nano Banana 2.1 instead uses `/v1/chat/completions`:
send `modalities: ["text", "image"]` and a user message containing text and reference
`image_url` parts with data URIs; images return in `choices[0].message.images`.
All four allow 300 seconds per request with no retries or fallbacks. No image processing
or prompt changes happen on the node. Prepare references at up to 2048 pixels per side in
the client. No smaller gateway request-body limit was found in the published image docs;
this is a client-side size recommendation, not a proved maximum byte count.

Settings publish `field` and `values`. A `size` mapping associates aspect ratios with
explicit dimensions; resolution choices are square dimensions, and clients compute the
dimensions for other shapes. Dotted field paths denote nested JSON, for example
`providerOptions.google.imageConfig.imageSize`. Only those published nested paths are
accepted; `providerOptions.gateway` and routing overrides remain refused. `advanced`
publishes pass-through request fields and their defaults; no image seed is offered.
Wrong fields receive 400 before an upstream call. Supported OpenAI fields still depend
on the selected model; the gateway validates parameter values.
GPT image models always return base64; `response_format=url` is refused. Their entries use
LiteLLM's native `additional_drop_params: [response_format]` to avoid its generation-parameter
validation rejecting this legacy selector on edits. Both routes use the implicit base64
default. No image format conversion is performed.

Only image entries use the dedicated image key. Its $20 monthly budget is set in Vercel,
resets on the 1st at 00:00 UTC, and is a soft cap: the request crossing the budget completes.
Later calls return HTTP 402 with `error.type: quota_for_entity_exceeded` and the fixed
message `Monthly image budget exhausted; resets on the 1st at 00:00 UTC`.
Do not retry until the reset or an owner budget change. Chat uses its existing key.

Profile sources, checked 2026-10-14: the [gateway catalogue](https://ai-gateway.vercel.sh/v1/models),
[gateway image API](https://vercel.com/docs/ai-gateway/modalities/image-generation/openai),
[Nano Banana 2.1](https://ai.google.dev/gemini-api/docs/models/gemini-nano-banana-2.1),
[Gemini images](https://ai.google.dev/gemini-api/docs/image-generation),
[GPT image settings and cost calculator](https://developers.openai.com/api/docs/guides/image-generation),
and [FLUX 3 gateway announcement](https://vercel.com/changelog/flux-3-image-now-available-on-ai-gateway).
GPT estimates use the official calculator's output tokens at 1024x1024 and the gateway's
$30/M output token rate, excluding prompt and reference input tokens. Both variants have
the same rate, but actual consumption can differ. Gemini estimates use the gateway's
resolution table, excluding additional input/text costs; FLUX uses its gateway size table.
Terms: [Google](https://ai.google.dev/gemini-api/terms),
[OpenAI](https://openai.com/policies/terms-of-use/),
[Black Forest Labs](https://bfl.ai/legal/terms-of-service).

`embed` uses Nemotron 1B; `embed-large` uses Qwen3-Embedding-4B Q8_0 on the node's CPU,
always on. `embed-large-bulk` serves the same vector space from the Mac for bulk ingest only;
students’ questions keep using the node’s `embed-large`. It has no tier or fallback and fails
with a 3-second connection limit when the Mac is stopped. All three embedding purposes accept
optional `input_type: "query"` or `"passage"`
on `/v1/embeddings`, for a string or list of strings. LiteLLM applies the model's configured
question/document wording and removes the field before forwarding. Other values and token
arrays with `input_type` receive 400. Without it, input is forwarded unchanged, preserving
existing indexes. Switching an existing index to roles requires re-embedding its passages
with `passage`, then querying with `query`; this repository's index and `/answer` keep plain
input. Changing embedding models requires a separate index.

`embed-large` returns 2,560 dimensions. Clients using HNSW should account for
[pgvector's limits](https://github.com/pgvector/pgvector#hnsw): `vector` supports at most
2,000 indexed dimensions, while `halfvec` supports 4,000. The proxy passes through the
model's vectors and does not implement dimension reduction.

A client project's network declaration is:

```yaml
services:
  client:
    # image, environment and command belong to the client project
    networks: [models]
networks:
  models:
    external: true
    name: homelab-models
```

## Mac vision serving

Vision and bulk embeddings share one native Homebrew llama.cpp router on the Mac. From
this checkout, outside the Codex sandbox (which cannot see Metal):

```bash
mac/homelab-router start
mac/homelab-router status
mac/homelab-router stop
```

`start` installs a user LaunchAgent (`org.homelab.router`) and waits up to 120 seconds for
health. It runs at login and launchd restarts it after exit. It discovers the Tailscale IPv4
using the CLI (checking `/usr/local/bin` and `/opt/homebrew/bin` even under launchd's minimal
PATH) or an unambiguous `utun` address, binds only that address on TCP 8090, and
refuses an occupied port. The owner's existing node-only tailnet grant gates access without
an API key. No new port is needed; 8092 is freed. Allow incoming connections for `llama-server`
if macOS asks. No wake lock is installed. The action listener (8091) and dictation cleanup
server (8095) keep their independent services and lifecycles.

The native router loads the requested model on the first call. With no requests, llama.cpp
unloads its model weights and KV cache after `IDLE_SECONDS` in `mac/homelab-router` (600 seconds
by default). `/models` then reports `sleeping` (or `unloaded` before the first request); a
sleeping child process remains and reloads on demand. Startup loads neither preset. The
preset template is `mac/router-models.ini.in`; absolute model paths are rendered privately
at installation, never committed. A private cache under router state keeps unrelated cached
models out of native discovery. llama.cpp owns model management; the usage profile command
can restrict which presets are available (see below).

`status` prints only the two preset names/states and current memory in bytes: router RSS,
router plus descendants' RSS, and their summed Darwin physical footprint (including Metal).
It calls `/health` and `/models`, which do not wake models. Compare the current family
physical footprint after inference with the same field after the idle interval; lifetime peak
memory cannot establish release. `status` exits 0 only for a healthy managed process and
checks PID start time. Output excludes addresses, paths and upstream arguments.
`stop` unloads the LaunchAgent, waits up to 30 seconds for the managed process, and removes
its plist so it stays stopped across login; `start` reinstalls it. Commands serialize with a
lock. State and generated presets are private under `~/.local/state/homelab-router/`; the
private plist is under `~/Library/LaunchAgents/`. Native content logging is disabled and
stdout/stderr go to `/dev/null`. Launcher failures append one fixed, content-free stage line
per failed invocation to `startup-failures.log` in router state (creation mode `0600`). For
example, `tailscale address unavailable` records discovery failure without CLI output,
exception text, addresses or paths. The record survives failed-start cleanup and `stop`;
launchd retries can produce additional lines. A failed health wait records a fixed timeout
line, covering native startup failures without retaining native output. No model download is
attempted.

Vision keeps the existing files under
`~/vision-bench-outputs/models/Qwen3-VL-8B-Instruct-GGUF/`:
`Qwen3-VL-8B-Instruct-Q4_K_M.gguf` and `mmproj-F16.gguf`. Its `vision` preset retains Metal
`-ngl 99`, context 16,384, one slot, flash attention and cache RAM 0. Temperature, completion
limit, prompt and page DPI belong to clients. Image inputs must be `data:` URLs.

The retired `mac/homelab-vision` and `mac/homelab-embed` commands only point here. Before
installing the router for the first time, stop both old processes with the previous launchers
(their `stop` commands check PID start time), then leave their old state unused.
The old vision state folder has been removed; retain `~/.local/state/homelab-embed/` because
it also contains historical download/compare files outside this slice. `start` refuses
any live legacy process. Model files stay in place. The vision pointer preserves address
discovery for the unchanged action listener.

Set **both** `HOMELAB_MAC_VISION_URL` and `HOMELAB_MAC_EMBED_URL` in the node clone's private,
root-owned `.env` to the **same** Mac Tailscale HTTP URL on port **8090**, ending in `/v1`.
Never put the address or tailnet name in git, logs or PRs. Empty or unset values retain
Compose's closed loopback default, so LiteLLM starts independently of the Mac. Both purposes
keep the 3-second connect limit and 300-second request limit, without retries, fallback or
stopped-Mac alerts. Cold model loading is included in the request limit.

## Mac bulk embedding serving

`embed-large-bulk` uses the same router described in [Mac vision serving](#mac-vision-serving).
The native preset name remains `Qwen3-Embedding-4B`, matching the gateway's upstream model.
Both node URLs point at port 8090; nothing listens on 8092 for bulk embeddings anymore.

Download the official GGUF once to `~/homelab-models/Qwen3-Embedding-4B-GGUF/` from
`https://huggingface.co/Qwen/Qwen3-Embedding-4B-GGUF/resolve/f4602530db1d980e16da9d7d3a70294cf5c190be/Qwen3-Embedding-4B-Q8_0.gguf`.
Verify SHA256 `b60ae5ce2dd6a0b77f82cadf21def1f310a3e10cde380ad0081b07a9d416949d`
before starting. Its preset preserves last-token pooling, L2 normalisation, context 8192,
batch/microbatch 2048, four threads and Metal `-ngl 99`. The purpose reports
`Qwen3-Embedding-4B` like `embed-large`, with identical optional `input_type` wording
(plain passages). Bulk ingest and node queries stay in the same vector space.

Before any stack restart, inspect med-ask processes and recent audit metadata:

```bash
ssh homelab-agent 'sudo docker ps --format "{{.Names}}"'
ssh homelab-agent 'sudo ps -eo pid,comm,cgroup | rg "med-ask|ingest"'
ssh homelab-agent 'sudo journalctl --since "10 minutes ago" -o cat | rg "model_call purpose=(vision|embed|embed-large|embed-large-bulk) " | tail -n 30'
```

Inspect the med-ask container's process names with `sudo docker top <container> -eo pid,comm`
and its ingest status without printing arguments or content. Recent continuous model calls
or a running ingest mean wait for completion; do not restart `homelab.service`.
Before merge, prove a cold synthetic-image vision call directly from the node, compare
20 generic sentence embeddings against the node's `embed-large` (lowest cosine at least
0.999), then record status and physical footprint before/after the full idle interval.
The direct probe uses the container's private vision URL for both models, before changing
node configuration:

```bash
ssh homelab-agent 'sudo docker exec -i homelab-litellm-1 python -' < tests/mac_router_probe.py
```

It requires both models initially unloaded or sleeping, sends only synthetic inputs, verifies
the real embedding model and dimensions, and prints timings/cosine without URLs or vectors.
Run `mac/homelab-router status` on the Mac immediately after the probe and again after at
least the idle interval with no inference calls; neither status call wakes a model.

After the approved merge and deployment, test vision and bulk through `homelab-models`
without starting a model by hand; stop the router and time both failures (about 3 seconds).
Restart the router outside the sandbox, then check `embed-large`, `embed`, `chat` and image
routes. Leave the router running.

## Mac usage profiles

`mac/homelab-profile` is the command interface for usage profiles. Run it outside the
sandbox. `status` emits one JSON object: `profile` (`working`, `inference`, or `custom`),
`services`, `settings`, `apps`, `running_jobs`, `settings_skipped`, `return_pending`,
`paused` and `pause_reason`. A normal pause reports `profile: inference`, `paused: true` and
`pause_reason: on_battery`; manual differences can still make the observed profile custom.
It observes launchd, running apps, power settings and native router presets rather than
trusting the last selected profile. Starting/stopping a service or editing a setting makes
it `custom` unless it exactly matches a configured profile. Working excludes the owner’s
system settings from matching. The JSON `settings` power mode/sleep describe AC settings,
even while on battery; GPU limit is global. Model loading/sleep is activity,
not a profile change. An unavailable router reports its model services disallowed.

```bash
mac/homelab-profile install
mac/homelab-profile status
mac/homelab-profile switch working
mac/homelab-profile check-work
mac/homelab-profile switch inference
mac/homelab-profile start vision
mac/homelab-profile stop vision
mac/homelab-profile job list
mac/homelab-profile job add example --calendar '{"Hour":4,"Minute":0}' -- "$PWD/mac/profile-example-job"
mac/homelab-profile job run example
```

Installation creates a private `~/.config/homelab/profiles.json` with editable defaults and
`org.homelab.profile`, a user LaunchAgent that checks every 30 seconds. It does not switch
profiles. Install/start the router first. The repository's `mac/profiles.example.json` has an
empty placeholder app list; `install` defaults Inference's list to Chrome and VS Code bundle
identifiers. Keep the owner's edited app list and paths only in the private file, never git.
Profiles contain `services`, `close_apps` (bundle identifiers), `settings` and `jobs` (boolean).
`return_seconds` defaults to 900; `idle_seconds` for jobs defaults to 300.

Working allows bulk embeddings, cleanup and VoiceInk and closes no apps. Its `settings`
are empty: it applies no sleep or power-mode default and does not match the owner’s system
settings. The owner keeps separate choices for AC and Battery. Legacy Working power-mode,
sleep and GPU fields remain readable: they are checked against the numeric bounds, then
ignored without rewriting the private file. Vision and unregistered future image presets
are excluded. Inference permits both router purposes, stops dictation, politely quits the
configured apps, selects High Power and disables sleep **only for AC** (`pmset -c`), and
sets the global GPU wired limit to 45056 MB. Battery power mode/sleep and both sources’
display settings are untouched. Inference requires AC power: `pmset -g batt` is checked before any switching
side effect, with the clear refusal `Inference requires AC power; connect the charger before
switching`. The helper rechecks AC before applying Inference settings. Its power mode must
be 2 (High Power). Only an explicit owner `switch inference`, or the owner's
[night window](#menu-bar-app-usage-and-night-window), enters it. Before any side effect,
that command lists recognised `codex exec`, model servers and GPU jobs descending from
VS Code and refuses. Python and other possible GPU runtimes are conservatively included even
when their script names do not mention inference. `--confirm-kill` is the owner's explicit override. Never use that override
with the production app list from a chat terminal. Jobs inherit a marker that refuses entry
into Inference, even with the confirmation flag; worker ancestry also catches wrappers that
strip environment variables. Registered job executables are owner-trusted
code, not a sandbox for untrusted programs.

When every router model has remained unloaded/sleeping and no job has run for 15 minutes,
the LaunchAgent returns to Working, except while paused. A short unplug has no profile
effect. The existing 30-second check records the first battery observation; after 10 continuous
minutes on battery it pauses Inference, empties the router allow-list and unloads every model.
Apps and dictation stay closed. Starting a model service by hand is also refused while paused,
and no scheduled job starts. Sleep prevention and the GPU limit stay unchanged. Power-mode
changes on battery do not make the owner-entered Inference session custom.

Paused Inference waits for power or the owner; it never returns to Working merely because the
idle timeout elapses. Replugging resets the battery timer and automatically resumes the same
Inference session, applying High Power before restoring its allow-list. Resumption starts a
fresh 15-minute idle window without replacing the original app/service or settings record.
If replugging is observed before ten minutes, the grace timer resets; the ordinary idle rule
still applies during this grace period. The owner can `switch working` at any time, including
while paused on battery, to restore everything. If that return fails partway through, the
existing check retries Working restoration instead of resuming Inference on AC. A private,
atomic recovery record at `~/.local/state/homelab-profile/session.json` records apps and
services stopped by the switch, `previous_powermode`, `previous_sleep` (both AC), and
`previous_gpu_limit_mb` (global) before any change. AC values come from `pmset -g custom`,
not the active-source `pmset -g` view. The return reopens only those apps/services and restores
all three recorded values exactly. An ordinary Working switch with no pending return writes
no power settings; it only resets a nonzero orphan GPU limit to zero. The root helper reads the record while it is pending; it is cleared only after
settings and app/service restoration succeed. Apps/services already stopped remain stopped
(so the resulting profile may be `custom`). A failed/cancelled quit retains the recovery record; explicitly `switch working`
to recover. The command relaunches apps; window and tab restoration follows each app’s own
settings. In Chrome, choose “Continue where you left off” if desired. macOS’s “Close windows
when quitting an application” controls session restoration for apps using the system behaviour.
VS Code restores windows by default. The owner chooses these settings; the command changes
none of them and never force-quits an app. The listener on 8091, the cleanup plist and VoiceInk settings are untouched.

The allow-list is a private router `allowed.json`; rendering omits disallowed presets without
changing any model flags. Native `/models?reload=1` reconciles the presets and unloads removed
models; the command verifies the resulting list. Allowed models keep their processes. This
requires a llama.cpp build supporting native preset reload. A mismatch fails the switch.
The gateway checks the native model list before Mac inference, using the existing private URL
and connection limit. A dedicated private `public/` directory under router state holds only
`homelab-profile.json`, served by llama.cpp’s native `--path` option on the existing router
address and port. It publishes exactly one boolean, `paused_on_battery`, never private profile
contents or paths. See the [native static-path option](https://github.com/ggml-org/llama.cpp/blob/master/tools/server/README.md).
The updated router must be restarted once to enable that path; do so outside the sandbox when
no ingest needs its models. This adds no listener or request proxy. A missing preset returns OpenAI-shaped HTTP 503 with, for example,
`vision is not available until Inference`. Its existing audit line adds
`profile_blocked=true`, with purpose and `upstream=false`, so waiting requests can be counted
without content. During battery pause the 503 instead says, for example,
`vision is paused because the Mac is on battery`, with `profile_paused=true`. Only the native
metadata boolean selects this fixed message. Missing/invalid metadata falls back to the normal
availability refusal; upstream bodies and diagnostics are never passed through. Other upstream errors retain sanitising. Clients retain their own queues;
requests never change profiles. A manual stop of a purpose has the same refusal wording.

Each registered job has its own launchd `StartCalendarInterval`. It runs only in an exact
profile permitting jobs, on AC power, after keyboard/mouse idle time. A missed run leaves one
pending bit; the next eligible idle check asks launchd to run it. Missed occurrences coalesce;
there is no work queue. Jobs execute under launchd, outside VS Code, with output discarded and
only PID identity, pending state and exit code recorded privately. The included example exits
successfully without doing work. `job run` also uses launchd and the same gates. Reinstalling
the main agent preserves job agents and state.

**One-time privileged installation, after merge, by the owner:** inspect the script and
sudoers template first. Verify `/usr/local` is root-owned and not writable by the owner.
The first command creates `/usr/local/sbin` if absent; verify it is root-owned and not writable
by the owner before installing the helper. Do not install this grant in a user-writable directory. From the merged
clone, typing the password:

```bash
sudo install -d -o root -g wheel -m 0755 /usr/local/sbin
sudo install -o root -g wheel -m 0755 mac/homelab-profile-apply /usr/local/sbin/homelab-profile-apply
PROFILE_SUDOERS=$(mktemp)
sed "s/@OWNER@/$(id -un)/g" mac/homelab-profile.sudoers.in > "$PROFILE_SUDOERS"
sudo visudo -cf "$PROFILE_SUDOERS"
sudo install -o root -g wheel -m 0440 "$PROFILE_SUDOERS" /private/etc/sudoers.d/homelab-profile
rm "$PROFILE_SUDOERS"
sudo visudo -c
```

The grant permits exactly `/usr/local/sbin/homelab-profile-apply working` and the same command
with `inference`, as root without a password. No other arguments, shell, arbitrary sysctl or
pmset command is granted. The root-owned script uses `/usr/bin/python3 -I` and fixed absolute
executables, no imports from the checkout and no shell. It reads only the invoking sudo user’s
private profile file and (for a pending Working return) `session.json`, rejecting final symlinks,
foreign ownership, nonregular/oversize files and nonnumeric settings. Inference always sets
High Power; a return accepts only recorded integer power modes 0–2, AC sleep 0–180 minutes
and GPU wired limits 0–45056 MB. Inference requires a complete pending recovery record
before writes. Ordinary Working does not apply configuration defaults; without a pending
return it only clears a nonzero orphan GPU limit. All pmset writes use `-c`; Battery settings
are never written. Zero GPU limit restores the system default; it also resets at reboot.
A partial system-setting failure retains the recovery record; returning to Working retries.
Older pending records containing only a power mode are refused, rather than inventing sleep
or GPU recovery values. Finish an existing return before updating the command/helper.
The sudoers file needs no change when reinstalling the corrected helper:

```bash
sudo install -o root -g wheel -m 0755 mac/homelab-profile-apply /usr/local/sbin/homelab-profile-apply
```

After reinstalling from the merged checkout, the orchestrator runs this **outside the sandbox**
as the owner, on AC, with Working services and no pending return or running jobs:

```bash
uv run python tests/mac_profile_privileged_acceptance.py
```

This uses the helper directly, never a production Inference switch or app quit. It checks
that the installed helper matches the checkout before any writes, pauses the profile agent,
saves the original private session and per-source settings, and creates a complete bounded
recovery record. `sudo -k -n` verifies both names without cached credentials or a prompt;
missing, unknown and extra arguments must fail. Readback uses `pmset -g custom` and sysctl:
Inference must select AC mode 2, AC sleep 0 and GPU 45056, leaving every Battery setting and
other AC setting unchanged. Working must restore the captured AC mode, AC sleep and GPU
limit exactly, with the full per-source readback identical to the original. The proof restores
private session state and reloads the agent in cleanup. A restoration failure retains the
recovery record and `~/.local/state/homelab-profile/privileged-acceptance-backup.json`, leaves
the profile agent paused, and must be relayed to the orchestrator. Never use a production
Inference switch from a chat terminal while VS Code must survive.

**Unprivileged acceptance test before merge:** save the private profile file first. Use only
TextEdit (`com.apple.TextEdit`) in the test Inference app list and copy the observed settings
into the test Inference profile, keeping Working settings empty and retaining Inference’s
fixed High Power requirement. Keep the Mac on the charger throughout the cycle. The test skips
settings; if the owner’s observed mode is not High Power, the inference phase correctly
reports `custom` while services and restoration are still verified. Set `return_seconds` to 15. Open
TextEdit with a disposable saved document, start VoiceInk and cleanup, then switch Working
with `--skip-settings`. From the orchestrator terminal, `check-work` and an unconfirmed
Inference switch must list the running chat and refuse without changing anything. After that
proof, only with the test app list verified, use `switch inference --skip-settings --confirm-kill`;
VS Code and Chrome are excluded. Check the stopped apps/services, wait for the automatic
return and confirm TextEdit, VoiceInk and cleanup relaunch. Acceptance checks app relaunch,
not document/window restoration; that follows the owner’s app and macOS settings.
Restore the private configuration afterwards; leave services in Working and report actual
settings (which may make the production profile `custom` until privileged installation).
`--skip-settings` persists across the automatic return and is reported explicitly in status.
To rerun only the cycle from production configuration, use
`uv run python tests/mac_profile_acceptance.py cycle`; it prepares the safe TextEdit-only test
configuration and restores the original configuration afterwards, without repeating the guard proof.
It verifies the captured prior mode and unchanged observed settings, quits TextEdit if the
test launched it, and removes its fixture. Before the first cycle using pause metadata,
restart the router outside the sandbox:

```bash
mac/homelab-router stop
mac/homelab-router start
uv run python tests/mac_profile_acceptance.py cycle
```

The cycle checks the native metadata path before changing test configuration. Unit tests cover
battery entry refusal, the full grace period, timer reset, pause/AC resume, suspended idle return
and manual Working restoration. Pinned gateway tests cover both purposes’ paused messages,
audit flags and invalid metadata. Actual privileged restoration is verified after owner installation.

### Menu bar app, usage and night window

The SwiftUI `MenuBarExtra` app is a thin client of `mac/homelab-profile`: every state, plan,
guard and switch comes from the command's JSON, and the app holds no profile rules. Its menu
shows *Working*, *Inference* (or *Inference, paused* with the reason), or *Custom*, and each
service's state with a Start/Stop button (one service changes the state to *Custom*, as the
command does). *Working* is one click. *Inference…* first runs `plan-inference` and shows the
configured apps it would quit, the dictation it would stop and the VS Code guard's running work,
then asks once. A battery refusal is shown as such, without side effects. The app then runs an
unconfirmed `switch inference`; if the guard refuses, the app repeats the guard's warning and its
work list and offers **Switch anyway** (`--confirm-kill`) or **Cancel**. Never use *Switch
anyway* while a chat or job under VS Code must survive.

**Build, install and start** (from the owner's checkout, outside any sandbox; it switches nothing):

```bash
mac/install-menu-bar.sh
```

It builds `mac/MenuBar` in release mode, replaces `~/Applications/HomeLab.app`, signs it ad hoc
(`codesign --sign -`) and loads the `org.homelab.menu` user LaunchAgent, which starts the app now
and at every login. A LaunchAgent works with an ad hoc signature; no Developer ID is needed. The
bundle's `Info.plist` records this checkout's `mac/homelab-profile` and interpreter
(`.venv/bin/python3`, else `python3`), so the app runs the same command as the
`org.homelab.profile` agent. Rerun the script after pulling changes. The menu bar icon shows
the state: laptop (*Working*), bolt
(*Inference*), pause (*paused*), sliders (*Custom*).

The commands it uses, all JSON on stdout:

```bash
mac/homelab-profile plan-inference      # apps/dictation to stop, guard work, AC refusal
mac/homelab-profile waiting             # refusals per purpose, last 24 hours
mac/homelab-profile usage               # Codex and Claude 5-hour/7-day usage
mac/homelab-profile schedule show
mac/homelab-profile schedule set --start 23:00 --end 07:00 --days mon,tue,wed,thu,fri
mac/homelab-profile schedule off
```

**Waiting work.** `waiting` runs two read-only commands over `ssh homelab-agent` (batch mode,
10-second connect limit): `journalctl -u homelab.service --since '24 hours ago'` filtered to
`profile_blocked=true` audit lines, and the public `GET /v1/model/info` from inside the LiteLLM
container (it publishes no host port). It counts `model_call` lines per purpose. An estimate is
count × the purpose's `seconds_per_image`; purposes publishing none (today `vision` and the
embeddings) show the count with “estimate unavailable”. Nothing is written on the node or
locally, and there is no queue. Unreachable node: the menu says waiting work is unavailable.

**Usage, on *Refresh*.** `usage` reads local files only, without writing any:

- Codex: the newest `rate_limits` entry in `~/.codex/sessions/**/*.jsonl` with `primary` 300 and
  `secondary` 10,080 window minutes (other limit families are ignored), newest files first.
  `used_percent` and `resets_at` become the 5-hour and 7-day figures, as of the entry's timestamp.
- Claude: `~/.claude/usage-now.json`, which the owner's status line already writes whenever a
  Claude chat renders it: `{"at": <epoch seconds>, "rate_limits": {"five_hour": {...},
  "seven_day": {...}}}` with `used_percentage` and `resets_at`. The figures are as of `at`.
  A missing or invalid file shows Claude as “unknown”. Nothing in the status line needs changing.

Each source reports `age_seconds` from its own timestamp, and the menu shows it beside the
“as of” time. Usage values stay on the Mac and out of git.

**Night window.** Off by default. Set it in the menu (start, end, days, *Save*; the switch turns
it on or off) or with `schedule`; it is stored in private `~/.config/homelab/night-window.json`.
The existing `org.homelab.profile` agent (installed by `mac/homelab-profile install`) evaluates
each selected night once, within five minutes after its start time. It enters Inference only when
the observed state is exactly *Working*, the Mac is on AC and the VS Code guard finds no running
work; the switch itself repeats the AC, job and guard checks. Otherwise, including a Mac that was
asleep at the start time, that night is skipped and the menu shows the reason. Saving the window
while inside it never enters it that night. The usual 15-minute idle return and battery pause
still apply. At the end time the agent returns to Working only if the window entered the session
and no manual switch, Start/Stop or `schedule off` has happened since; those end its ownership.
Days name the night by its start day. Jobs and gateway requests still never enter Inference; a
job cannot set the window, and the window's entry is refused in job context.

**Turning it all off:**

```bash
mac/homelab-profile schedule off
launchctl bootout "gui/$(id -u)/org.homelab.menu"
rm "$HOME/Library/LaunchAgents/org.homelab.menu.plist"
rm -rf "$HOME/Applications/HomeLab.app"
```

The profile agent and its idle return and jobs keep working without the app. The app wrote
nothing for usage, so there is nothing else to remove.

## Mac action listener

The owner's launchd user agent accepts only `POST /backup` with an empty body on TCP 8091,
bound to the Mac's runtime Tailscale IPv4. Extend the tailnet grant so only the node can reach
this port before installing it. There is no listener key; the tailnet rule is the gate.
The agent runs as the logged-in owner, using the same SSH key and `homelab-agent` configuration
as the manual copy. Keep this checkout and its Python interpreter at their installed paths.
From this checkout, using a persistent Python 3 installation:

```bash
python3 mac/homelab-listener install
mac/homelab-listener status
mac/homelab-listener stop
mac/homelab-listener start
mac/homelab-listener uninstall
```

Install writes `~/Library/LaunchAgents/org.homelab.listener.plist` with mode `0600`. launchd
starts it at login and restarts it after exit; `stop` unloads it until `start` or the next login.
Uninstall removes the plist and unloads the agent, retaining private logs in
`~/.local/state/homelab-listener/listener.log` (directory `0700`, creation umask `0077`).
`status` reports the launchd process state; confirm its actual binding with
`lsof -nP -iTCP:8091 -sTCP:LISTEN`. Only the Tailscale address must appear. Tailscale being down
prevents binding; launchd retries. The agent does not keep the Mac awake.
Click Allow if macOS asks about removable-volume access or incoming connections. If an Allow
click cannot resolve volume access, stop and investigate before changing the security setup.

Requests carry no arguments, paths or commands; other actions or request bodies receive 404.
One copy runs at a time; concurrent requests receive 409 (`busy`). The listener checks for
`/Volumes/SD Card`, runs the existing copy command with a 180-second timeout, and returns JSON
status `no-card`, `done` (file name and bytes only), or `failed` (fixed short reason).
A timeout kills the copy and its SSH child; partial files remain and are never overwritten.
Default HTTP access logging is disabled; the private log contains result and byte metadata.
No copy content passes through this channel.

After merging, install the changed bot files and reminder from the node clone after showing
`sudo diff`, using the modes in AGENTS.md. Configure the private listener URL as described in
[Rebuilding the node](#rebuilding-the-node), then restart only
`homelab-telegram-bot.service`. Validate `/backup` with the card ejected, inserted, and the
agent stopped; restart the agent afterwards. Verify the card file and
`sudo homelab-copy status` timestamp/bytes on the node. From the node, an empty POST to an
unknown listener path must return 404. The privileged router gate also applies to `/backup`.

## Mac vision measurements

`mac/vision_bench.py` benchmarks native Metal llama.cpp on macOS. It starts each supplied
GGUF with its projector on `127.0.0.1`, measures launch-to-health readiness and Darwin's
lifetime peak physical footprint plus the GGUF and projector file sizes, then stops it,
including on interruption. This sum estimates memory including mapped weights; it may
double-count resident mappings and is not a direct resident-set measurement. No service is
installed and no node or model route is changed. Install `brew install llama.cpp`, then run:

```bash
uv run --group benchmark python mac/vision_bench.py --help
uv run --group benchmark python mac/vision_bench.py \
  --model '<NAME>' '<WEIGHTS-GGUF>' '<MMPROJ-GGUF>' \
  --sample '<PDF-OUTSIDE-GIT>' '<ONE-BASED-PAGE>' en prose score \
  --results-dir "$HOME/vision-bench-outputs/numbers" \
  --outputs-dir "$HOME/vision-bench-outputs"
```

Repeat `--model` and `--sample` for identical samples across sequential candidates.
Languages are `en`/`es`; kinds are `prose`/`table`/`figure`/`scan`; modes are `score`/`judge`.
Defaults: 150 DPI, 16384 context tokens, 4096 completion tokens, temperature/seed zero,
no prompt cache. Page timings cover HTTP image prefill and generation; rendering is separate.
The default prompt and figure descriptions remain available. Use `--prompt exact` for the
verbatim transcription prompt and `--no-figures` to omit figure descriptions. Reconciliation
uses `--reconcile '<OUTPUT-A-DIRECTORY>' '<OUTPUT-B-DIRECTORY>' --dpi 300`: each indexed
`NNN.text.txt` pair and one image go into one chat request with the fixed reconciliation prompt.
Missing/empty inputs record a page failure. Supply identical ordered samples for both inputs.
Use `--run-name v2-r1` (one model per invocation) to name both the output subdirectory and
numeric JSON file; existing result files or output directories are refused.

Normalisation applies Unicode NFKC, removes soft hyphens, joins words hyphenated at line ends,
casefolds and collapses whitespace. Word bags use whitespace tokens, retaining punctuation
and repeated-word counts. Primary recall, precision and F1 aggregate matched token counts over
all valid reference/output tokens, independent of reading order. CER/WER remain secondary
Levenshtein scores after the same normalisation. References are `invalid_reference` when
fewer than 80% of whitespace tokens are alphabetic after stripping Unicode punctuation
(an empty reference is invalid). Invalid references are reported and excluded from accuracy
aggregates; transcription still runs for visual review. Judgement pages have no accuracy score.
Failure codes: 0 success, 1 request/render/input error, 3 truncated/non-stop completion,
4 server startup failure. Startup failures record every sample. Latency includes failures;
token means include successful calls only. Results report total invocation duration, cold load,
file-size bytes, peak footprint bytes and their sum as `peak_memory_bytes`.

Results contain numeric measurements only (language codes: en=0/es=1; kind codes:
prose=0/table=1/figure=2/scan=3), with sample indices instead of paths or page numbers.
Optional outputs retain images, reference text, transcriptions and figure descriptions for
local review. All inputs and output directories must be outside every Git repository;
nothing the benchmark reads or writes is committed. Keep private sample arguments locally.
PDF rendering and scoring dependencies live only in the `benchmark` group, outside app images.
Cold load means a fresh process, without evicting the operating system's file cache.

## Rebuilding the node

Recovery is a rebuild from sources. No node backup exists or will be made. Host files come
from `node/`, code from this public GitHub repository, and Brain notes from their own GitHub
repository through the owner's Mac clone. Brain is copied from the Mac; originals can be restored from the encrypted weekly copy
(see [Restoring originals from a copy](#restoring-originals-from-a-copy)). The corpus can
also be copied from the owner’s Mac;
Postgres is derived by re-ingesting, models are fetched upstream, and secrets are re-issued.
The rebuilt node holds no GitHub credential and never clones Brain.

This procedure describes the node verified on 2026-10-05: Ubuntu 26.04.1 LTS, kernel 7.0,
systemd 259, Docker 29.8 with Compose v5.5, and Tailscale 1.102. Vendor apt repositories supply
Docker and Tailscale; check the versions available when rebuilding. These commands have not
been rehearsed. Run them only during an owner-authorized rebuild, stopping on any error.
Commands run on the node as `aleix` unless marked **on the Mac**. Replace angle-bracket
placeholders locally; never commit their values. Keep the local console available until both
SSH accounts work through Tailscale.

1. **Install Ubuntu and choose the system disk.** In the Ubuntu Server 26.04.1 LTS installer,
   select the **Samsung MZ7TY256, 238.5 GiB**, by model and size. Never identify the target by
   `sda` or `sdb`: kernel names can swap between boots. Leave the **Micron MTFDDAV512TBN,
   476.9 GiB**, deliberately unused, untouched. **Choosing the wrong disk in the installer
   is the one irreversible mistake in this procedure: the installer destroys that disk's
   data.** Check the model and size again before confirming the partition changes.

   Use three partitions on the Samsung: 1 GiB EFI at `/boot/efi`, 2 GiB at `/boot`, and the
   remainder as an LVM physical volume in `ubuntu-vg`. Create only `ubuntu-lv`, 64 GiB,
   mounted at `/`; leave at least 128 GiB free in the VG for step 2. Create the owner's
   `aleix` login with sudo access and select OpenSSH server. Configure Wi-Fi on `wlp1s0`
   with the Wi-Fi name and passphrase from the owner's password manager. Ubuntu's installer
   writes the private netplan; it is never copied into this repository. `eno1` is optional.
   After boot, verify the target layout and install the host utilities used below:

   ```bash
   lsblk -o NAME,MODEL,SIZE,TYPE,MOUNTPOINTS
   sudo vgs ubuntu-vg
   sudo lvs -o lv_name,lv_size ubuntu-vg
   sudo apt-get update
   sudo apt-get install ca-certificates curl git rsync openssh-server lvm2 cryptsetup ufw iw chrony polkitd python3 openssl age
   ```

2. **Create, format and register the encrypted data volume.** These commands apply only to
   the new, empty `data` LV in the Samsung's `ubuntu-vg`; do not run them on an existing
   volume. Create a new data-volume passphrase in the owner's password manager and enter
   it at cryptsetup's hidden prompts. No keyfile or UUID is recorded here.

   ```bash
   sudo lvcreate -L 128G -n data ubuntu-vg
   sudo cryptsetup luksFormat --type luks2 /dev/ubuntu-vg/data
   sudo cryptsetup open --allow-discards /dev/ubuntu-vg/data homelab-data
   sudo mkfs.ext4 /dev/mapper/homelab-data
   sudo cryptsetup close homelab-data
   sudo install -d -m 0755 -o root -g root /srv/homelab
   sudoedit /etc/crypttab /etc/fstab
   ```

   Add exactly one entry to each file, leaving Ubuntu's existing entries intact:

   ```text
   # /etc/crypttab
   homelab-data  /dev/ubuntu-vg/data  none  luks,noauto,discard
   # /etc/fstab
   /dev/mapper/homelab-data  /srv/homelab  ext4  noauto,nofail,x-systemd.device-timeout=10s  0  2
   ```

3. **Install and join Tailscale.** Use the [vendor's apt repository](https://pkgs.tailscale.com/stable/)
   for Ubuntu Resolute. Authenticate through the URL printed by `tailscale up`, using the
   owner's Tailscale account. The access policy lives in the admin console: verify that it
   permits the owner's Mac to reach this replacement node. Its policy, tailnet name, email
   and addresses stay outside this repository. Update the Mac's local `homelab` and
   `homelab-agent` SSH aliases to the replacement node's Tailscale hostname or address.

   ```bash
   curl -fsSL https://pkgs.tailscale.com/stable/ubuntu/resolute.noarmor.gpg | sudo tee /usr/share/keyrings/tailscale-archive-keyring.gpg >/dev/null
   curl -fsSL https://pkgs.tailscale.com/stable/ubuntu/resolute.tailscale-keyring.list | sudo tee /etc/apt/sources.list.d/tailscale.list >/dev/null
   sudo apt-get update
   sudo apt-get install tailscale
   sudo systemctl enable --now tailscaled.service
   sudo tailscale up
   tailscale version
   tailscale status
   ```

4. **Install Docker and Compose.** Use [Docker's Ubuntu apt instructions](https://docs.docker.com/engine/install/ubuntu/).
   On this fresh Ubuntu install, omit distro Docker packages. The signed repository below
   is the installed node's `docker.list` form of the vendor's repository configuration.

   ```bash
   sudo install -d -m 0755 /etc/apt/keyrings
   sudo curl -fsSL https://download.docker.com/linux/ubuntu/gpg -o /etc/apt/keyrings/docker.asc
   sudo chmod 0644 /etc/apt/keyrings/docker.asc
   echo "deb [arch=$(dpkg --print-architecture) signed-by=/etc/apt/keyrings/docker.asc] https://download.docker.com/linux/ubuntu $(. /etc/os-release && echo "$VERSION_CODENAME") stable" | sudo tee /etc/apt/sources.list.d/docker.list >/dev/null
   sudo apt-get update
   sudo apt-get install docker-ce docker-ce-cli containerd.io docker-buildx-plugin docker-compose-plugin
   sudo docker version
   sudo docker compose version
   ```

5. **Create the accounts and their restricted SSH keys.** The owner is in `sudo` and
   `docker`. The agent has an interactive shell and journal access; its root sudo grant
   is installed in step 6. The bot is a system account with no login and no Docker group.

   ```bash
   sudo usermod -aG sudo,docker aleix
   sudo useradd --system --user-group --create-home --home-dir /home/homelab-agent --shell /bin/bash homelab-agent
   sudo usermod -aG systemd-journal homelab-agent
   sudo useradd --system --user-group --no-create-home --home-dir /nonexistent --shell /usr/sbin/nologin homelab-bot
   sudo install -d -m 0700 -o aleix -g aleix /home/aleix/.ssh
   sudo install -d -m 0700 -o homelab-agent -g homelab-agent /home/homelab-agent/.ssh
   sudo install -m 0600 -o aleix -g aleix /dev/null /home/aleix/.ssh/authorized_keys
   sudo install -m 0600 -o homelab-agent -g homelab-agent /dev/null /home/homelab-agent/.ssh/authorized_keys
   sudoedit /home/aleix/.ssh/authorized_keys /home/homelab-agent/.ssh/authorized_keys
   ```

   Put the owner's public login key from the Mac in the owner's file. In the agent's file,
   use its public key from the Mac with the restriction shown below, substituting the Mac's
   Tailscale addresses locally. Keep both `authorized_keys` files and all key material out
   of git. No outgoing GitHub key or GitHub SSH config is needed on the node.

   ```text
   from="<MAC-TAILSCALE-IPV4>,<MAC-TAILSCALE-IPV6>" <AGENT-PUBLIC-KEY>
   ```

   The installer generates new SSH host keys. Check their fingerprint at the node's console:

   ```bash
   sudo ssh-keygen -lf /etc/ssh/ssh_host_ed25519_key.pub
   ```

   **On the Mac**, remove only the old known-host entry for the replacement node, compare
   the new fingerprint with the console, and test both logins before applying hardening or ufw:

   ```bash
   ssh-keygen -R '<NODE-TAILSCALE-HOST-OR-ADDRESS>'
   ```

   ```bash
   ssh homelab 'id'
   ssh homelab-agent 'id'
   ```

6. **Apply `node/` with its recorded modes.** Before the encrypted-volume clone exists,
   download the public merged `main` source archive to the owner's home. This is a bootstrap
   copy of committed files, not an installer script. Inspect each difference before copying;
   for an absent destination compare against `/dev/null`. The diff loop exits on an inspection
   error; exit status 1 from `diff` means a displayed difference. Keep vendor stock
   `/etc/ufw/before.rules`. Do not install the stale SSH `.bak-2026-09-12` file.

   ```bash
   mkdir -p "$HOME/homelab-host"
   curl -fsSL https://github.com/TelesforoAleix/homelab-v2/archive/refs/heads/main.tar.gz | tar -xz -C "$HOME/homelab-host" --strip-components=1
   cd "$HOME/homelab-host"
   while IFS= read -r source; do
     destination="/${source#node/}"
     if sudo test -e "$destination"; then
       sudo diff -u "$destination" "$source" || test "$?" -eq 1 || exit 2
     else
       diff -u /dev/null "$source" || test "$?" -eq 1 || exit 2
     fi
   done < <(find node -type f | sort)
   ```

   Run the following only after reviewing all diffs. Directory and file modes are also
   recorded in `AGENTS.md`. Preserve the vendor's `root:polkitd` ownership and `0750` mode
   on `/etc/polkit-1/rules.d`; the rule itself is `root:root` `0644`. All other directories
   below are `root:root` `0755`. The firewall files are `0640`, SSH hardening is `0600`,
   sudoers is `0440`, operational scripts are `0755`, and the remaining files are `0644`.

   ```bash
   sudo install -d -m 0755 -o root -g root /etc/ssh /etc/ssh/sshd_config.d /etc/ufw /etc/docker /etc/sysctl.d /etc/profile.d /etc/sudoers.d /usr/local/sbin /opt/homelab-telegram-bot /etc/systemd/system
   sudo install -d -m 0755 -o root -g root /etc/systemd/system/homelab-notify@.service.d /etc/systemd/system/homelab-watchdog.service.d /etc/systemd/system/homelab-telegram-bot.service.d
   sudo install -d -m 0750 -o root -g polkitd /etc/polkit-1/rules.d
   sudo install -m 0600 -o root -g root node/etc/ssh/sshd_config.d/10-homelab-hardening.conf /etc/ssh/sshd_config.d/10-homelab-hardening.conf
   sudo install -m 0640 -o root -g root node/etc/ufw/after.rules node/etc/ufw/after6.rules node/etc/ufw/user.rules node/etc/ufw/user6.rules /etc/ufw/
   sudo install -m 0644 -o root -g root node/etc/docker/daemon.json /etc/docker/daemon.json
   sudo install -m 0644 -o root -g root node/etc/sysctl.d/99-homelab-swappiness.conf /etc/sysctl.d/99-homelab-swappiness.conf
   sudo install -m 0644 -o root -g root node/etc/profile.d/homelab-console-timeout.sh /etc/profile.d/homelab-console-timeout.sh
   sudo visudo -cf node/etc/sudoers.d/homelab-agent-v2
   sudo install -m 0440 -o root -g root node/etc/sudoers.d/homelab-agent-v2 /etc/sudoers.d/homelab-agent-v2
   sudo install -m 0644 -o root -g root node/etc/homelab-copy.recipient /etc/homelab-copy.recipient
   sudo install -m 0755 -o root -g root node/usr/local/sbin/homelab-copy /usr/local/sbin/homelab-copy
   sudo install -d -m 0700 -o root -g root /var/lib/homelab-copy
   sudo install -m 0755 -o root -g root node/usr/local/sbin/*.sh /usr/local/sbin/
   sudo install -m 0644 -o root -g root node/opt/homelab-telegram-bot/*.py /opt/homelab-telegram-bot/
   sudo install -m 0644 -o root -g root node/etc/polkit-1/rules.d/50-homelab-bot.rules /etc/polkit-1/rules.d/50-homelab-bot.rules
   sudo install -m 0644 -o root -g root node/etc/systemd/system/*.service node/etc/systemd/system/*.timer node/etc/systemd/system/*.target /etc/systemd/system/
   sudo install -m 0644 -o root -g root node/etc/systemd/system/homelab-notify@.service.d/*.conf /etc/systemd/system/homelab-notify@.service.d/
   sudo install -m 0644 -o root -g root node/etc/systemd/system/homelab-watchdog.service.d/*.conf /etc/systemd/system/homelab-watchdog.service.d/
   sudo install -m 0644 -o root -g root node/etc/systemd/system/homelab-telegram-bot.service.d/*.conf /etc/systemd/system/homelab-telegram-bot.service.d/
   sudo visudo -c
   sudo sshd -t
   sudo systemd-analyze --generators=yes verify /etc/systemd/system/homelab.service /etc/systemd/system/homelab-data.target /etc/systemd/system/homelab-notify@.service /etc/systemd/system/homelab-watchdog.service /etc/systemd/system/homelab-watchdog.timer /etc/systemd/system/homelab-telegram-bot.service /etc/systemd/system/wifi-powersave-off.service /etc/systemd/system/homelab-copy-reminder.service /etc/systemd/system/homelab-copy-reminder.timer
   ```

   `--generators=yes` supplies the mount unit from fstab during verification. Vendor units
   may warn about removed directives; resolve failures in the recorded units before proceeding.

7. **Enable the host units, firewall and sysctl.** Keep a console and an existing SSH session
   open while reloading SSH and enabling ufw; verify a fresh connection from the Mac afterward.
   The recorded rules allow `tailscale0` and Wi-Fi 41641/udp, and the `DOCKER-USER` chain
   drops inbound Docker traffic from `wlp1s0` and `eno1` for IPv4 and IPv6. Leave ufw's stock
   `IPV6=yes` and `MANAGE_BUILTINS=no` in place.

   ```bash
   sudo systemctl daemon-reload
   sudo systemctl reload ssh.service
   sudo systemctl enable --now docker.service tailscaled.service chrony.service wifi-powersave-off.service
   sudo systemctl restart docker.service
   sudo systemctl enable homelab-telegram-bot.service homelab-watchdog.timer homelab-copy-reminder.timer
   sudo ufw default deny incoming
   sudo ufw default allow outgoing
   sudo ufw default deny routed
   sudo ufw --force enable
   sudo ufw status verbose
   sudo iptables -S DOCKER-USER
   sudo ip6tables -S DOCKER-USER
   sudo sysctl -p /etc/sysctl.d/99-homelab-swappiness.conf
   iw dev wlp1s0 get power_save
   sudo mkdir -p /var/log/journal
   sudo systemd-tmpfiles --create --prefix /var/log/journal
   sudo journalctl --flush
   ```

   Persistent journaling lets the watchdog classify the previous boot. Bot and watchdog are
   enabled but not started before credentials exist; the watchdog timer first runs on the
   validation reboot. Defer enabling `homelab.service` until step 12: unlocking starts the
   data target, and the stack must not start before its clone and secrets exist. Do not enable
   `homelab-data.target` or `homelab-notify@.service`; the unlock script and failure handlers
   start them. Console logins now expire after 15 idle minutes; SSH sessions do not.
   **On the Mac**, verify the hardened logins and agent sudo access:

   ```bash
   ssh homelab 'id'
   ssh homelab-agent 'sudo -n true'
   ```

8. **Unlock the volume.** Enter its new passphrase from the owner's password manager.
   This is the same manual step required after every boot.

   ```bash
   sudo data-volume.sh unlock
   findmnt /srv/homelab
   ```

9. **Clone this repository and copy Brain from the owner's Mac.** The public code clone is
   root-owned and uses HTTPS without a GitHub credential. Brain is a copy of the Mac clone's
   working notes, excluding `.git`; keep the private repository URL and GitHub access on
   the Mac. Bring the Mac clone up to date before copying. The Brain directory stays
   owner-owned, and containers mount it read-only. Copy modes let the app UID read the notes.

   ```bash
   sudo git clone --branch main https://github.com/TelesforoAleix/homelab-v2.git /srv/homelab/homelab-v2
   sudo install -d -m 0775 -o aleix -g aleix /srv/homelab/brain
   sudo install -d -m 0755 -o root -g root /srv/homelab/models /srv/homelab/postgres
   sudo mkdir -p /srv/homelab/models/embed-large
   sudo chown 10001:10001 /srv/homelab/models/embed-large
   sudo chmod 0755 /srv/homelab/models/embed-large
   ```

   **On the Mac**, copy notes, including hidden note files but excluding Git metadata:

   ```bash
   rsync -a --chmod=D755,F644 --exclude='.git' '<MAC-BRAIN-CLONE>/' homelab:/srv/homelab/brain/
   ```

10. **Re-issue the secrets and provision the private allowlists.** Create a fresh gateway
    key with the owner's gateway account and budget, a random hexadecimal database password
    in the password manager, and a new Telegram bot token through BotFather. Enter keys and
    the password only at hidden prompts; never put them in shell arguments or history.
    `.env` holds the database password and four volume paths and is `root:root` `0600`.
    Optional `HOMELAB_MAC_VISION_URL` and `HOMELAB_MAC_EMBED_URL` both point to the Mac
    router on port 8090 and live only in this private `.env`, each ending in `/v1`; leave
    them unset for closed defaults.
    Restore them from the owner’s Mac serving configuration, never from Git.
    Create a separate gateway key used only for images and set its $20 monthly budget in
    Vercel. Both gateway keys are `root:10001` `0440`, mounted only into LiteLLM and read
    at startup. Never place either key in `.env`, Compose environment or configuration.

    ```bash
    sudo install -d -m 0700 -o root -g root /srv/homelab/homelab-v2/secrets
    sudo bash -c 'umask 077; read -r -s -p "new gateway key: " K </dev/tty; echo; test -n "$K" || exit 1; printf "%s" "$K" > /srv/homelab/homelab-v2/secrets/gateway_api_key'
    sudo chown root:10001 /srv/homelab/homelab-v2/secrets/gateway_api_key
    sudo chmod 0440 /srv/homelab/homelab-v2/secrets/gateway_api_key
    sudo bash -c 'umask 077; read -r -s -p "new image gateway key: " K </dev/tty; echo; test -n "$K" || exit 1; printf "%s" "$K" > /srv/homelab/homelab-v2/secrets/image_gateway_api_key'
    sudo chown root:10001 /srv/homelab/homelab-v2/secrets/image_gateway_api_key
    sudo chmod 0440 /srv/homelab/homelab-v2/secrets/image_gateway_api_key
    sudo bash -c 'umask 077; read -r -s -p "new hexadecimal database password: " P </dev/tty; echo; [[ "$P" =~ ^[[:xdigit:]]{32,}$ ]] || exit 1; printf "%s\n" "HOMELAB_DB_PASSWORD=$P" "HOMELAB_BRAIN_PATH=/srv/homelab/brain" "HOMELAB_CORPUS_PATH=/srv/homelab/homelab-v2-data/originals/corpus" "HOMELAB_POSTGRES_PATH=/srv/homelab/postgres" "HOMELAB_MODELS_PATH=/srv/homelab/models" > /srv/homelab/homelab-v2/.env'
    sudo chown root:root /srv/homelab/homelab-v2/.env
    sudo chmod 0600 /srv/homelab/homelab-v2/.env
    sudo install -d -m 0750 -o root -g homelab-bot /etc/homelab-telegram-bot
    sudo bash -c 'umask 077; read -r -s -p "new Telegram bot token: " T </dev/tty; echo; test -n "$T" || exit 1; printf "%s" "$T" | systemd-creds encrypt --name=bot-token --with-key=tpm2 --tpm2-pcrs="" - /etc/homelab-telegram-bot/token.cred'
    sudo chown root:root /etc/homelab-telegram-bot/token.cred
    sudo chmod 0600 /etc/homelab-telegram-bot/token.cred
    sudo install -m 0640 -o root -g homelab-bot /dev/null /etc/homelab-telegram-bot/allowlist
    sudo install -m 0640 -o root -g homelab-bot /dev/null /etc/homelab-telegram-bot/privileged-allowlist
    sudo install -m 0640 -o root -g homelab-bot /dev/null /etc/homelab-telegram-bot/restart-allowlist
    sudoedit /etc/homelab-telegram-bot/allowlist /etc/homelab-telegram-bot/privileged-allowlist /etc/homelab-telegram-bot/restart-allowlist
    sudo chown root:homelab-bot /etc/homelab-telegram-bot/allowlist /etc/homelab-telegram-bot/privileged-allowlist /etc/homelab-telegram-bot/restart-allowlist
    sudo chmod 0640 /etc/homelab-telegram-bot/allowlist /etc/homelab-telegram-bot/privileged-allowlist /etc/homelab-telegram-bot/restart-allowlist
    ```

    Enter numeric Telegram user/chat IDs obtained by the owner through Telegram locally,
    one per line in `allowlist`, with at least one recipient. `privileged-allowlist` may be
    empty; any IDs entered must also be in `allowlist`. `restart-allowlist` may be empty;
    `chrony.service` is the only restart permitted by the recorded polkit rule. The three
    files are private deployment settings, not sources recovered from the node. All secret
    values, addresses, tailnet names, emails, Wi-Fi names, chat IDs and UUIDs stay outside
    the repository. Only the owner-approved login name appears in SSH hardening.
    The token is TPM2-sealed without PCR binding; no plaintext token file is created.

    For Telegram `/backup`, provision `/etc/homelab-telegram-bot/mac-listener-url` with the
    owner-confirmed `http://<MAC-TAILSCALE-IP>:8091` base URL (no action path). The address is
    private deployment data; never put it in Git, a PR, or logs. With the existing `homelab-bot`
    account/group provisioned, create the file, enter the URL using `sudoedit`, and set its mode:

    ```bash
    sudo touch /etc/homelab-telegram-bot/mac-listener-url
    sudo chown root:homelab-bot /etc/homelab-telegram-bot/mac-listener-url
    sudo chmod 0640 /etc/homelab-telegram-bot/mac-listener-url
    sudoedit /etc/homelab-telegram-bot/mac-listener-url
    ```

    The bot reads the file on each `/backup`; an absent file means not configured. Include the
    owner's ID in both bot allowlists to permit this privileged command. It makes one direct
    HTTP call with a 210-second timeout, without proxy, redirect or retry. No sudo/polkit grant
    or copy-file access is added. Install the Mac agent from the Mac section, after allowing
    only the node to reach TCP 8091 in the tailnet policy. Restart only the bot after updating
    its installed Python files; `homelab.service` needs no restart for this channel.

11. **Copy the corpus from the owner's Mac.** Create the destination on the unlocked volume:

    ```bash
    sudo mkdir -p /srv/homelab/homelab-v2-data/originals
    sudo chown 0:0 /srv/homelab/homelab-v2-data /srv/homelab/homelab-v2-data/originals
    sudo chmod 0755 /srv/homelab/homelab-v2-data /srv/homelab/homelab-v2-data/originals
    sudo install -d -m 0755 -o root -g root /srv/homelab/homelab-v2-data/originals/corpus
    sudo install -d -m 0755 -o aleix -g aleix /srv/homelab/homelab-v2-data/originals/corpus/about-aleix
    ```

    **On the Mac**, copy `corpus.json` and `eval.json` directly onto the encrypted volume:

    ```bash
    scp '<MAC-CORPUS-DIRECTORY>/corpus.json' '<MAC-CORPUS-DIRECTORY>/eval.json' homelab:/srv/homelab/homelab-v2-data/originals/corpus/about-aleix/
    ```

    Back **on the node**, make the installed corpus root-owned and readable by the app.
    Compose mounts it read-only. Set `HOMELAB_CORPUS_PATH` in the private `.env` to
    `/srv/homelab/homelab-v2-data/originals/corpus`. On an existing node, copy with
    `sudo cp -a /srv/homelab/corpus /srv/homelab/homelab-v2-data/originals/`,
    verify contents and modes, then set the path. Check no med-ask ingest is running
    before restarting the stack. Verify the mount and eval before removing the old copy.

    ```bash
    sudo chown root:root /srv/homelab/homelab-v2-data/originals/corpus/about-aleix /srv/homelab/homelab-v2-data/originals/corpus/about-aleix/corpus.json /srv/homelab/homelab-v2-data/originals/corpus/about-aleix/eval.json
    sudo chmod 0644 /srv/homelab/homelab-v2-data/originals/corpus/about-aleix/corpus.json /srv/homelab/homelab-v2-data/originals/corpus/about-aleix/eval.json
    ```

12. **Start the stack and bot.** Enable the stack under the data target now that its inputs
    exist. First startup downloads Nemotron into `models` and the official Qwen3-Embedding-4B
    Q8_0 into `models/embed-large` (owned by `10001:10001` for its non-root server), and builds
    the app image. Both embedding servers are always on; wait for both model downloads and
    Postgres initialization. On an existing node, prepare that same subdirectory before deploy;
    if `HOMELAB_MODELS_PATH` is overridden, create it beneath that path. The worker
    creates Procrastinate's schema through the library's schema manager. The bot registers
    its command menu with Telegram; start its private chat from the owner's Telegram client.

    ```bash
    sudo docker compose -f /srv/homelab/homelab-v2/compose.yaml config --quiet
    sudo systemctl enable --now homelab.service
    sudo systemctl start homelab-telegram-bot.service
    sudo systemctl start homelab-copy-reminder.timer
    sudo docker compose -f /srv/homelab/homelab-v2/compose.yaml ps
    sudo systemctl is-active homelab.service homelab-telegram-bot.service
    ```

13. **Re-ingest both collections.** Postgres starts empty and is derived from these sources;
    nothing is restored from a database backup. Wait until the local embedding server is
    ready before running ingestion. The about-Aleix corpus takes under a minute; a full Brain
    ingest took 92 minutes on 2026-09-23. Allow the Brain command to finish in the SSH session.
    The per-command environment selects Brain without changing the default serving collection.

    ```bash
    sudo docker compose -f /srv/homelab/homelab-v2/compose.yaml exec -T api homelab ingest
    sudo docker compose -f /srv/homelab/homelab-v2/compose.yaml exec -T -e HOMELAB_ACTIVE_COLLECTION=brain api homelab ingest
    ```

14. **Prove the rebuilt node is back.** Health must return `"status":"ok"`. In Telegram,
    send `/ask <QUESTION-COVERED-BY-THE-CORPUS>` and verify a grounded answer with numbered
    sources. Trigger one test alert through the installed notifier and confirm its receipt
    in the owner's Telegram chat; the label is deliberate and does not fail the stack.

    ```bash
    curl -fsS http://127.0.0.1:8000/health
    sudo systemctl start homelab-notify@homelab.service
    sudo systemctl is-failed homelab-notify@homelab.service
    ```

    Expect `inactive` and exit status 1 from `is-failed` after a successful oneshot alert.
    Reboot only now, with the current boot saved in the persistent journal:

    ```bash
    sudo reboot
    ```

    Reconnect with `ssh homelab`, wait for the watchdog's 90-second boot delay, and confirm
    its Telegram notice says the data volume is `LOCKED`. Explicitly start the stack while
    locked to prove that its mount condition skips it without failure or an additional alert:

    ```bash
    data-volume.sh status
    sudo systemctl start homelab.service
    systemctl show homelab.service -p ActiveState -p Result -p ConditionResult
    sudo journalctl -b -u homelab.service -u homelab-watchdog.service --no-pager
    ```

    Expect `ActiveState=inactive`, `Result=success`, `ConditionResult=no` and a journal entry
    showing the condition skip. Confirm `/status` still works and `/ask` reports the knowledge
    service down in Telegram. Finish by unlocking and checking health again:

    ```bash
    sudo data-volume.sh unlock
    curl -fsS http://127.0.0.1:8000/health
    ```

## Weekly originals copy

With the Mac listener installed and its private URL configured on the node, plug the volume
**SD Card** into the Mac and send `/backup` to the Telegram bot as a privileged user. No mounted
card replies “Connect the SD card and send /backup again.” Success replies
“Copy done: N KB, <file name>.” Failure replies “Copy failed: <short reason>.” If the Mac is
asleep, away or off, it replies “The Mac is not reachable; wake it and send /backup again.”
The Mac must be awake and on Tailscale; see [Mac action listener](#mac-action-listener).

The manual command remains `mac/homelab-copy '<VOLUME-NAME>'` from this checkout (or pass
`/Volumes/<NAME>`). Python 3 is required on the Mac. The command
accepts mounted removable or ejectable volumes, including cards in a built-in SD reader.
It refuses the startup volume, every APFS volume in its boot container, fixed internal disks,
and existing `homelab-originals-YYYY-MM-DD.tar.age`
files. Use a different card or explicitly move an existing file before another copy that day.
It prints every `*-data/originals` folder and its size, streams one tar archive through
age on the node directly onto the card, flushes the card, compares encrypted byte counts,
then records the successful copy on the node. Folder sizes are allocated bytes (`du -sk`
multiplied by 1024); the archive count is its exact encrypted length. Failed/partial files remain on the card for
inspection; they are never confirmed or overwritten. No archive is staged on either disk.
The archive contains paths relative to `/srv/homelab`, with numeric owners and modes.
Do this while originals are idle: tar is not a transactional snapshot of changing files.
Sources such as med-ask's book PDFs, databases, models and code are excluded.

The committed recipient is public; the node cannot decrypt these copies. The private age
key remains only in the owner's password manager. `sudo homelab-copy status` reports the
last successful timestamp (Unix seconds) and encrypted byte count, or `null` before the first
copy. Root-owned state lives in `/var/lib/homelab-copy/` with mode `0700` (record `0600`).
The daily persistent `homelab-copy-reminder.timer` sends one Telegram message per check
when the copy is strictly more than seven days old or missing, including while the data
volume is locked; the reminder says to plug the SD card into the Mac and send `/backup`.
Fresh copies produce no message. The service uses the existing TPM-sealed
Telegram credential and allowlist through `homelab-notify.sh "message text"`.

## Restoring originals from a copy

The owner performs the restore, first into an empty external volume directory. Obtain the
private age identity from the password manager and enter it at the hidden prompt below.
Do not put it in shell history, an argument, a file or this repository. On the Mac, with
Homebrew `age` installed, use Bash process substitution to keep the identity in memory:

```bash
bash
set -o pipefail
mkdir '/Volumes/<RESTORE-VOLUME>/restored-originals'
read -r -s -p 'age private identity: ' COPY_IDENTITY; printf '\n'
age --decrypt --identity <(printf '%s\n' "$COPY_IDENTITY") \
  '/Volumes/<COPY-VOLUME>/homelab-originals-YYYY-MM-DD.tar.age' | \
  tar -xpf - -C '/Volumes/<RESTORE-VOLUME>/restored-originals'
COPY_RESTORE_STATUS=$?
unset COPY_IDENTITY
printf 'restore exit status: %s\n' "$COPY_RESTORE_STATUS"
```

Check the pipeline exit status and inspect the restored files. Each `<project>-data/originals/`
goes back to `/srv/homelab/<project>-data/originals/` on the unlocked rebuilt node.
`homelab-v2-data/originals/corpus/about-aleix/` contains `corpus.json` and `eval.json`;
use it instead of the Mac corpus copy in rebuild step 11. Copy directories without changing
their inner layout; as root on Linux restore numeric ownership and modes from the archive
(`tar --numeric-owner -xpf -`) or apply each project's known ownership after a Mac restore.
med-ask's originals directory is `root:10001` `2770`; preserve its file modes and do not
replace an active application's originals. The owner should compare the first restore with
the source before relying on the weekly copies. Rebuild code and derived stores separately.

