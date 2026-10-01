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
