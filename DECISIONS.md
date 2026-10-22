# Decisions

Append-only. One paragraph per choice that was not obvious, newest last. Reversals are new entries.

**2026-09-18 — v2 is a clean repository, v1 is archived.** Ten days of v1 produced ~6k lines of
stdlib Python and ~44k lines of process documentation. The infrastructure was sound; the process
and the stdlib-only constraint were the cost. v2 keeps the server and the security posture, uses
mature libraries for machinery, and records decisions here instead of in ADRs.

**2026-09-18 — First slice is the Second Brain.** Questions over the owner's Markdown notes with
cited sources, from Telegram. It exercises models, knowledge and provenance, follows the RAG
certificate being taken, and is used daily.

**2026-09-18 — API-first model access; the v1 helper retires.** Vercel AI Gateway holds the
provider relationship, budget and dashboard. Local inference (llama.cpp on the node; later a GPU
node or the Mac) is one more OpenAI-compatible provider. The helper's credential isolation, fail-
closed governor and count caps were built for subscription CLIs and are replaced by one key in one
process plus the gateway's budget.

**2026-09-18 — Docker Compose wrapped in one systemd unit.** llama.cpp is an image rather than a
host build, services get a private network, and the same file runs on any Docker host. venv +
systemd would have been lighter for a single Python process on a single node forever, which this
is not.

**2026-09-18 — Postgres for jobs, vectors and app state; Procrastinate for jobs; LangGraph for
workflows.** v1's custom Run store (23.1A) is left in v1; its rules — persist before ack, never
blindly repeat effectful work — carry over. LangGraph enters with the first workflow that needs
interrupt/resume.

**2026-09-18 — LlamaIndex for ingestion and retrieval, LangGraph for orchestration.** Messy
sources are expected. The two never touch: LangGraph nodes call the knowledge service as a
function. LangChain retrieval classes and LlamaIndex agents/Workflows stay out.

**2026-09-18 — Public, minimal docs, not a teaching project.** README, this file, ARCHITECTURE and
CLAUDE.md. No guides, briefs or handovers; docs change when behaviour changes.

**2026-09-19 — The Telegram bot is the first client and stays a host service.** Its code moves
here; its unit and TPM-sealed token do not change. It moves into Compose when it is redesigned.

**2026-09-19 — Planning, research and history live outside this repository.** This repository
describes only what exists. Work arrives as self-contained task prompts; plans, research notes and
historical process artifacts are kept elsewhere and are not named here.

**2026-09-25 — LlamaIndex names the Brain tables.** `brain_chunks` and `brain_docs` are logical
table names passed to its adapters; the physical `data_` prefix is accepted because no application
code reads the tables by name and adapter-owned storage should not be overridden.

**2026-09-25 — Use `gitleaks-action@v3` in CI.** Version 2 stopped working on GitHub runners;
version 3 keeps the repository's secret scan in the normal push and pull-request checks.

**2026-09-25 — Inject the Postgres KV store into the document store.** The installed LlamaIndex
`PostgresDocumentStore.from_params` and KV adapter disagree on their arguments. Constructing a
`PostgresKVStore` through its public API and passing it to `PostgresDocumentStore` keeps the
library responsible for persistence without depending on adapter internals.

**2026-09-25 — Build application images on service start and in CI.** The systemd unit pulls
`main` and runs Compose with `--build`, making an agent restart deploy the current Dockerfile;
CI builds that image so Dockerfile failures are caught before a protected-main deployment.

**2026-09-25 — Install `libpq5` in the runtime image.** The PostgreSQL adapter needs the native
client library at runtime, and the first node deployment exposed its absence from the slim base
image. A system package supplies it without adding a Python dependency.

**2026-09-25 — Keep the gateway secret root-owned and group-readable by the app.** File-backed
Compose secrets retain host ownership and mode. `root:10001` with mode `0440` lets the non-root
containers read the key while retaining root ownership on the encrypted volume.

**2026-09-25 — Use `AGENTS.md` as the shared repository guide.** The original `CLAUDE.md`
guidance is now maintained in one file for both tools; a one-line import keeps Claude compatible
without duplicating instructions.

**2026-09-25 — Configure application logging under Uvicorn.** Uvicorn only handles its own logger
hierarchy, so application INFO metrics otherwise disappear when the root logger has no handler.
Give `homelab` its own stderr handler at startup and a configurable log level (INFO by default),
without changing Uvicorn's logging or recording content.

**2026-10-01 — Record host operational files under `node/`.** Paths mirror the node's filesystem:
`node/<path>` installs at `/<path>` from the node's clone of merged `main`, after backing up and
showing installed-file diffs. All are `root:root`; units and drop-ins use `0644`, scripts use
`0755` and sudoers uses `0440`. The installed bytes were recorded before adoption so changes
remain reviewable. Container route configuration stays in `config/routes.yaml`.

**2026-10-01 — The agent has root through sudo; the owner approves plans.** The repository now
records the node's existing `homelab-agent ALL=(ALL) NOPASSWD: ALL` grant. Its SSH key is accepted
only from the owner's Mac. The owner approves what a task touches, its risks and recovery before
execution, rather than each command. Any irreversible step outside that approval stops the run;
recording this security boundary requires the owner's approval before merge.

**2026-10-01 — Volume users rely on a mount condition and target lifecycle.** They declare
`ConditionPathIsMountPoint=/srv/homelab`, `After=homelab-data.target`,
`PartOf=homelab-data.target` and `WantedBy=homelab-data.target`, without requiring the target or
placing working, root or state directories on the volume. systemd creates mount dependencies
for those directories and runs dependency jobs before conditions, causing a locked volume to
fail and alert instead of being skipped. `homelab.service` retains its Docker requirement and
uses an absolute Compose file path in start and stop commands; Compose preserves the named
project and resolves relative paths from that file's directory. Pytest checks the contract for
every recorded unit wanted by the data target.

**2026-10-01 — The notifier recognises the `homelab` alias.** The stack already invokes
`homelab-notify@homelab.service`, but the shared script rejected that alias and dropped alerts.
Mapping it to `homelab.service` fixes v2 alerts while keeping all five aliases still used by v1.

**2026-10-01 — Verify systemd units on the node, not in CI.** `systemd-analyze verify` checks host
executables and users that the CI runner does not have. Run it after installation on the node;
the pytest volume-contract check runs inside the existing required CI `test` job, where it
gates merges without inventing host users or substituting executables on the runner.

**2026-10-01 — Grounded answers use one retrieval and one chat call.** The answer endpoint shares
the query request and retrieval, numbers chunks in one prompt and asks the configured `chat`
route for a JSON answer/refusal signal with inline citations. It returns cited provenance in
first-citation order. Malformed replies, missing citations and invalid source numbers fail closed
to a plain refusal rather than exposing an unsupported answer. The model has no tools; only the
question and retrieved chunk text go to it. Metrics contain counts and timing without content.

**2026-10-01 — Adopt the Telegram bot under mirrored host paths.** Its public installed code,
unit, credential/failure drop-ins and chrony-only polkit rule are recorded under `node/`, then
formatted and adopted in separate commits. Private allowlists and the TPM-sealed token stay on
the node. The bot remains a stdlib host client: `/ask` uses the loopback answer API, the old model
helper client and `/spend`/`/model` go, and host commands retain their capability gates. Output is
only text sent to Telegram, never instructions to dispatch. Handler errors log command and
exception type without exception text; requester IDs remain metadata in the node's journal.

**2026-10-01 — Register Telegram commands from the router at startup.** `setMyCommands` and
`setMyDescription` derive from the same registry as help, including aliases, so the phone menu
tracks the actual commands without a separate setup script. Registration failure is logged with
safe metadata and does not stop polling. Plain-text replies are split at 4096 UTF-16 units so long
answers and source lists fit Telegram without cutting a Unicode character.

**2026-10-01 — One active collection setting, separate stores.** `HOMELAB_ACTIVE_COLLECTION`
defaults to `about_aleix` and selects the loader and logical table prefix for CLI ingestion,
the existing durable ingest task and both API readers. Brain remains available by configuration
without modifying its indexed data or changing public request and response shapes.

**2026-10-01 — Load the about-Aleix JSON directly.** Public entries become question-and-answer
documents with stable entry ids, question titles and id-prefix pillar types. The provenance
path is the entry id, allowing existing readers and citations to identify entries. Category is
retained but hidden with bookkeeping metadata. A read-only corpus mount keeps draft content
outside the repository; structural errors abort before writes and bad entries log only positions.

**2026-10-01 — Evaluate corpus self-questions from the CLI.** `homelab eval` reports hit@5 and
MRR against each entry's own id, plus retrieval miss ids and answer refusal count and ids. It
uses the same answer code path as the API and prints numbers and ids only; no eval file or
personal content is committed or logged.


**2026-10-02 — Use DeepSeek V4.1 Flash for the chat route.** The owner selected
`deepseek/deepseek-v4.1-flash` through Vercel AI Gateway for lower-cost grounded answers and
explicitly approved sending the draft corpus questions and retrieved chunks through this route
for evaluation. The shared setting applies to API answers, Telegram `/ask` and the CLI eval;
local embeddings and both collections' indexed data remain unchanged.


**2026-10-02 — Score independent eval sets alongside the baseline.** A version-1 eval file in
the read-only corpus mount tests paraphrases, unsupported questions and visitor groups without
committing personal content. Answer correctness requires a non-refusal citing at least one
expected entry; retrieval hit and rank are separate measures. Multiple failure reasons preserve
both retrieval misses and answer failures. Existing baseline fields remain at the JSON root,
with added `sets` and numeric `eval_file_found`; a missing file leaves the baseline runnable.
Visitor groups roll up already scored items, avoiding extra model calls. Empty subsets report
zero rates. No endpoint, answer prompt, model route or refusal behaviour changes.


**2026-10-02 — Restore Luna for answer latency and keep eval runs alive across call errors.**
The owner selected `openai/gpt-5.6-luna` through the gateway, replacing DeepSeek V4.1 Flash
after slow calls and repeated eval timeouts. Eval catches per-item retrieval or answer errors,
records `error` with the exception type only, and continues without retrying. Error counts
are included in baseline, set, expectation and visitor group blocks. All items stay in metric
denominators; completed retrieval scores survive an answer error, while retrieval errors score
zero and skip that item's answer call. Baseline retrieval errors appear as error failures rather
than miss ids. Answer-call latency measures the entire answerer path, includes failed attempts,
and reports count, median, nearest-rank p95 and maximum seconds (zero for no calls). No answer
endpoint, prompt, timeout, retries, refusal behaviour or bot code changes.


**2026-10-02 — Answer supported parts in the third person.** The answer prompt covers relevant
parts supported by numbered chunks, identifies uncovered parts and signals partial answers,
refusing only when nothing relevant is supported. It always refers to Aleix by name in the
third person. API constants keep the name and fixed refusal and partial invitation together;
the endpoint appends the invitation only after citation validation. The model's partial flag
is a required strict boolean, the public response exposes it, and every refusal clears it.
Existing fail-closed checks remain. Eval adds partial counts to sets, expectation blocks and
visitor groups without changing scoring: partial answers still need an expected citation on
answer items and fail refusal items. Retrieval, routes and clients remain unchanged.


**2026-10-02 — Revert partial answers after the acceptance bar failed.** The deployed partial
answer slice passed bank paraphrases (22/22) and visitor answers (21/23) in its first run,
but refusal checks fell to 9/33 and visitor refusals to 4/5, below the required 26/33 and
5/5. The owner required both sequential runs to pass every threshold. Restore the preceding
answer prompt, response shape, refusal text and eval reporting through a separate validated
revert PR. Keep the original decision here as append-only history. No corpus, answer key,
retrieval, model route or client changes are made.


**2026-10-02 — Restore third-person voice and the invitation without partial answers.** Add
only a third-person rule naming Aleix to the strict answer prompt, and use the fixed refusal
inviting visitors to ask him directly. The partial-answer logic from PR #10 stays reverted as
recorded by PR #11; internal JSON remains answer/refused, public responses keep sources, and
citation validation still fails closed. Eval counts non-refused answers containing whole-word
I, me, my or mine case-insensitively, once per answer, including quotes. Baseline, set and
visitor-group counts contain no text or match details and reuse existing calls. Retrieval,
routes, clients and other endpoints are unchanged.

**2026-10-05 — Remove the retired v1 installation from the node.** Remove only the owner's
approved list of v1 programs, units, configuration, state, accounts, checkouts and stale backups,
after checking checkout cleanliness and pushed HEADs, metadata hashes against the owner's
off-node copies, and disabled/inactive units. The helper's gateway key was already revoked.
Keep Brain, v2's files and data, base configuration and every Docker image. Prune only Docker's
build cache. The notifier now accepts exactly `bot`, `watchdog` and `homelab`; remove the retired
`model-helper`, `workbench` and `harness` aliases. Restart the bot to drop the removed group and
send one deliberate `homelab` failure alert to validate the installed notifier.


**2026-10-05 — Recovery is a rebuild from sources, with no node backup.** Record the installed
base configuration byte for byte under mirrored `node/` paths, retaining its historical
comments without adopting v1 scripts. The owner permits the login name in SSH hardening;
addresses, tailnet names, emails, Wi-Fi names, chat IDs, UUIDs and all key or secret values stay
excluded. README contains the rebuild procedure, and every PR changing `node/` updates it.
Code is cloned from public GitHub over HTTPS; Brain working notes and the corpus are copied
from the owner's Mac, excluding Brain's Git metadata. Postgres is derived by re-ingestion,
models come from upstream and secrets are re-issued. No GitHub credential is provisioned on
the rebuilt node. The owner authorized removal of the old node GitHub SSH key, its public key
and its github.com-only SSH config, retaining known_hosts and authorized_keys, and handles
revocation in GitHub. Identify the Samsung MZ7TY256 system disk by model and 238.5 GiB size;
leave the deliberately unused 476.9 GiB Micron MTFDDAV512TBN untouched. Kernel disk names are
not stable identifiers. The runbook is documented without a rehearsal.


**2026-10-06 — LiteLLM is the single purpose route table.** Replace `config/routes.yaml` with
`config/litellm.yaml`; all callers use purpose names through the same OpenAI-compatible proxy.
Only LiteLLM holds the gateway key and joins `homelab-models`, without a host port or proxy
authentication database. Pin official BerriAI release `v1.103.3` (2026-10-03) to
`sha256:e6e1c46cec92ab58b7ff95c790420aba9f05269b662a66c0dd11714171b9c64b` rather than pulling
mutable tags or the PyPI releases compromised on 2026-03-24. Release 1.104.0 requires a master
key, so it cannot serve this slice's accepted-and-ignored client-key contract. Disable telemetry
and remote price-map fetching, use bundled tokenizers and run non-root with the existing secret
group. Hooks enforce exact aliases and their reasoning efforts, preserve the real embedding
model in responses, and redact upstream errors; middleware limits the HTTP surface and rejects
routing overrides. A single HTTP audit line replaces vendor/access logs that can expose content.
The app discovers purposes from `/v1/models`, preserving one route table and the existing index.


**2026-10-07 — Measure local vision before choosing routes.** A generic Mac-only benchmark
renders externally supplied PDFs at 150 DPI, transcribes with one fixed prompt and compares
normalised text using character/word Levenshtein error rates. Figure descriptions use a second
fixed prompt and remain local for human judgement. Sequential fresh llama.cpp processes bind
only to loopback, disable prompt caching and report launch-to-ready time and Darwin lifetime
peak physical footprint including Metal. Numbers contain sample indices and numeric language/
kind codes, never source names, text or page numbers. Private inputs and optional review outputs
are rejected inside Git repositories. PyMuPDF and RapidFuzz are benchmark-only dependencies;
no node service, route or final model choice changes.


**2026-10-07 — Remeasure vision with order-insensitive scoring and fixed prompts.** Use
NFKC, soft-hyphen removal, line-end word joining and casefolding before whitespace word-bag
recall, precision and F1. Count duplicate tokens and aggregate corpus counts; retain punctuation
in metric tokens. Mark references invalid below 80% alphabetic whitespace tokens after stripping
Unicode punctuation, including empty references, and exclude them from accuracy aggregates while
still transcribing. Retain CER/WER as secondary measures. Report GGUF plus projector file sizes
and Darwin lifetime peak footprint as an explicit estimate including mapped weights, which can
double-count resident mappings. Add the fixed exact and reconciliation prompts, pairing two
prior output directories by sample index with one image per request; require 300 DPI for
reconciliation. Named runs refuse existing output directories, and figure descriptions can be
omitted. All samples, images, text and results remain outside Git; no route or model is chosen.


**2026-10-08 — Serve vision on demand from the owner’s Mac.** Add exactly `vision` and
`vision:xhigh` to the single LiteLLM route table. Local vision uses the measured Qwen3-VL-8B
Q4_K_M, F16 projector, Metal and context 16,384; the hosted rung uses gateway
`deepseek/deepseek-v4.1-flash` without configured reasoning effort. The Mac binds only its
runtime Tailscale IPv4 on port 8090, with the owner’s node-only tailnet policy as its gate.
The address stays in the node’s private `.env`, passed only to LiteLLM; an empty setting uses
a closed loopback endpoint so proxy startup does not depend on the Mac. The pinned proxy
accepts numeric YAML timeouts; a deployment hook converts this route’s 300-second timeout to
HTTPX with a separate 3-second connection limit. No retries, fallback or stopped-Mac alert.
The owner starts/stops a PID-managed native process; no login/boot job or wake lock. Keep the
existing model files in place, and private state/log under `~/.local/state/homelab-vision/`.
Clients own page prompts, DPI and generation settings, and retain queued pages on failure.


**2026-10-08 — Add a larger local embedding purpose and opt-in text roles.** Keep `embed`
and its plain-text vectors unchanged; add exactly `embed-large`, without tiers or fallback.
Use the official `Qwen/Qwen3-Embedding-4B-GGUF` file `Qwen3-Embedding-4B-Q8_0.gguf` at
revision `f4602530db1d980e16da9d7d3a70294cf5c190be`, fetched by llama.cpp from an immutable
URL into `models/embed-large`. A dedicated cache owned by `10001:10001` permits a non-root
server without changing Nemotron's cache or service. Use the same llama.cpp image, CPU only,
four threads, context 8192, batch/microbatch 2048, last-token pooling and explicit L2
normalisation, as required by the official model cards. No client network or published port.
Optional `input_type` accepts only `query`/`passage` on string or list-of-string input;
`model_info.input_prefixes` holds each purpose's wording. Remove the field upstream; absence
leaves input unchanged. Nemotron gets its question/document prefixes, Qwen gets a generic
English retrieval instruction for questions and plain passages. Both embedding responses
report their real models. Mac vision audit lines take the served name from the response,
while client responses retain the purpose. Existing callers and indexes do not use roles;
clients adopting roles must re-embed passages, and Qwen's 2560 dimensions require accounting
for pgvector HNSW's 2000-dimensional `vector` limit (4000 for `halfvec`). No dimension handling.


**2026-10-08 — One purpose per kind of work.** Add `grade` for yes/no relevance judgements
per candidate passage and `translate` for passage translation on request. Both use exactly
`chat`'s gateway model, API base, key reference and low reasoning effort for now, so either
purpose can later move to a smaller or local model without a client change. No tiers, wildcard
or fallback; prompts and parallel-call scheduling belong to clients. Existing purposes,
request fields, audit lines and real-model handling stay unchanged.


**2026-10-08 — Copy only originals, encrypted directly to removable media.** Each project
keeps irreplaceable inputs under `*-data/originals`; discovery has no maintained list.
Move v2's corpus into that convention, retaining its layout and read-only mount. Tar streams
through age with the owner's public recipient on the node and directly onto an external Mac
volume; neither machine stages an archive. The Mac flushes and checks the encrypted byte
count before a separate confirmation atomically records the time and bytes. A daily timer
reminds after strictly seven days (or no copy), using the existing TPM credential and notifier.
The private identity stays in the password manager; restore is an owner-run memory-only
identity pipeline. Folder sizes and encrypted byte counts are metadata, never content logs.


**2026-10-08 — Telegram starts a fixed Mac backup action over Tailscale.** The first node-to-Mac
control channel is a stdlib launchd user agent binding only the runtime Tailscale IPv4 on TCP
8091. The node-only tailnet rule is the gate, with no listener key. Exactly one argument-free
`POST /backup` invokes the existing SD Card copy; a nonblocking lock refuses concurrent work.
Parse the existing final confirmation line rather than changing the copy CLI; return only
status, file name and encrypted bytes, with fixed short failure reasons. A 180-second timeout
kills the copy process group, including SSH, before releasing the action lock. The bot's
privileged router gate applies; it reads a private URL file and uses one direct HTTP call with
a longer 210-second timeout, without redirects or retries. Its sandbox, sudo and polkit remain
unchanged. Missing card and unreachable Mac have actionable Telegram replies, and the weekly
reminder now points to `/backup`. Addresses remain private and logs contain only metadata.


**2026-10-08 — Runbook moved to docs/operations.md.** README stays a short overview.


**2026-10-12 — A bulk embedding purpose uses the same vector space on the Mac.** Add
`embed-large-bulk` for bulk ingest with the official Qwen3-Embedding-4B Q8_0 file, identical
pooling, normalisation and batch/context flags, plus Metal. The measured Mac throughput is
about 18 times the node’s CPU; questions keep using always-on `embed-large`, so student use
does not depend on the Mac. The name identifies the vector space and kind of work, without
a tier. Real-model reporting and role prefixes match the node purpose. Serving is started
and stopped by hand on Tailscale TCP 8092, reachable only by the node under the owner’s grant.
The private URL stays beside vision’s URL in the node’s `.env`; a closed default permits
proxy startup without it. A 3-second connect limit, no retries and no fallback make missing
Mac serving fail fast rather than silently moving a long ingest back to the slow CPU.
Private state, PID start-time checks and content-disabled logs follow vision, with no
automatic start or wake lock.


**2026-10-14 — Hosted image purposes share LiteLLM with a separate gateway budget.**
The owner selected Nano Banana 2, Nano Banana 2.1, both GPT Image 2.5 variants (Flare and
Sunburst), and FLUX 3 Image if its edit route works. Four `image-*` entries use the OpenAI
provider path to Vercel, because the pinned Vercel provider has no image-edit config.
Only images use a new Compose secret with a $20 monthly key budget; chat's key is unchanged.
The proxy exposes edits, generations and capability discovery, while Nano Banana 2.1 retains
its chat request shape. Capability discovery projects only names and configured profiles,
never LiteLLM deployment metadata. Published nested settings remain narrowly allowed, and
image errors are sanitized with a clear 402 budget message. No node image processing,
retries, fallback, proxy budget or master key. Direct proofs must supply response forms and
timings before merge; documented reference limits are not represented as full-count tests.
GPT entries use LiteLLM's native parameter-drop setting for the legacy `response_format`
selector rejected by its generation validation; GPT output is implicitly base64 and URL
requests are refused. No provider adapter or image conversion is introduced.
The direct two-reference FLUX 3 edit succeeded, retaining FLUX 3 rather than FLUX.2 pro.
Nano Banana 2 was excluded after three two-reference edit calls returned 400. On the owner's
authorized diagnostics, the exact repeat and a call omitting size both returned a sanitized
language-model classification refusal. Its model page documents language APIs despite the
general edits guide listing it. The owner authorized removal rather than a chat-route
substitution; any later route is the owner's decision. The remaining four profiles retain
their successful measurements, with no null capability fields.


**2026-10-14 — Remove FLUX routes whose gateway response the pinned LiteLLM rejects.**
FLUX 3's gateway response contains `usage.input_tokens_details: null`, rejected by
LiteLLM 1.103.3 and main as checked by the orchestrator on 2026-10-14. Remove its entry;
it returns when LiteLLM accepts that response. The owner's permitted FLUX.2 pro alternative
also returned an image but omitted the same required field and failed the pinned
`ImageResponse` parser, so it is not added. No adapter, response rewriting or sanitizer
change. The client agreed that combined size fields publish only `sizes` entries containing
`value`, `aspect_ratio`, `resolution` and `price`; separate settings retain distinct named
fields under `aspect_ratios` and `resolutions` or `qualities`. Existing GPT and Nano Banana
2.1 profiles already satisfy that rule and stay unchanged.

**2026-10-14 — Estimate GPT Image 2.5 reference cost from measured Flare usage.** One versus two synthetic 2048 px references on Flare used 1,521 additional image tokens; at OpenAI's $8/M image-input rate ([OpenAI pricing](https://developers.openai.com/api/docs/pricing)), this estimates $0.0122/reference. Sunburst uses the same estimate by assumption.


**2026-10-15 — One native Mac router loads vision and bulk embeddings on request.** Replace
the two hand-started servers with llama.cpp router mode and per-model INI presets preserving
their exact inference flags, projector and gateway model names. A generated user LaunchAgent
keeps the lightweight router available at login on the existing node-only Tailscale port 8090;
8092 is freed. Both private node URLs point at the router. Native autoload and idle sleep
(default 600 seconds, configured once) own the model lifecycle, rather than adding start/stop
actions to the backup listener or building model management ourselves. The listener on 8091
and cleanup server on 8095 stay independent. No profiles, allow-lists, app quitting or system
settings are introduced. Preserve embedding vectors, real-model reporting, 3-second connect
limits and 300-second request limits. Runtime paths and addresses stay private; content logs
are disabled. The retired vision pointer retains address discovery for the unchanged listener.


**2026-10-15 — Owner-selected Mac usage profiles restrict native router presets.**
Working and Inference are private editable configurations, with custom determined by observed
service availability, app closures and system settings. Keep model lifecycle in llama.cpp:
render only permitted presets and use native reload, verifying its resulting model list.
The gateway reads that list before Mac calls and emits a fixed, sanitised HTTP 503 naming the
purpose and Inference, with a boolean on the existing purpose audit line. No request rewriting,
client queue or listener change. Only the owner enters Inference; recognised VS Code descendant
work requires an explicit confirmation flag. Record closures before attempting graceful quits
so a cancellation or crash leaves recovery information. Restore only recorded apps/services;
previously stopped services stay stopped and can leave custom state after return. A 30-second
launchd check returns after 900 seconds without loaded router models or jobs.

The privilege boundary is one root-owned standalone script with two exact sudoers argument
forms. An isolated system Python reads only bounded numeric settings from the invoking sudo
user's private file, then calls fixed absolute pmset/sysctl binaries without a shell. This
keeps settings editable without granting arbitrary commands; rejects symlinks, nonregular,
oversize or foreign-owned files; and caps GPU wired memory at 44 GiB. Install only after merge
with the owner's password and protected root-owned parent directories. launchd owns calendar
jobs and deferred idle launches; one pending bit coalesces missed runs, without a work queue.
A job context marker and worker-ancestry check reject Inference entry, including through
wrappers that strip environment variables; jobs execute outside the VS Code process tree. The public example contains no owner's app list or paths.


**2026-10-15 — Profile return guarantees app relaunch, not document restoration.**
Mac acceptance proved graceful quits and automatic relaunch of TextEdit and dictation.
TextEdit did not reopen its saved document with macOS’s default “Close windows when quitting
an application” behaviour. The orchestrator resolved this escalation: acceptance checks app
relaunch; window and tab restoration belongs to each app’s settings. Document Chrome’s
“Continue where you left off” and the macOS option for the owner to choose, without changing
any app or system preference.


**2026-10-15 — G14: Working follows the owner’s power mode; Inference requires AC.**
Remove power mode from Working’s defaults, settings application and matching; legacy private
fields are ignored without rewriting the owner’s file. Inference requires AC before any
switching side effect and selects High Power; the privileged helper rechecks AC before writes.
Record the observed power mode before entry and restore it on every return, including idle
and unplug returns. The existing 30-second launchd check prioritises unplug return even while
models or jobs are active, reopening precisely the recorded apps/services. Sleep prevention
and the 45056 MB GPU limit remain unchanged. Keep the two exact sudoers argument forms:
the standalone helper validates the invoking owner’s private recovery record, accepting only
integer modes 0–2, rather than accepting extra command arguments. Ordinary Working invocation
with no pending return leaves power mode alone.


**2026-10-15 — G14 amended: battery pauses Inference after a ten-minute grace.**
Supersede unplug return: observe continuous battery time in the existing launchd check; a
short unplug leaves Inference running, and replug resets the timer. After 600 seconds, retain
the Inference session and recovery record while native preset reload unloads/disallows all
models. Apps and dictation remain closed. Status exposes Inference with paused/on_battery
metadata; manual heavy starts and new jobs cannot bypass the pause. Paused time suspends idle
return. Replug resumes that existing owner-entered session, restores High Power before the
allow-list and starts a fresh idle window. The owner can return to Working on battery at any
time, restoring the original apps/services and recorded power mode.

Publish only a pause boolean through llama.cpp’s native static-file serving on the existing
node-only router address/port; its dedicated directory contains no private configuration or
paths. The gateway uses that boolean for a fixed purpose-specific battery-pause 503 and adds
`profile_paused` to the existing audit line. Native presets still enforce the allow-list; no
request rewriting, custom HTTP server, listener change, queue or new sudoers arguments. A failed
resume retains paused state and its published reason; the existing check retries transitions.
An explicit Working return records its intent before restoration, so a failure is retried
instead of resuming Inference when AC returns.


**2026-10-15 — G14 correction: preserve separate AC and Battery settings exactly.**
The owner’s AC sleep is never while Battery sleep is one minute. The former `pmset -a`
application and Working sleep default would overwrite those choices; they are superseded.
Inference records AC power mode and AC sleep from `pmset -g custom`, plus the global GPU
limit, before any switch side effect. Only AC mode/sleep are changed (`pmset -c`); Battery
and display settings are never written. Return to Working restores all three recorded
values exactly, with existing numeric bounds and complete-record validation before writes.
Working owns no system-setting defaults or matching requirements. Legacy Working fields
are bounded and ignored; without a pending return the helper writes no pmset settings and
only resets an orphan nonzero GPU limit to zero. Missing recovery fields cause refusal,
rather than guessed restoration. The two exact sudoers arguments stay unchanged. Reinstall
the corrected root-owned helper after merge; privileged acceptance captures both power
sources and proves exact restoration without switching apps or quitting VS Code.

**2026-10-15 — The Mac menu bar app stays a client of the profile command.** The SwiftUI
`MenuBarExtra` runs the checkout's `homelab-profile` with the interpreter the installer records
in its bundle, the same pair the profile LaunchAgent runs, rather than a copy that could drift.
What it needs that the command lacked became tested subcommands: `plan-inference`, `waiting`,
`usage` and `schedule`. Read-only reports skip the command lock, so a slow node read never
delays a tick. It starts at login through a user LaunchAgent, which works ad hoc signed; it is
not a login item. Inference asks once from the plan; the guard's own refusal, not the app,
decides whether *Switch anyway* is offered.

**2026-10-15 — The night window decides each night once, at its start.** It lives in the
existing profile agent's tick, not in a job, and reaches Inference only through `switch`, which
still refuses job context and repeats the AC and guard checks. The night is recorded before any
side effect, so a skipped or failed start is not retried every 30 seconds. A first check more
than five minutes after the start skips the night: waking a sleeping Mac in the morning must not
quit the owner's apps. Saving the window inside its hours skips that night for the same reason.
Manual actions end ownership but keep the night recorded, so a manual return is not re-entered.
The 15-minute idle return still applies inside the window; the end only returns a session the
window still owns. A night-window error never stops the rest of the tick.

**2026-10-15 — Usage figures are read from local files only.** Codex's newest five-hour/weekly
`rate_limits` entry is read from its session logs, newest files first, as of the entry's own
timestamp. Claude's come read-only from `~/.claude/usage-now.json`, which the owner's status line
already writes with the owner's approval, as of its `at` time; a missing or invalid file is
unknown. The repository ships no status-line script and the app never writes usage. Each source
reports its age. Waiting estimates use only published `seconds_per_image`; other purposes show
their count with no estimate.


**2026-10-16 — Install the Mac control plane from main.** Login agents previously ran the
working checkout, so checking out an executor branch changed live code. One private installed
copy now contains the commands, stdlib-only sibling dependencies and menu sources, stamped
with a clean commit contained in freshly fetched `origin/main`. Use the stable Homebrew
`/opt/homebrew/bin/python3` entry point for every agent and menu subprocess, recorded in the
install, rather than any checkout virtual environment. Existing job plists are rewritten in
place without changing calendars, job commands or private state. Dependency comparisons restart
only affected loaded agents; menu sources/path changes rebuild the installed app. Router refresh
briefly interrupts vision and bulk embeddings, so operators check med-ask before updating.


**2026-10-16 — Profile service states and model selection remain native router policy.**
Private version-2 profiles give each routed service off/on request/on plus allowed ids and a
default from sibling version-1 `models.json`. Explicit migration backs up the old file and
copies the current two presets' exact flags, with unchanged default permissions. Non-router
services remain off/on. The command alone reads/writes profiles for clients.

One native preset per allowed model uses `<purpose>-<id>` as its canonical name and the purpose
as the default's alias; the legacy embedding upstream name also aliases the default. This
avoids duplicate child processes for default and explicit requests. On disables native idle
sleep and uses startup load plus the native load API after reload; no supervisor is introduced.
Sleep settings move out of router CLI because CLI overrides presets. Build 11429 supports
unlimited runtime capacity with zero but rejects startup loads against zero, so capacity is
sized to the entire registry (at least four); registry additions require a router restart.
Two bounded LiteLLM patterns route explicit Mac model names without per-model gateway entries.
The hook checks native ids/aliases, retains refusal sanitising and audits only the base purpose
for dynamic names. All unavailable-profile refusals say “not available in this profile”.


**2026-10-16 — Measure registrations separately and report native load failures honestly.**
Model registration copies an existing service entry's flags and configured parent directory,
verifies Hugging Face's published file hashes and loads through a temporary standalone
loopback llama-server. This measures Darwin physical footprint without changing live native
presets. New entries remain unallowed everywhere; only final registry writes hold the state
lock. The registry needs no new fields or version; optional version-2 profile memory budgets
retain compatibility and explicit migration backs up existing files. Budgets sum all pinned
models plus the largest on-request model, reporting unknowns rather than estimating them.
The owner replaced the proposed memory-specific refusal: llama.cpp `d81235049` reports a
generic failed load and shared exit code, so the gateway says the Mac could not load this model
now (often for lack of memory), and to try later. There is no memory precheck, upstream change,
proxy or retry. Gateway deployment waits for the orchestrator's med-ask ingest check.


**2026-10-16 — Charge mapped model files alongside measured footprint for profile budgets.**
The Mac verification showed that Darwin physical footprint alone omits the mmapped GGUF
weights and understates the loaded model's memory. Registration and measurement now add the
configured weights and projector file sizes to the largest post-warmup process-family
physical-footprint sample, rounding the byte sum up once to MiB. File identity deduplicates
hard-link aliases. This follows the existing vision benchmark's file-plus-footprint method
and accounts for all configured weights without a custom resident-page or Metal introspector.
It deliberately provides a conservative budget allowance: copied weights may already appear
in charged allocations, so some bytes can be counted twice. It is not an exact wired-memory
counter or a promise about later requests. Preset flags, isolated loading and the warning-only
budget rules remain unchanged. Existing registry entries require explicit remeasurement;
the earlier footprint-only values cannot be corrected reliably without a load.

**2026-10-16 — Apply current-profile service edits before committing the profiles file.**
The Profiles window sends the complete document through the command on stdin, preserving
fields it does not edit. `profiles-preview` shares save validation and budget calculation,
so the app owns no memory policy. `profiles-set` observes the profile under the command lock,
applies only changed services through the existing native preset reload/load path, then
atomically saves. It does not re-enter Inference, quit configured apps or recapture power
recovery. Failure attempts to restore previous services and leaves the profiles file unchanged;
a failed restoration explicitly asks for a recovery switch. Custom state defers application,
and paused Inference retains empty native presets until the existing AC resume. Other-profile
edits and CLI changes to power/app/job fields still wait for a switch.

**2026-10-16 — Independent encrypted bulk SSD for rebuildable storage.** The Micron holds
one LUKS filesystem at `/srv/homelab-bulk` for Docker, containerd and client rebuildable files;
models move there now. Both persistent runtime roots move by configuration because Docker
29's containerd image store does not follow Docker's data-root. A 4096-byte root-only key
on main unlocks bulk after main, and an owner passphrase slot recovers bulk without main.
No TPM dependency is added. Docker/socket/containerd are disabled at boot and gated on both
mounts; the existing unlock script starts them before the data target. Host alerts remain
independent. Copy both roots with metadata intact and retain the renamed originals and old
models until an owner reboot/unlock proof; cleanup is a separate approved deletion. The
default Docker builder garbage-collects toward 50 GB. Databases and irreplaceable inputs
stay on main, with originals covered by the weekly copy.

**2026-10-19 — A mature library owns the host bot's Telegram machinery.** This supersedes
the earlier decisions to keep the bot stdlib-only. As the bot grows into a conversational
assistant, python-telegram-bot supplies polling, sending and command registration, with
inline keyboards, callback queries and file downloads available for later work. The router
and executors retain the commands, replies and access gates. A hash-pinned environment
under `/opt/homelab-telegram-bot/.venv`, built with host Python and installed with
`pip --require-hashes`, keeps the bot independent of Docker and volume unlock. The host
user, sandbox, TPM-sealed credential, TLS verification and no-listening-port boundary stay
unchanged. Tests use the same library through the `telegram-bot` dependency group.


**2026-10-19 — Planning stays a read-only encrypted-volume mirror, with event notices.**
The host bot gains optional planning reads through a private configuration file and a
read-only deploy key generated on the node. Git owns clone/pull machinery; no model, listening
port or planning write channel is introduced. The mirror and outbox marker belong to
`homelab-bot` under `/srv/homelab/frontline/`. Only that path becomes writable in the sandbox.
A separate data-target hook restarts the bot after unlock to refresh its mount namespace.
On target stop it stops the bot to release its private writable bind, which otherwise pins
the filesystem after host unmount. A separate release helper waits for host unmount and
starts the bot against the locked root filesystem; a retried unlock owns the restart instead.
This briefly interrupts the bot during lock without making boot-time status depend on the
volume or changing the unlock script or its existing units. PID 1's target state gates reads so retained namespace mounts do not expose
planning while locked. Missing configuration disables only the feature.

The first outbox read starts at the current end. Thereafter each confirmed send immediately
persists line and recipient progress through an atomic replacement with fsync. The owner
chose at-least-once delivery: uncertain sends retry, accepting a rare duplicate between
Telegram delivery and durable local acknowledgement rather than losing a notice. No periodic
summary is sent; read commands provide pending groups, a requested recap and full slice details.


**2026-10-20 — The host bot reads planning through a loopback model purpose.**
LiteLLM now publishes `127.0.0.1:4000` so the sandboxed host bot can use the same purpose-only
model endpoint as container clients. Only API and LiteLLM publish, both on loopback; no other
service changes. `frontline` uses chat's gateway model and low effort. A synthetic request
asked for gateway zero data retention, but HTTP 403 confirmed that the current Hobby team
cannot use it (Pro/Enterprise required). The option stays off; no account setting changes.

Every free-text question from the main allowlist receives privately configured summary
files and computed pending work. A bounded JSON second pass can read one more clone text
file; this avoids another dependency and gives the model no actions or writes. Paths stay
inside the clone, exclude Git internals and accept only size-capped regular UTF-8 text.
Episodes stay in bounded memory, with only the active episode sent as history. Follow-up
heuristics retain the topic; a new topic, command or 30-minute silence closes it. Explicit
Telegram replies reopen retained episodes. Restarts clear history rather than persisting
conversation content. Existing commands, allowlists, sandbox and content-free logs remain.
Long replies prefer paragraph boundaries, with smaller boundaries for oversized paragraphs.


**2026-10-20 — The bot proposes orders; only an owner's tap queues them.**
The frontline model returns at most one structured proposal, never executable commands.
The bot refreshes and validates against the read-only mirror, displays the exact action,
verbatim owner answer or full merge-review verdict, then requires an allowlisted Yes/No tap.
All merge approvals require an orchestrator review, not only ⛔ slices. Either tap closes
the episode. Orders snapshot state, paused and attention kind/since; every successful pull
invalidates changed expectations or lost preconditions as overtaken, including attention
answered from the orchestrator without a changed timestamp.

A standard-library HTTP listener binds only the node's runtime Tailscale IPv4 on TCP 8110,
retrying absence without fallback. Like the Mac's 8091 channel it has no key: the owner's
Tailscale policy gates it. This host exception leaves loopback container APIs unchanged.
GET lists durable queued orders repeatedly; POST receipts remove them idempotently. The
Mac executor is separate work. Both routes refuse access before unlock. Atomic, fsynced
orders and pending Telegram edits live beside the clone, never in it; closed records are
retained without automatic pruning. Long confirmations display all content before buttons.
Only order metadata is logged. Existing sandbox permissions suffice: the unit changes a
comment only, with exposure 1.3 before and for the candidate. No dependency, credential,
polkit, allowlist or planning-write change is needed.


**2026-10-20 — The bot keeps an encrypted inbox without turning inputs into actions.**
Free text can be a question, an order, or something to keep. A separate clarification-only
call asks no more than two questions; the owner can force saving immediately. Stored episodes
and reply mappings are removed, so inbox content does not become future question context.
An atomic directory per id publishes JSON metadata and optional original attachment together;
fsync precedes acknowledgement. This avoids partial visible records without another dependency.
Items remain new and local; orchestrator targets are validated repository names, with no new
network route or change to orders. The store layout is documented for a future Mac reader.

Public-page fetching uses standard-library HTTP with checked numeric socket destinations and
original-host TLS/SNI. Every DNS result and redirect must pass the public-address gate; IPv6
transition/site-local addresses are conservatively refused too. Five seconds, three redirects
and 1 MiB bound fetching. Only an isolated action-free call sees raw page text; its bounded
metadata reaches only clarification. Failures retain the URL without a summary. Attachments
use the existing Telegram library, stay opaque, and have declared/actual 20 MiB checks and a
2 GiB aggregate admission cap including the incoming file. No sandbox/dependency changes.
Inbox data stays on the encrypted volume beside orders, outside the clone and the current
weekly originals copy. No automatic deletion or processing is introduced.

**2026-10-20 — The encrypted weekly copy includes the bot inbox.** `homelab-copy` adds the
real `/srv/homelab/frontline/inbox` directory at the stable archive path `frontline/inbox`
when it exists. It uses the same age encryption and checked streaming as originals, rejects
a symlinked inbox, and leaves the planning clone, orders and markers out. The restore procedure
assigns inbox ownership to `homelab-bot`, with directories `0700` and files `0600`.


**2026-10-20 — Mac orchestrators consume the inbox through the existing root SSH account.**
A single isolated, standard-library `homelab-inbox` helper owns the fixed-path list, file and
receipt operations. Existing passwordless root sudo adds no privilege and needs no listener
or grant. Development receives general kept items and its messages; repositories receive only
their addressed messages. Receipts atomically replace metadata, preserving bot ownership and
mode; per-directory advisory locks serialize concurrent receipts without another state file.
Files and directory entries are flushed before success. Multi-id receipts commit independently
and retry idempotently. Descriptor-relative opens reject symlinks throughout the path and
non-regular/multiply linked files; attachments are read only for explicit file streaming.
The bot already renders arbitrary status values, so it needs no change to display read items.

**2026-10-22 — An owner-operated Mac queue runs resumable commands under launchd.**
The owner's corpus labelling and coding eval scripts already checkpoint their own work and
start their own loopback model servers. Keep the queue ignorant of their implementation:
absolute argv, optional cwd, FIFO, explicit play/stop, one process group at a time in any
profile. Scheduled calendar work keeps its eligibility rules and gets the next slot ahead
of queued work. Group termination and a ten-second kill bound release model memory before
another command starts. Battery pause and AC resume each require ten continuous minutes;
a wake assertion covers running work, without overriding lid sleep. One immediate retry
handles a transient command failure; a second retains the failed record and exit code.

A small atomic JSON list under the profile command's existing lock is a deliberate exception
to libraries owning queue machinery: the node's Procrastinate requires Postgres, absent on
the Mac, and Task Spooler only supplies ordering. No new dependency or node service is added.
launchd owns the persistent worker in the private installed copy, so chat, terminal and
profile-switch lifetimes do not own jobs. Per-command local 0600 output logs are explicitly
requested here and retained until removal, an exception to content-free default logging.
An optional progress-file environment variable keeps reporting independent of job internals.
