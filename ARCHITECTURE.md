# Architecture

## Boundary

```
                 Loopback API (127.0.0.1:8000)
                     │ knowledge and jobs
                api / worker / CLI
                     │ OpenAI-compatible HTTP, purpose names
Other Compose clients ── homelab-models ── LiteLLM (no published port)
                                            │ default network
                              ┌─────────────┴──────────────┐
                       Vercel AI Gateway       llama-embed / llama-embed-large
                      (hosted chat/images)        (local embeddings)

api / worker ── Postgres + pgvector (private default network)
```

Home Lab owns provider keys, the route table, knowledge provenance, job durability and the API
contract. LiteLLM owns purpose routing and the OpenAI-compatible model endpoint; only its
container holds the gateway key. LlamaIndex owns parsing, chunking, embedding, storage and
retrieval; Procrastinate owns the job machinery.

## Deployment

One `compose.yaml`: `api`, `worker`, `postgres` (pgvector image), `llama-embed` (llama.cpp
server), `llama-embed-large` (CPU Qwen3-Embedding-4B) and `litellm`. Only `api` publishes a
port, on loopback.
`node/etc/systemd/system/homelab.service` wraps the stack so it starts after the encrypted
volume is unlocked (`homelab-data.target`) and
stops with it. Brain and the corpus are bind-mounted read-only; Postgres data and model files
live on the volume.

Volume users declare `ConditionPathIsMountPoint=/srv/homelab`, `After=homelab-data.target`,
`PartOf=homelab-data.target` and `WantedBy=homelab-data.target`. They do not require that target
or place `WorkingDirectory=`, `RootDirectory=` or `StateDirectory=` on the volume: those
directories create implicit mount dependencies before conditions are checked, turning a locked
volume into a failure alert. The stack instead passes Compose the absolute `-f` path; its
project name and relative paths come from `compose.yaml`.

`node/` mirrors host paths and records the volume unlock script and target, failure notifier,
boot watchdog, stack unit and agent sudo grant. The notifier accepts only `bot`, `watchdog` and
`homelab`, mapping them to the bot, watchdog and v2 stack units. Retired v1 programs, units,
configuration, state, accounts and checkouts have been removed from the node; Brain and v2's
volume data remain. Host files are installed as `root:root` from the
node's clone of merged `main`, after backups and visible diffs, with mode `0644` for units and
drop-ins, `0755` for scripts and `0440` for sudoers. Unit changes require `daemon-reload` and
validation on the node. Bot Python files and the polkit rule also use `0644`. The agent has root
through sudo; the owner approves execution plans
and risks. The service restart pulls `main` and rebuilds containers, while host-file installation
is explicit. Bot code, its unit, credential and failure drop-ins, and the narrow polkit grant
are recorded here. Private allowlists and secret credential contents stay outside this tree.

Base SSH, ufw, Docker, sysctl, console timeout and Wi-Fi configuration also live in `node/`;
their installed modes are recorded in `AGENTS.md`. Recovery is the
[Rebuilding the node](docs/operations.md#rebuilding-the-node) procedure: no node backup, a public code
clone, Brain from the owner's Mac, originals restored from an encrypted copy (or the
owner's Mac corpus), re-ingestion and re-issued secrets.
The rebuilt node needs no GitHub credential. Only the Samsung system disk is rebuilt; the
deliberately unused Micron disk stays untouched.

Every project's irreplaceable inputs live in `/srv/homelab/<project>-data/originals/`.
The corpus lives at `/srv/homelab/homelab-v2-data/originals/corpus/`, mounted read-only.
`homelab-copy stream` pipes all originals directories through tar and age to SSH stdout;
only a public recipient is installed. The Mac writes directly to a mounted removable or
ejectable volume, excluding the startup volume and every APFS volume in its boot container,
checks the reported encrypted byte count, then calls `confirm` to atomically record the
last-good time and bytes outside the encrypted volume. A daily persistent timer uses that
record and the existing notifier to remind the owner after seven days, or before any copy.
No archive is staged, and the private identity remains in the owner's password manager.

The Telegram client remains a stdlib-only host service, running as `homelab-bot` with its
existing sandbox and TPM-sealed token. It long-polls Telegram with no listening port. The main
allowlist gates dispatch; the privileged allowlist is a subset and gates `/backup` and
`/restart`, which also checks its unit allowlist. Polkit independently permits only restarting `chrony.service`.
`/ask` posts to the loopback answer API, returns only text and never dispatches model output.
Host metrics, restart and help do not depend on the knowledge service or unlocked volume.
Answers include numbered source titles, refusals omit sources, and replies are split within
Telegram's length limit. The router also supplies startup `setMyCommands` and `setMyDescription`;
registration failures are nonfatal.
User IDs remain audit metadata in the node's journal; `/ask` logs its argument length only.
Handler errors log the registered command and exception type, never exception text.

The first node-to-Mac action channel is the owner's launchd user agent on TCP 8091, binding
only the runtime Tailscale IPv4. The tailnet grant permits only the node; there is no key.
The bot reads its base URL from private `root:homelab-bot` `0640`
`/etc/homelab-telegram-bot/mac-listener-url`, and makes one empty `POST /backup` with a
210-second timeout, no proxy, redirect or retry. Missing configuration, no card, busy, failure
and unreachable Mac each produce text replies. Only metadata (requester, result, bytes) is
logged. The bot gains no privilege or access to copies, and its existing sandbox is retained.
The listener's fixed action list has exactly `backup`, with no caller arguments; everything
else receives 404. A nonblocking lock refuses concurrent actions as busy. If SD Card is mounted,
it invokes the existing Mac copy command and waits at most 180 seconds, killing the process
group on timeout. It parses only the final confirmation report for file name and encrypted
bytes; no content passes through the listener. launchd runs it at login and restarts it after
exit; private state/logs remain in the owner's home. This host channel publishes no container
port and does not change the loopback API boundary or the independent model router lifecycle.

The two embedding models are the only resident local models. The `local` queue has concurrency 1.

## Routes

`config/litellm.yaml` is the single route table. `embed-large` is an additional embedding
purpose, served by always-on `llama-embed-large` on the node's CPU with the official
Qwen3-Embedding-4B Q8_0 GGUF, last-token pooling, L2 normalisation and four threads.
It runs as `10001:10001`, drops all capabilities and uses `no-new-privileges`; its writable
cache is `models/embed-large` on the encrypted volume. It joins only the default network
and publishes no port. LiteLLM depends on both embedding containers being started.

`embed-large-bulk` serves the same official GGUF and vector space from the owner’s Mac for
bulk ingest; questions keep using the always-on node’s `embed-large`. Both Mac purposes share
one native llama.cpp router,
managed at login by `org.homelab.router` through `mac/homelab-router`. It binds only the runtime
Tailscale IPv4 on TCP 8090 under the existing node-only grant; 8092 is freed. The private
`HOMELAB_MAC_EMBED_URL` and `HOMELAB_MAC_VISION_URL` point at the same router, with closed
Compose defaults. Native presets retain the exact model/projector and inference flags:
vision uses context 16,384, one slot, cache RAM 0, flash attention and Metal `-ngl 99`;
embeddings use last-token pooling, L2 normalisation, context 8192, batch/microbatch 2048,
four threads and Metal `-ngl 99`. Native aliases `vision` and `Qwen3-Embedding-4B` match the
gateway's unchanged upstream names and select each service's profile default. Embeddings report the same real model and role wording as
`embed-large`, preserving its vector space.

Autoload is enabled. Version-2 private profiles select off, on request or on for each routed
service and its allowed models/default from a private version-1 registry. Presets are rendered
only for allowed models; purpose aliases and `<purpose>-<id>` names share one child per model.
On-request models sleep after 600 seconds. On models disable idle sleep, load natively at
startup, and are explicitly loaded/verified through the native API after a profile switch.
Router capacity covers the registry to prevent LRU eviction of on models.
The profile command registers Hugging Face downloads beside a model path from private
configuration, refuses downloads leaving under 20 GiB, verifies published SHA-256 hashes,
and test-loads on a separate temporary loopback server without changing live router policy.
Measured Darwin process-family physical footprint, rounded up to MiB, fills `memory_mb`;
failed loads register nothing and new models join no profile's allowed list. Optional profile
budgets default to 16384/40960 MiB for Working/Inference. Status reports pinned model memory
plus the largest on-request model, explicit unknowns and warning-only budget excesses. Sleeping
children remain ready to reload. launchd owns process restart; llama.cpp owns model loading,
cache discovery and idle sleep, using a private cache under router state. The usage profile
command controls preset availability through native reload; no custom model runtime or
listener actions are added. The action listener on 8091 and dictation cleanup server on 8095
stay independent. The retired Mac launchers are pointers, with vision's address-discovery export
retained for the unchanged listener. Legacy state is unused; historical embedding files remain.
Generated presets and the LaunchAgent contain runtime paths only in private user state.
Content logging is disabled and native output is discarded. Failed launcher invocations retain
only a fixed startup-stage line in private state, never exception text or CLI output.
`status` exposes only preset states and current router/family RSS and Darwin physical
footprint. The router itself installs no wake lock.

`mac/homelab-profile` observes services, app bundle ids and settings, reporting Working,
Inference or custom as JSON. Private editable profiles and an atomic recovery record live
outside git. Working allows bulk embeddings and dictation and leaves the owner’s system
settings alone; system settings are excluded from its profile matching. Inference requires
AC power, permits router services, stops dictation, politely quits configured apps, selects
High Power and disables sleep only for AC (`pmset -c`), and raises the global GPU wired limit
to 45056 MB. Entry reads per-source settings from `pmset -g custom` and records the AC power
mode, AC sleep and GPU limit; every return restores those exact bounded values from private
recovery state. Battery power and display settings are never written. Only the owner enters
Inference, by hand or through the owner-set night window below; a VS Code descendant-work guard
requires explicit confirmation before side effects.
The root-owned `homelab-profile-apply` accepts only the two profile names through exact sudoers
arguments, validates bounded numeric settings from the invoking user's private files and calls
only fixed pmset/sysctl commands with an isolated interpreter. No user code executes as root.
A user LaunchAgent checks inactivity every 30 seconds and returns to Working after 900 seconds
without loaded router models or jobs, reopening precisely the recorded apps/services.
A short unplug is ignored. Ten continuous minutes observed on battery pauses Inference:
native presets are emptied and models unloaded while apps/dictation remain closed. Status
reports `profile: inference`, `paused: true`, `pause_reason: on_battery`. Power-mode changes
on battery do not turn an existing Inference session into custom state. Paused time suspends
idle return; the Mac waits for AC or an explicit Working switch. Replug automatically restores
High Power before the allow-list, preserving the original recovery record and restarting the
15-minute idle window. A native static JSON file on the existing router port publishes only
a pause boolean, so gateway refusals name the battery pause and audit `profile_paused=true`.
No profile contents, app lists or paths are served. Ordinary Working application writes no
power settings; without a pending recovery record it only resets a nonzero orphan GPU limit
to zero. The helper refuses Inference without a complete bounded recovery record. Legacy
Working settings are validated, then ignored without rewriting the private configuration.
Manual changes are compared to observed profiles, rather than trusting a selected-name flag.

Mac login agents and the menu client's profile command run from the private installed copy
at `~/Library/Application Support/homelab/mac/`, with one recorded Homebrew Python interpreter.
`mac/install-mac-tools` accepts only a clean commit contained in freshly fetched `origin/main`,
stamps that commit, rewrites existing agent paths without re-registering jobs, and restarts only
affected loaded agents. Working-checkout branch switches cannot change live code. All profile,
job and router state stays in its existing private folders. Menu builds use installed sources.

The SwiftUI menu bar app under `mac/MenuBar` is a client of `homelab-profile`: it runs the
command recorded in its bundle and renders its JSON (status, `plan-inference`, the guard's
refusal, `waiting`, `usage`, `schedule`), holding no profile rules. It is ad hoc signed into
`~/Applications` and started at login by the `org.homelab.menu` user LaunchAgent; it uses only
Apple's SDK. `waiting` makes two read-only calls over the node-only `homelab-agent` SSH account:
the last 24 hours of `CONTAINER_NAME=homelab-litellm-1` journal lines with
`profile_blocked=true`, and public
`/v1/model/info` from inside the LiteLLM container. It returns counts per purpose, with an
estimate only where `seconds_per_image` is published, and keeps nothing; there is no queue.
`usage` only reads local files: Codex's session logs and `~/.claude/usage-now.json`, which the
owner's Claude Code status line writes, and reports each source's age from its own timestamp.
The night window is the one unattended entry into Inference, owner-set and off by default, in
private configuration. The existing profile agent evaluates each selected night once at its
start: only from exact Working, on AC, with no VS Code guard work, then through the same guarded
switch, which still refuses job context. It records ownership privately and returns to Working
at the end only if no manual switch, service action or `schedule off` ended that ownership.

The router's private preset allow-list omits disallowed services; native preset reload unloads
removed children without request rewriting or changes to model flags. The gateway checks the
Mac's native model list before inference. An absent preset receives a fixed purpose-specific
HTTP 503 saying "not available in this profile"; the existing content-free audit adds `profile_blocked=true` so
refusals are countable per purpose. Native router failed-load errors become HTTP 503 saying the Mac could not load the model
now (often for lack of memory), and to try later. No memory-specific diagnosis is inferred;
other failures remain sanitised. Clients own waiting work.
Light Mac jobs use individual launchd calendar agents and run only in an exact permitting
profile, on AC power with idle input. Missed runs coalesce into a pending bit, retried through
launchd at the next eligible idle check. Jobs never enter Inference and never run under a
VS Code terminal. These owner-trusted local commands are independent of the node's durable
Procrastinate jobs; no Mac work queue is introduced.
The deployment hook keeps each purpose's 3-second connect and 300-second request limits,
without retries, fallback or stopped-Mac alerts.

Embedding requests optionally accept `input_type` (`query` or `passage`). Prefixes live in
`model_info.input_prefixes` per purpose: Nemotron uses `query: ` / `passage: `; Qwen uses a
generic retrieval instruction before questions and leaves passages plain. The pre-call hook
transforms string/list-of-string input and removes the field. Invalid roles and token-array
input with roles receive 400. Without the field the input is unchanged. The repository's
own callers, index and answer endpoint retain plain embeddings.

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
and `image-gpt-2.5-sunburst`. Only these entries use
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
no host port.
The key sent by a client is accepted and ignored; there is no master key, database, UI or
virtual key configuration. HTTP middleware exposes `GET /v1/models`, `GET /v1/model/info`,
`POST /v1/chat/completions`, `POST /v1/embeddings`, `POST /v1/images/edits`,
`POST /v1/images/generations` and
liveness; management endpoints and LiteLLM-specific request fields are refused. Provider
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
No hosted observability callbacks are configured. `/health` keeps `status`, `version` and
`routes`, and replaces `gateway_key_present` with `models_base_url`.

## Jobs

The Procrastinate app persists jobs in Postgres. Its `ping` task runs on the default queue;
`ingest_brain` retains its durable task name but ingests the active collection on the
concurrency-1 `local` queue and is not scheduled.

## Knowledge

`HOMELAB_ACTIVE_COLLECTION` selects `about_aleix` (default) or `brain` for the ingest job,
CLI and both readers. LlamaIndex stores each in its own logical `<collection>_chunks` and
`<collection>_docs` tables (physical `data_` prefix). Switching collections leaves the other
collection's data untouched. Public JSON corpus entries load directly as documents: question
then answer, stable id and path, question title, id-prefix pillar type and category metadata.
Bookkeeping, category and pillar type are hidden from embedding and LLM metadata. Non-public
entries are skipped. Invalid individual entries log only their position; structural corpus
errors abort before storage writes. The read-only corpus mount defaults to `/data/corpus`,
with `HOMELAB_CORPUS_FILE=/data/corpus/about-aleix/corpus.json`.

Included Markdown from Brain is loaded with source provenance, split by Markdown structure and
sentence boundaries, embedded by the configured `embed` route, and stored in pgvector. The
Postgres document store tracks source hashes for incremental upserts and deletions. Brain remains
read-only and frontmatter is not embedded. The loopback API retrieves top-k scored chunks with
provenance through LlamaIndex's vector retriever.

`POST /v1/knowledge/answer` shares `/query`'s question and `top_k` validation and retrieval.
One prompt numbers retrieved chunks `[1]` through `[k]` and instructs the `chat` route to answer
only from those chunks with `[n]` citations, or to refuse when they cannot answer. The question
and chunks are untrusted data, not instructions. Answers always refer to Aleix by name in the
third person. One LlamaIndex client call returns an internal JSON answer/refusal signal.
Sources expose number, title, path and score in first-citation order,
without duplicates. Missing or out-of-range citations and malformed replies fail closed to a
plain refusal: “That isn't covered in what Aleix has written here — you can ask him directly.”
A 60-second model timeout with no retries bounds the call. Only the question and chunk text
leave the node; logs contain question length, top-k, retrieved/cited counts, refusal
and elapsed time, never content. This contract works with any indexed corpus.

`homelab eval` measures corpus self-question hit@5 and MRR by stable provenance id, and
refusals through the answer endpoint's code path. It requires the about-Aleix collection and
prints only numbers and ids, without persisting an evaluation artifact. The optional
`HOMELAB_EVAL_FILE` defaults to `/data/corpus/about-aleix/eval.json` in the existing read-only
mount. Version-1 files add three independent sets: bank paraphrases, refusal checks and visitor
questions, the latter also rolled up by group without extra calls. Answer items measure retrieval
against any expected entry and require a non-refused answer citing an expected path for answer
correctness; refusal items require refusal. Retrieval and answer failures are recorded
independently by id and reason. The baseline JSON fields remain unchanged at the root; `sets`
holds additional scores and numeric `eval_file_found` reports presence. Missing files run only
the baseline. File validation errors exclude content, and all evaluations reuse the existing
retriever and answer path.
The baseline, each set and each visitor group include `first_person_count`, counting each
non-refused answer once if it contains whole-word `I`, `me`, `my` or `mine` (case-insensitive,
including quotes). Only counts are exposed, without text or match details.

## Rules

1. Secrets never in git or images — Compose secrets or systemd `LoadCredential=`.
2. Only `api` publishes a port, on loopback.
3. Brain is mounted read-only; everything derived lives on the encrypted volume.
4. Containers run as non-root with `cap_drop: ALL`.
5. Content is not logged by default; ids, sizes, timings and costs are.
6. Spend control is the gateway key's budget and dashboard.
7. Jobs: persist before ack; idempotent or non-retryable.
8. Indexing is local (node, or the owner’s Mac for explicit bulk embedding calls); knowledge
   answering sends only the question and retrieved chunks off-node.
   Explicit vision calls send page images to the owner’s Mac or the selected hosted vision rung.
