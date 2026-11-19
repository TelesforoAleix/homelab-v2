# Model gateway

This document covers the LiteLLM model gateway in detail: the route table and its purposes, the
policies for client and task networks, image and video media, cost accounting and the audit line.
Read it when you add or change a purpose, a hook in `config/litellm_hooks.py`, a gateway client or
a media model. The contract other repositories rely on is summarised in
[contracts](contracts.md#model-gateway); the boundary is in [ARCHITECTURE.md](../ARCHITECTURE.md).

## Routes and purposes

The two embedding models are the only resident local models.

`config/litellm.yaml` is the single route table. `embed-large` is an additional embedding
purpose, served by always-on `llama-embed-large` on the node's CPU with the official
Qwen3-Embedding-4B Q8_0 GGUF, last-token pooling, L2 normalisation and four threads.
It runs as `10001:10001`, drops all capabilities and uses `no-new-privileges`; its writable
cache is `models/embed-large` on the encrypted volume. It joins only the default network
and publishes no port. LiteLLM depends on both embedding containers being started.


Embedding requests optionally accept `input_type` (`query` or `passage`). Prefixes live in
`model_info.input_prefixes` per purpose: Nemotron uses `query: ` / `passage: `; Qwen uses a
generic retrieval instruction before questions and leaves passages plain. The pre-call hook
transforms string/list-of-string input and removes the field. Invalid roles and token-array
input with roles receive 400. Without the field the input is unchanged. The repository's
own callers, index and answer endpoint retain plain embeddings.

The native LiteLLM wildcard `eval/*` maps `eval/<provider>/<model>` to
`openai/<provider>/<model>` at the same Vercel endpoint and with the same key as named chat
routes. No catalogue allowlist is kept. Unknown IDs fail upstream. On every client network,
the gateway refuses an `eval/…` request with HTTP 403 unless a live task token lists that
exact purpose. Accepted requests use the existing task endpoint restriction, request counter
and cost recording. Refusals log a reason without tokens or content.

The route table also offers `chat`, `chat:high` and `chat:xhigh`
through Vercel AI Gateway's `deepseek/deepseek-v4.1-flash`, at low, medium and high reasoning
effort respectively, and `embed` through the unchanged local `llama-nemotron-embed-1b-v2`.
`grade` makes yes/no relevance judgements per candidate passage, with exactly `chat`'s model and low-effort settings.
`translate` translates passages on request, with exactly `chat`'s model and low-effort settings.
`vision` reaches the owner’s login-managed Metal llama.cpp router over Tailscale, serving
Qwen3-VL-8B-Instruct Q4_K_M with mmproj-F16 and context 16,384. `vision:xhigh` reaches the
same gateway model as chat, without a configured reasoning effort. There is no `vision:high`.
The Mac binds only its runtime Tailscale IPv4 on TCP 8090; the tailnet policy allows only the
node. Its private URL comes from `HOMELAB_MAC_VISION_URL` in the node’s root-owned `.env`,
read by the route table with `os.environ/`. An unset URL leaves the proxy running and makes
local vision fail; Compose uses a closed loopback endpoint. The deployment hook supplies an
HTTPX timeout with a 3-second connect limit and 300-second page request limit. Stopped serving
is normal: errors are generic OpenAI-shaped 4xx/5xx, without retries, fallback or alerts.
Clients keep pages queued and supply prompts, DPI, temperature and completion limits.
The user LaunchAgent keeps the router available at login, with models loaded only on request.
State stays private in the owner’s home. Image inputs must be `data:` URLs.

Three hosted image purposes use the gateway through OpenAI-path entries:
`image-nano-banana-2.1`, `image-gpt-2.5-flare`,
and `image-gpt-2.5-sunburst`. These and the native media routes use
`/run/secrets/image_gateway_api_key`, read into `HOMELAB_IMAGE_GATEWAY_API_KEY` by the
entrypoint. It has the existing key's `root:10001` `0440` protection and a separate $20
monthly budget set in Vercel. The cap is soft: the crossing request completes, then image
calls return 402 `quota_for_entity_exceeded`, with a fixed budget message. It resets on the
1st at 00:00 UTC. Chat and embeddings keep their existing entries and keys.
Image requests have 300-second timeouts without retries or fallbacks. LiteLLM only routes:
clients supply prompts, sizes and references; the node does no image processing.
Nano Banana 2.1 uses chat completions with `modalities: [text, image]`; the other two
use multipart image edits (or JSON generations without references). `/v1/model/info` returns
only `model_name` and configured `model_info` for every entry, never deployment parameters.
Image profiles publish reference limits, settings, terms, prices and measured response/timing.
A combined size field uses only a `sizes` list of `{value, aspect_ratio, resolution, price}`.
Separate settings use `aspect_ratios` and `resolutions` or `qualities`, with distinct request
fields; a profile never carries both forms.
Published nested settings are allowed only at their specified paths, preventing routing
overrides inside provider options. Provider image errors are sanitized at the HTTP boundary.

Image responses alone may add `x-homelab-cost-usd`, a decimal USD string. Cost precedence is
Vercel's billed-cost header when supplied, then its body `providerMetadata.gateway.gatewayCost`
(or `cost`), then a positive LiteLLM `x-litellm-response-cost`; otherwise the header is absent.
The live GPT Image Flare proof returned no cost header and billed USD 0.00597 in both metadata
fields. An HTTP response observer captures billing before LiteLLM's parser, so a charged
parse failure retains the cost after error sanitization. Provider metadata is removed from
all image response bodies. Image streams are buffered to finish billing and remove metadata
before sending headers; their SSE format remains unchanged. Chat and embeddings are unchanged.

The hook atomically adds each reported cost and one charged call to Postgres
`homelab_image_costs`, keyed by SHA-256 of the bearer key and UTC calendar month. No prompts,
images, raw keys or per-call ledger are stored. The API applies the idempotent one-table SQL
migration at startup. `GET /v1/images/cost` returns only the supplied bearer key's current month
as `{month: "YYYY-MM", total_usd: "decimal", charged_calls: integer}`; missing rows return zero.
LiteLLM forwards this read to the private API, keeping other clients off the API network.
Image calls and the read require a nonempty bearer key. Keys remain arbitrary trusted-client
identities, without central registration: possession of a key grants access to its total.

An empty-key sentinel row per month holds the two notification flags in that same table.
Every three minutes, a host service reads total spend across client rows, compares it with
`HOMELAB_IMAGE_MONTHLY_BUDGET_USD` (Compose default 20), and uses the existing TPM-sealed
credential and `homelab-notify.sh` to send 80% and 100% notices. Row locks serialize senders;
each successful notice commits its flag before proceeding. Failed sends remain pending.
The host invokes the API's accounting command through Docker exec; no new secret, listener,
channel or container privilege is added. These totals cover reported calls since deployment,
not earlier gateway usage or calls made outside the proxy. They observe spend; rule 6's
Vercel budget remains the enforcement boundary.

Names are `purpose:tier`; image purposes and embeddings have no tiers. Clients
choose tiers themselves. Two bounded native LiteLLM patterns also admit `vision-<id>` and
`embed-large-bulk-<id>` to the Mac router. The hook validates id syntax and native preset/alias
availability before inference; arbitrary dynamic ids are never logged. No global wildcard,
provider pass-through or fallback is offered.
Unknown names, deployment model ids and unavailable tiers receive LiteLLM's default
OpenAI-shaped 4xx and never reach a provider. The pre-call hook checks against the same config
and enforces each alias's configured effort; middleware blocks provider/routing overrides.

Every application, including this repository's API, worker, CLI and eval, knows only
`HOMELAB_MODELS_BASE_URL` (default `http://litellm:4000/v1`), a client key from the environment,
and purposes discovered from `GET /v1/models`. LlamaIndex's OpenAI-compatible clients send
those purposes as `model`. Chat responses report the alias. A post-call hook sets embedding
responses' `model` to the configured real model; clients record it at ingest. The pinned
release's raw-model metadata switch prevents LiteLLM overwriting that value with either
embedding alias.
Moving embeddings through this proxy does not change the embedding model or indexed data.

Other Compose projects attach trusted clients to `homelab-models`. Only LiteLLM joins that
named network, alongside the private default network it uses to reach both embedding servers.
Postgres, API and both embedding servers do not join the client network. LiteLLM publishes
only `127.0.0.1:4000`, for the host Telegram bot.
The key sent by a client is never forwarded upstream; only media accounting uses its hash.

A task container gets none of that trust. LiteLLM also joins `homelab-tasks`, an `internal`
network on the fixed subnet in `config/litellm.yaml`, and nothing else in Compose does. The
middleware's first act is to compare the socket's peer address with that subnet; Uvicorn
believes forwarded headers only from loopback, so a task container cannot claim another
address. A request from the subnet needs a live task token and may then use only
`POST /v1/chat/completions`, as `application/json`, with a purpose the token names. Anything
else gets 401, 403, 415 or 400 and a `task_refused reason=...` audit line, before any other
rule, including liveness. A request that passes counts against the token's `max_requests`, 100 by
default; past it the answer is 429, reason `budget`. That budget is both the spend cap and the step
limit of a run. It is counted per token hash in memory, so restarting LiteLLM forgives it. Requests
from other networks never enter this path. The store is a directory on the encrypted volume,
mounted read-only into LiteLLM: one file per token, named by the token's SHA-256, holding only its
purposes, expiry and budget. Deleting the file revokes it. The next request sees that, because
each one reads the store. The proxy also records what each request cost: for a request that
passed, it keeps the last 16 KiB of the reply as it streams by, without changing or delaying it,
and when the reply ends reads `usage.cost`, the amount the gateway billed. It appends
`{"cost_usd": <number>}` (`null` when the cost is missing, not a non-negative number, or the status
is 400 or more) to `task_cost_dir`, one file per token hash, mounted read-write in LiteLLM only and
holding nothing else. A write that fails logs one line and never fails the request; refused
requests and other networks write nothing. The token itself exists only in
the runner's memory and the agent container's environment. The `task-worker` purpose has
`deepseek/deepseek-v4-flash` through Vercel AI Gateway with low reasoning effort; it is an
ordinary purpose to any other client.
There is no master key, LiteLLM-managed database, UI or virtual key configuration.
HTTP middleware exposes `GET /v1/models`, `GET /v1/model/info`, `GET /v1/images/cost`,
`POST /v1/chat/completions`, `POST /v1/embeddings`, `POST /v1/images/edits`,
`POST /v1/images/generations`, Home Lab's `POST /v1/videos`, `GET /v1/videos/{id}`,
`GET /v1/videos/{id}/content` and liveness; management endpoints and LiteLLM-specific request fields are refused. Provider
headers are removed and upstream errors are reduced to status and a generic message. `/v1`
only grows through new purposes and optional fields; incompatible changes go under `/v2`.

The official BerriAI image is pinned by digest, runs as `10001:10001`, drops all capabilities
and uses `no-new-privileges`. Only LiteLLM mounts `/run/secrets/gateway_api_key`, still
`root:10001` `0440`. Its entrypoint reads the file into the process environment at startup;
no gateway key appears in Compose, env files or config. Telemetry, third-party callbacks,
admin UI and remote model-price fetching are disabled; tokenizer assets are bundled and
remote image URLs are refused. External model destinations are the gateway and the login-managed
Mac router; the local embedding destinations stay on the private network. Tests run the real pinned
image without external networking, synthetic credentials and loopback upstreams, checking
for attempted external connections as well as the endpoint contract.

The journald audit line contains purpose, real model (the served model from the Mac's response
for `vision`), HTTP status, total elapsed seconds (through the end of a stream) and upstream-attempt
status. Unknown aliases are logged as `unknown` so arbitrary client strings cannot become content in the journal. Default vendor
and access logging is disabled because errors can contain bodies, prompts or credentials.
No hosted observability callbacks are configured.

## Using the gateway

Models are served by LiteLLM using `config/litellm.yaml`, the single route table. The API,
worker, ingestion CLI and eval discover purposes from its `/v1/models` endpoint and use those
names through the existing LlamaIndex clients. They hold only `HOMELAB_MODELS_BASE_URL`
(default `http://litellm:4000/v1`) and `HOMELAB_MODELS_API_KEY` (default `homelab`, accepted
and ignored today). The gateway secret is mounted only into LiteLLM, which reads it at startup;
its `root:10001` ownership and `0440` mode are set at provisioning.

The API's `/health` returns `status`, `version`, `routes` and `models_base_url`; the API holds
no gateway key. LiteLLM publishes only
`127.0.0.1:4000` for host clients. Its official image is pinned by digest in Compose and updated
deliberately. Telemetry, remote model-price updates, hosted callbacks and the admin UI are off;
tokenizers are bundled. Its external model calls go only to Vercel AI Gateway; embeddings go
to `llama-embed` or `llama-embed-large` on the private network. The journal records one line
per endpoint call with purpose, real model, HTTP status, elapsed seconds and whether an upstream call was attempted.
Content, embeddings and keys are excluded, including on errors and streaming calls.

### Use the model endpoint

Attach a client container from another Compose project to the external Docker network
`homelab-models`, created by this stack. Only LiteLLM joins it; Postgres, API and both
embedding servers remain on the stack's private network. Give the client its base URL `http://litellm:4000/v1`
and an arbitrary API key through its environment (host clients use
`http://127.0.0.1:4000/v1`), then use OpenAI-shaped `GET /v1/models`,
`GET /v1/model/info`, `POST /v1/chat/completions`, `POST /v1/embeddings`,
`POST /v1/images/edits` and `POST /v1/images/generations`. The text and embedding names are `frontline`, `chat`
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
a 4xx without an upstream call. The accepted client key does not restrict model access:
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

### Image models that are not offered

The published `seconds_per_image` values come from single direct gateway calls on the node; they
estimate latency, not guarantee it. Nano Banana 2 has no entry: the live gateway classifies
`google/gemini-3.1-flash-image` as a language model and refuses it on `/images/edits`. FLUX 3 and
FLUX.2 pro have no entry because the pinned LiteLLM parser rejects their responses'
`usage.input_tokens_details`; no adapter, response rewriting or substitution is used. The proof
runs and their numbers are in git history.

A model with a combined size field publishes only `sizes`, a list of
`{value, aspect_ratio, resolution, price}`. Models with separate fields publish
`aspect_ratios` and either `resolutions` or `qualities`, each naming a distinct request
field. A profile never carries both forms. The GPT entries already use `size` and
`quality`; Nano Banana 2.1 uses separate nested aspect-ratio and image-size fields.

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

Image responses add only `x-homelab-cost-usd`, a decimal USD amount, when billing is known.
Read it even on errors or an HTTP 200 with no usable image; a charged failure still counts.
Vercel's billed header takes priority when present, followed by
`providerMetadata.gateway.gatewayCost` (or `cost`) from its body, followed by LiteLLM's positive
computed cost. Provider metadata and all other provider headers are removed. Missing cost is
unknown, not zero. The direct low-quality 1024×1024 Flare inspection returned HTTP 200, one
image, no billed-cost header and USD 0.00597 in both Vercel metadata fields. The gateway
observer runs before schema parsing, so a parser failure does not erase a charge. Image SSE
responses are buffered until completion to set the header and strip metadata.

With the same client bearer key, call `GET /v1/images/cost` at the model endpoint (also offered
by the loopback API on port 8000). It accepts no query parameters and returns:

```json
{"month":"2026-10","total_usd":"0.00597","charged_calls":1}
```

`month` is the current UTC calendar month. A new key or month returns `"0"` and zero calls.
No key selector is accepted; only possession of the bearer key selects a total. These remain
trusted-client keys without registration or central authentication. Image calls now require
a nonempty bearer key so every reported charge has an owner. Costs use exact Postgres numeric
addition and key hashes; the API applies `image_costs.sql` at startup. Unknown-cost calls,
earlier usage and calls outside the proxy are absent from these totals. Compare matching
periods with Vercel; the local counter does not replace its dashboard or enforce its budget.
If recording a confirmed charge fails, the response is a fixed 503 with the charge header;
do not automatically repeat that paid call. The journal records only the numeric cost and a
fixed accounting-failure marker.

The hook checks that the accounting table is reachable before sending an image upstream;
startup migrations or a database outage therefore refuse new paid work with 503.

`homelab-image-cost-notice.timer` runs every three minutes while the volume is unlocked.
Its host service reads the same table through `docker compose exec -T api`, sends plain text
through the existing encrypted credential and notifier, and records successful 80% and 100%
deliveries in monthly sentinel rows. `HOMELAB_IMAGE_MONTHLY_BUDGET_USD` in Compose defaults to
20 and controls only these alerts; changing it does not change Vercel's budget. Failed sends
remain pending for the next tick. Concurrent senders are serialized. A crash after Telegram
accepts a message but before its database acknowledgement can repeat it; Telegram supplies
no idempotency key for that boundary. No production thresholds are fabricated for tests.
Check with `systemctl status homelab-image-cost-notice.timer` and
`journalctl -u homelab-image-cost-notice.service`. The rebuild steps install and enable it.

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

## Media client contract

The image studio uses the existing loopback model gateway with its own bearer client key.
The [README contract](platform.md#image-studio-operations-and-video-jobs) records the exact
fields and model/price table. Discovery adds an `operations` mapping to `/v1/model/info`:
`generate_image`, `edit_image`, `upscale_image`, `image_to_video`, `upscale_video`, `edit_video`.
Each listed model's `model_info` supplies `operations`, `default_for`, USD `price` and
`capabilities`: durations, resolutions, aspect ratios, audio, end frame, reference images,
reference videos and mask. Operation-specific restrictions override general capabilities;
Grok editing has no duration/resolution/aspect-ratio or audio controls. Kling resolution
selects std (720p) or pro (1080p), since its SDK ignores the generic resolution field.
Kling inherits aspect ratio from the start image and refuses the aspectRatio control. Unknown or unoffered operations
have empty lists. One to four verified models may serve each operation, with one default.
Existing image routes and estimates remain compatible. Topaz Wonder adds a native image
upscale purpose on the same multipart `/v1/images/edits` path, returning base64 in `data`.

Home Lab middleware owns `POST /v1/videos`, `GET /v1/videos/{id}` and
`GET /v1/videos/{id}/content`; LiteLLM's own video routes are bypassed. The start body is
JSON `{model, operation, prompt?, image?, endImage?, referenceImages?, video?, duration?,
aspectRatio?, resolution?, generateAudio?, source?}`. Inputs are inline base64 data URIs;
URLs, provider/routing options and query parameters are rejected. Generation requires a
start image or one to the profile's reference-image limit without a start or end image;
`video-grok` supports up to five references. Upscale/edit require MP4 video. No listed model accepts an end image.
Start-plus-reference combinations are refused because the selected models do
not document preserving both controls. Source metadata for upscale is
`{width,height,duration,frameRate,frameCount?}`. Bodies are limited to 48 MiB before upstream
access; invalid fields and unsupported capabilities return 400, oversized bodies 413.

An optional client `Idempotency-Key` (1–128 letters, digits or `_.:-`) derives a stable
UUIDv5 from the key hash and request token. The jobs primary key deduplicates starts;
repeating the token returns the original job, independent of body changes. Without the header,
each POST is new. Raw tokens are not stored. A start stores metadata before returning 202 `{id,operation,model,status:"queued",cost_usd:null}`.
Status reads use the same shape with `queued|running|completed|failed` and a decimal-string
cost once known and `error` null unless failed; failures carry a stored type and sanitized short message. SHA-256 of the bearer key selects owned jobs; other keys receive 404 for
both status and content. Content streams bytes, returns 409 until completion and 410 after
expiry. The video response-header allowlist is exactly `content-type`, `content-length`,
`x-homelab-cost-usd`. Provider references, response bodies and URLs never reach the client; a failed job may expose its sanitized provider message to its owner.

Only the gateway holds the existing image-key secret. Config holds the media purposes and
native model identifiers separately from LiteLLM's model list. The native transport follows
Vercel's AI SDK v4 wire contract: POST `/v4/ai/video-model/start` with `ai-model-id`,
`ai-video-model-specification-version: 4`, a server-generated UUID `idempotency-key`, and
protocol header `ai-gateway-protocol-version: 0.0.1`, auth method `api-key`, and
JSON `{prompt,n,image?,frameImages?,inputReferences?,duration?,resolution?,aspectRatio?,
generateAudio?,providerOptions?}`. File values use `{type:"file",mediaType,data:<base64>}`.
The start response's opaque `operation` is persisted; POST `/v4/ai/video-model/status` sends
`{operation}` and receives `pending|completed|error`, with `videos` and provider metadata.
For a wire body above 292 KiB (300 KiB minus Vercel's 8 KiB attribution allowance), the same
inline body goes to `/v4/ai/video-model`, accepting SSE `result` or `error`. No input is hosted
or uploaded by Home Lab. Grok edit uses a data URI in server-owned `spacexai.videoUrl`;
Topaz upscale uses inline `inputReferences`. Native image upscale POSTs `/v4/ai/image-model`
with the v4 image header and `{prompt:"",n:1,files:[file],size?}`.

A bounded asyncio background task is the simple non-retryable executor for these gateway
HTTP calls, with four active jobs and a one-hour deadline. It is not another persistent queue;
Procrastinate continues to own the repository's durable job machinery. No prompt or media
is placed in its task arguments or database. The API's startup accounting migration also
creates `homelab_video_jobs`: UUID, key hash, operation, model purpose, opaque Vercel reference,
status, numeric cost, created/updated times. No second media store or container is added.
First video access after restart resumes jobs with a saved async reference; jobs without one
become failed rather than repeating an ambiguous paid request. A crash before reference
persistence, or during the single-request flow, can leave an unknown charge requiring Vercel
reconciliation. It must not trigger an automatic client retry.

Completed media is kept only in memory for up to one hour, at most 16 results, then discarded;
no output file is written. Async content can be fetched again through its saved operation
while Vercel retains it. Hosted outputs stream without a provider authorization header,
redirects, cookies or provider headers; inline results stream in chunks. Results can expire
upstream sooner. A single-request result is unavailable after a restart.

Billed `providerMetadata.gateway.gatewayCost` (falling back to `cost`) is the only video debit.
One row lock commits the job's first known cost and the monthly `homelab_image_costs` increment
in the same Postgres transaction. Status and content reads cannot double-charge; charged
failures count too. Video estimates are never money. Unknown remains null. The old
`/v1/images/cost` name now totals images and video, with its existing response shape and bearer
ownership. The $20 Vercel budget and host 80%/100% notifications are unchanged. Existing image
cost-source precedence remains unchanged. A transient final-accounting failure retains the
confirmed charge in memory and retries its transaction on the next video request; logs contain
only job ID, configured purpose, state, timing, numeric cost, error type and error code or class, never input or error messages.

The loopback management reads project the image's `config/litellm.yaml`, including native
media purposes, without calling models or using the gateway key. They report the first route
in table order as live; this is the configured selection, not observation of gateway traffic.
All current routes are enabled. Local provider `/models` checks are cached for 30 seconds;
hosted providers report `configured` without a probe. Responses contain no addresses or keys.

### Operating video jobs

The [media client contract](platform.md#image-studio-operations-and-video-jobs) lists paths,
fields, models and prices. Video uses the existing image key; the Vercel $20 budget, monthly
reset, and host 80%/100% alerts apply to combined image/video spending. The historical
`GET /v1/images/cost` path and `homelab-image-cost-notice` unit names are unchanged.
No provider key is added to the API or worker. The gateway mounts the transport module from
`src/homelab/api/video_jobs.py` read-only, alongside the config; the API startup applies
`video_jobs.sql` beside the existing image accounting migration.
Kling 2.5 Turbo refuses an end frame in standard mode; no listed model takes an end frame.
Grok generation accepts one to five reference images without a start or end image, as an
alternative to a start image. The references are sent as `inputReferences`.

Job status and content require the creating client's bearer key. Fetch content promptly through
the loopback gateway; never expose its URL or use a public upload to bypass inline size limits.
Inputs are limited to a 48 MiB JSON body. Four background calls may be active, each up to one
hour. Downloaded outputs are streamed, and completed results are kept only in memory, at most
16 for up to one hour. A provider may expire its output sooner. Single-request results vanish
on restart; saved async operations can be polled again while Vercel retains them.

A gateway restart marks interrupted jobs without an upstream reference failed on the next
video request. Do not retry them automatically: Vercel may already have billed them. A missing
cost is unknown, not zero. Reconcile such calls with Vercel's dashboard. If final accounting
fails temporarily, the next video request retries the pending transaction; restarting first
can lose that in-memory confirmation. Known costs and terminal states commit atomically.
No status read or content download charges a job again. Audit logs contain only UUID, configured
purpose, state, duration, numeric cost and failure type and code or class; error messages are not logged. Failed status responses contain a stored short sanitized reason. Default HTTP/library diagnostics remain disabled.

Read-only accounting checks from the node (no client keys or content):

```bash
sudo docker exec -i homelab-api-1 python - <<'PY'
from homelab.api.image_costs import connect
with connect() as c:
    print(c.execute("SELECT status, count(*), sum(cost_usd) FROM homelab_video_jobs GROUP BY status").fetchall())
PY
```

For a fake-charge concurrency proof against an isolated schema, run
`python tests/video_store_proof.py --source-root <checkout>` in an environment with the API's
Postgres connection. It creates a uniquely named test schema, proves exactly-once accounting
and ownership, and removes only that schema; it does not call a provider or touch application rows.
