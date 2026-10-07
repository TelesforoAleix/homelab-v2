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
