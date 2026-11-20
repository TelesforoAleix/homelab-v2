# Run execution

This document covers how coding work executes: the run API's internals, the `runs` service,
the task store and the stopped host runner kept for rollback, with their provisioning, switch
and rollback steps. Read it before changing `src/homelab/api/runs.py`,
`src/homelab/contracts/run_api.py`, `src/homelab/runs/`, `src/homelab/tasks/`,
`config/executors.yaml` or the task scripts. The wire contract for clients is
[run-api.md](run-api.md); the launcher it depends on is in
[operations.md](operations.md#install-the-fixed-fence-launcher).

## Run API

`POST /v1/runs` creates an application-owned version-one run or returns its existing id for the
same `request_id` and validated request. A changed request returns `409 request_conflict`.
`GET /v1/runs/{id}` reads its executor/hash, attempts, state, times and metrics;
`GET /v1/runs` lists changes by an application-scoped cursor or looks up a request id.
The [run guide](run-api.md) describes the wire format and provisioning.

The API alone mounts `/etc/homelab/run-keys.json` read-only. Each root-owned entry contains an
application, bearer SHA-256, scopes and `repositories`, a map from `owner/name` to default branch.
Write/read/eval calls require their `runs:*` scopes; a key never inherits another key's repository
policy, even within one application. A new submission for an unlisted repository returns 403;
a branch equal to its listed default or `main` returns 409. Another application's run is 404.
No repository policy lives in Postgres. API startup validates the file and
`config/executors.yaml`; malformed data prevents start. The executor table maps each role and
level to an ordered list of Pi configurations or CLI harnesses; the first entry and its hash
are recorded at submission. Dispatch replaces that initial choice with the executor permitted
by usage before each ticket attempt or the first whole-run attempt. `runs` never reads
`homelab_roles`; the table and legacy claim protocol remain for the installed rollback runner.

A ticket write or sealed check becomes one ordinary task on the serial `tasks` queue, deferred through
Procrastinate in the run's transaction. Non-eval tickets choose their `ticket` role/level entries
with the same `usage.choose` policy as slices: only a usage limit permits the next entry, and
no usable entry means `waiting: usage_limit` until Procrastinate's scheduled retry. The ticket
row matches the slice row at all levels. Codex and Claude commands both come from
`slices.command`, through `cli-run`, operating on a plain copy; allowed, unpinned regular files
return through offline data containers. Prepare, fresh clones, pins, check, commit and App push
remain the task path's gates. A failed check retries once from a fresh clone with the check's
output, then escalates. Other execution failures escalate without trying a different executor.
There is no per-ticket review or review feedback. The client reviews the slice before merge.
Pi ticket entries run Pi without usage probes or review. `after` request ids become task dependencies; a
non-`done` predecessor cancels its queued descendants with `predecessor`. Unrelated active runs of the application exclude another
write on that branch. Each run-linked task stores an empty goal; claim resolves the sole copy
in `homelab_runs.goal` and adds assets in memory. Request metadata and queue arguments never
copy that goal. Database triggers synchronize status, attempts and usage changes. Bridge writers
and cursor reads take a transaction lock before locking run/task rows, so revision watermarks
cannot skip a late commit. Unlinked legacy tasks keep their existing queue behavior.

Queued cancellation records `cancelled_by_client` and cancels descendants; cancellation after
claim returns `409 not_resumable`. Ticket messages also return `not_resumable` because that path
cannot resume a conversation. Slice, cut and review requests return
`409 role_not_available` unless `HOMELAB_RUN_ROLES` enables the role (empty by default).
Read mode for tickets/checks or disabled roles and any supplied `limits` return 409 with
`status_reason:role_not_available` and a message that they arrive with the execution service,
leaving no run, task or job. History is recorded while the host keeps its full single-branch
clone. Delivered commits remain empty and final messages null until supplied by an executor.
Goals, assets and final messages are never logged.

Eval marks are `{set, case, executor}`, require `runs:eval`, and validate executor entries
with the catalogue validator. They use exactly that entry without a catalogue lookup or
fallback; a usage limit waits normally. Pi is accepted only for tickets; other roles refuse it
at create. Marked slice/cut/review runs use the same whole-run path as real work, and marked
tickets the same ticket path. Each request names its own executor; clients compose the slice
or cut/ticket chain and its later review. Eval writers deliver only to `eval/` branches;
unmarked writers cannot deliver there. Both push paths retain the private-content check and
record delivered commits normally. Eval diffs are no longer exported or read by `src/`, and
`/v1/tasks/eval` returns 404. Its installed mounts and stopped rollback runner stay untouched.

`check` is an eval-only write role with executor omitted. The platform fetches `base_commit`
and `work.overlay.commit` from the authorized repository into an isolated Git store, transfers
only base objects through the existing data shape into a fresh task copy, checks out the base,
prepares as for a ticket, then imports overlay objects offline and writes selected regular
files. Overlay paths may cover directories;
missing paths, links, submodules, `.git`/`.venv` and linked destination parents fail closed.
The check command runs with no network or logins on the existing launcher `check` shape, with
Git and the environment read-only. It runs no agent, commits nothing and pushes nothing.
Command completion is `done` with `check: {exit_code, tail}` (last 16 KiB, replacement UTF-8).
Setup/launcher failures fail the run. Result data lives in the existing request JSON; the
non-null store executor/hash use an empty selection and its hash, projected as null in GET.
No migration is needed. Whole-run completion writes its task mirror before its authoritative
run state in one transaction, preserving marked conversation states against the legacy bridge.

### Contract usage

`src/homelab/api/contract_usage.py` counts what the run-api readers accept. After a body
validates, `POST /v1/runs`, `POST /v1/runs/{run_id}/messages` and, after its ownership check,
`POST /v1/runs/{run_id}/cancel` add one to `homelab_contract_usage`, keyed by contract
(`run-api`), application, operation, shape and day. `shape` returns `base` for every request
today. The same request's unknown fields, found by walking the raw JSON against the request
models through nested models, lists and optional types (free-form dictionaries such as
`attribution` are not walked), are upserted into `homelab_contract_unknown_fields` as paths
such as `$.work.reviewer` or `$.assets[].note`, keyed by contract, application, operation and
path, with first and last seen times and a count. A name outside `^[A-Za-z0-9_-]{1,64}$` is
recorded as `?`, and at most 32 paths are kept for a request. Refused requests and reads are not
counted. Recording is best effort: a failure logs one warning naming only the exception type,
and the request proceeds. Both tables are created by `schema.sql` with `IF NOT EXISTS`.

`GET /v1/contracts/usage` requires `runs:read` and returns
`{contract, schema, since, shapes, unknown_fields}` for every application over the last 30 days,
today included: shapes ordered by day, application, operation and shape; unknown fields, by last
seen within the window, ordered by application, operation and path. `schema` is the SHA-256
of the exact text `python -m homelab.contracts.export run-api` prints, computed once per
process; the same hash names the document in a `422 invalid_request`.

## Container task runner

The platform image contains `python -m homelab.runs`. Compose defines it as `runs` under the
opt-in `runs` profile, uid 21020 with no capabilities, Docker socket, host helper or published
port. The node's private environment enables that profile for ordinary `homelab.service`
starts. The old host runner is stopped and disabled, retained only for rollback; its scripts
and unit remain installed.

Whole-run work uses a separate Procrastinate `runs` queue, with a library repository lock
and two worker slots across slice, cut and review. Tickets keep their serial `tasks` loop.
`HOMELAB_RUN_ROLES=slice,cut,review` enables submission and dispatch; the API and service both
receive the setting, whose Compose default is empty. Only review requires read mode; cut
accepts write mode to commit failing tests, as well as read mode. Review tries Claude first
(sonnet at every level), then Codex gpt-6.1-sol high.
The node enables both this setting and the Compose profile; the stopped host runner, lifetime
locks and shared-directory prerequisites remain part of rollback and recovery.

The launcher creates `run-<uuid>` work/run folders as uid 21000. No mirror is retained:
each copy clones the existing branch or the remote default tip for a new branch, without
hardlinks/templates/hooks. A repository-scoped App header reaches only the clone container's
environment, without logins; offline checkout/configuration writes happen in a data container.
`cli-run` alone mounts the harness logins. Its optional `read_only` flag protects all of `/work`
while keeping `/run/factory` writable. Read turns return only their final message.
Optional CLI stdin carries contract-bounded goals/assets in a regular-file descriptor,
snapshotted and rendered into an anonymous memory file by the launcher. Prompt content never
enters CLI argv or launcher records; existing two-descriptor callers are unchanged.
Write turns stream a bundle through the launcher into private runner state. Independent Git
checks require exactly `HEAD` and the requested branch at one tip, a valid bundle, descent
from the pinned base, and a base-to-tip diff within allowed paths and outside pinned paths.
An authenticated default-branch recheck and the public-repository deny-list scan precede a
non-force App push. Rewritten remote history fails delivery.

Real executors come from the ordered role/level catalogue; evals use their supplied entry. Codex permits a five-hour window
below 99%; Claude permits below 90% with a reading less than an hour old, or a passed reset.
Weekly windows are recorded with the readings. Claude probes are lazy, at most hourly, after
30 minutes of reading age, with the attempt time persisted even on probe failure. Exhausted
choices become `waiting:usage_limit` with a scheduled Procrastinate retry at the earliest
reset; an unknown reading retries in an hour. Once a harness starts, further turns retain its
executor and session, including after a usage-limit result, and its copy until retention removes it.

Whole-run records own their state independently of the ticket bridge. Question, escalation,
client-step waiting and handover finals become resumable question, escalated, waiting and
done records. Message ids are durably unique per run and queue one additional turn; goals,
assets, messages and final text never enter job arguments or logs. Successful write turns
record delivered tips. On restart the launcher removes this caller's containers first, then
running attempts end `failed:interrupted`. A run is queued once; a second interruption fails
it and cancels queued descendants. The whole-run loop removes failed/cancelled work/run
pairs promptly through `cli-remove`; an hourly sweep removes other ended copies three days
after their last turn ended. It holds the store lock against claims/messages and retries
launcher refusals later, never removing an in-use copy. Copy readiness and `resumable` clear
after removal, while the harness login retains the session. Later messages prepare the
branch again and resume that session; cancellation, deduplication, branch exclusion and
usage-reset gates still apply. Partial preparations are recorded before creation too.
Public deny hits and unreadable deny lists end `failed:push_refused`, without matched content or paths.

The Python runner uses the same task store and Procrastinate transactions directly over the
API's database login. A session advisory lock `(157442, 1)` excludes another `runs`; legacy
claim/recovery CLI processes take the same lock before touching the queue. Startup and each
queue turn read a read-only bind of the host's `system.slice` cgroup parent and refuse a
populated `homelab-task-runner.service`. Missing/unreadable host state fails closed. Binding
the parent observes cgroup creation/removal without systemd or Docker access. A database
session watchdog stops live launcher containers if the lock connection is lost. An
exclusive flock on a read-only host lock-file bind also excludes manual invocations of the
unchanged root task script; provisioning this ephemeral file is a switch prerequisite.

The launcher creates each work folder using the existing `homelab-task` uid, refusing uid 0
and 21020. Its root-controlled work parent prevents the runner replacing the folder between
a no-follow check and a Docker bind. `runs` mounts task work read-only and never writes inside
it. The launcher rejects overlapping shapes or folder lifecycle operations on one work id,
so another launched container cannot swap a bind source during validation. A fixed clone shape uses only that work bind and the existing fence proxy/network; the
launcher validates repository and branch and invokes system Git directly, without a shell,
hooks, credential helpers or login mounts. Prepare, Pi, check and commit use the task
shapes. CLI workers use the existing `cli-run` profile; networkless data containers copy
only regular files into and out of separately launcher-created, uid 21000 task-attempt folders.
No mounted folder belongs to `runs`. Configuration snapshots and private run records belong
to `runs` and are never mounted by the launcher.

Check retries, commits, costs and failure notices use the task machinery.
A sanitized temporary Git directory exposes only the clone's objects and
refs for the unchanged deny-list/canary scan and push, never its config or hooks. Each push
mints a fresh RS256 App JWT with OpenSSL, requests an installation token restricted to one
repository, then uses HTTPS with an explicit URL/refspec and an authorization header held
only in process memory. The independent remote default-branch guard still applies.

Per-service secret copies are read-only; logs retain ids and fixed codes, never credentials
or task content. The healthcheck requires an answering launcher socket. Because Compose binds
the socket file's inode, restart `runs` after restarting the launcher socket, as with Factory's
runner and sessions containers. Provisioning, switch prerequisites and rollback are in
[operations](#runs-service).

## Coding tasks

The active `runs` service executes tickets as described in [Run API](#run-api) and the
[runbook](#runs-service). The following host-script layouts describe the
stopped, installed rollback runner. Its optional review remains available only on that path;
`runs` never reviews a ticket.

`homelab-task-run` runs one ticket against one working copy; see the
[runbook](#coding-tasks-in-a-container). Pi is the agent. It has no sandbox of
its own, and model-generated commands and repository content are untrusted, so the containers
are the boundary. By hand it stops at the check. The [task runner](#task-runner) calls it for
each attempt at a queued task, with a review and the commit step.

| Step | Runs | Network | Token | Read-only in the copy |
|---|---|---|---|---|
| prepare | checks the copy is clean and pins exist; `uv sync --locked`, up to 3 tries | `homelab-packages` only | none | `.git` |
| agent | Pi, ticket on stdin | `homelab-tasks` only | the run's | `.git`, `.venv` |
| check | writes the diff, restores pins, runs the check | none | none | `.git`, `.venv` |
| review | with `--review-purpose`, after a passing check: Pi with `read,grep,ls`, instructions, ticket and diff on stdin | `homelab-tasks` only | its own, 20 requests | the whole copy |
| commit | with `--allow`, after a passing check and review: judges the change, makes one commit | none | none | `.venv` |

Every container is non-root (the working copy's owner, never root), has no capabilities and
`no-new-privileges`, a read-only root with tmpfs `/tmp` and home, limits on memory, CPU and
processes, and a wall-clock limit. It mounts only the working copy, and it uses Docker's `none`
log driver, because the daemon's `json-file` default would keep prompts and code on disk.

The root socket launcher also exposes these five layouts as the `task` profile for root and
uid 21020 (`homelab-runs`). It selects one work-folder identifier under `/srv/homelab/task-work`,
uses that folder's non-root uid/gid, and resolves approved `homelab-task:3` to its immutable id.
Each shape fixes Docker options and the entry command. Bounded arguments and per-shape
allow-listed environment values supply in-container task data; they never select host paths,
images, networks, mounts or Docker flags. Agent/review stdin is a passed descriptor, and
content goes only to the caller's output descriptors. The stopped host runner starts its
containers directly. The [launcher contract and node probe](operations.md#task-launcher-profile)
describe this boundary.

Prepare's only way out is `package-proxy`, a tinyproxy on the internal `homelab-packages` network,
reached through `HTTPS_PROXY`. The proxy is that network's only other member and the only part with
an outside route, its own `homelab-package-egress`. It allows `CONNECT` to port 443 of `pypi.org`,
`files.pythonhosted.org` and `registry.npmjs.org` and refuses everything else, plain HTTP to those
hosts included. Prepare gives uv 120 seconds per download (`UV_HTTP_TIMEOUT`) and runs
`uv sync --locked` up to 3 times, 5 seconds apart, so one slow wheel does not end the run; the third
failure ends it `prepare_failed`. Prepare runs the repository's build backend, so it can still talk to those hosts.

The host never runs git in a working copy or reads its content before the commit step. Those files
are attacker-influenced and the script is root: a planted `core.fsmonitor` or hook would run on the
host. Git runs in the containers, and `.git` stays read-only in the first three steps so hooks,
config and the base commit are as they were. The check step keeps its index and new objects in `/tmp`. The diff is
taken before pins are restored, so it shows an attempt to change them. `.venv` is read-only for
the agent and the check, and the check ignores in-tree bytecode, so the agent cannot replace
the test runner. Bind sources are refused if they are symlinks, which Docker would follow.

The commit step is the only one that can write `.git`, so it runs nothing the copy supplied. `.venv/bin`
leads the image's `PATH` and prepare ran repository code, so it starts `/usr/bin/python3 -I` by full
path with a fixed `PATH`, ignores global and system git config, and turns hooks off. It stages the
tree, excluding `.venv` and `.task-commit-message`, and refuses (exit 3, failing the attempt) when
the change has a path outside `--allow` or inside a pin, an embedded repository, no message or
nothing changed. A path with a component starting `.git`, or a `uv.lock`, also needs an `--allow`
entry that is itself such a path, so `src` does not cover `src/.gitattributes`. An `--allow` entry
covers itself and anything below it. The commit is made with `commit-tree`, which reads no working
tree, on the base commit, by the author given on the command line. The repository's own
configuration has no say in it, and the message is the file the agent wrote.

Pi runs with project approval, extensions, skills, templates, themes, MCP and sessions off,
which also skips the `sessionDir` lookup that happens before project trust. `AGENTS.md` still
loads as context: it is no more trusted than any other file the agent can read. Pi exits 0 after
exhausting its retries, so a run judges the agent by its last stop reason, not its exit code.

The token is issued just before the agent starts, expires a minute after its limit, and is
deleted when the agent stops, even on a signal; a run also clears whatever a dead run left.
Docker cannot isolate containers on one bridge from each other without also cutting them off
from LiteLLM (`enable_icc` works per network), so a lock allows one run at a time. A spent budget
ends a run as `agent_budget_spent`: Pi reports the proxy's refusal as its last error. The
record stays in a root-only folder on the encrypted volume. It holds repository content by
design, and the token is scrubbed from every file in it.

**The review** sits between a passing check and the commit step, within the same attempt. The
reviewer is a second Pi in a hardened container like the others, with the whole working copy
mounted read-only and no `bash`, `edit` or `write`: it cannot change anything or rerun the tests,
and its only token is for the review purpose with a budget of 20 requests. It judges only whether the
diff does what the ticket asks, within its allowed paths, without weakening the check, and treats
the repository, ticket and diff as data. The host passes the diff as bytes and never interprets it;
each part of the prompt is fenced by a fresh random word so none can forge its own end. The host
reads only the last assistant reply: it needs `stopReason` `stop` and a last non-empty line that is
exactly `VERDICT: PASS` or `VERDICT: FIX`, and a Fix needs notes above it (cut to 8 KiB). A Pass goes
on to the commit step. A Fix gives the notes to the worker in the same working copy for one more
agent run (with the budget the first run left), check and review; a second Fix ends the run
`review_rejected`. Anything unclear, a spent budget, a timeout or a non-zero exit ends it
`review_failed`, and a diff over 256 KiB `review_diff_too_large`: an unreviewed commit never
happens when a review was asked for. Every container that talks to a model gets a token of its own
and each is scrubbed from the run folder. **The review is not a security boundary.** It is a model
reading untrusted text; the commit step's rules on allowed paths and pins stay the only enforcement.

### Task runner

`homelab-task-runner` is the stopped and disabled rollback service on the host. When enabled,
it starts containers and pushes, one task at a time. It waits while the data volume
is locked or a run by hand holds the lock, and every 30 seconds when the queue is empty.

```text
claim ── fresh clone ── homelab-task-run (prepare, agent, check, review, commit) ── push ── finish
              └───────────── a new attempt, from a new clone ◄───────────────┘
```

**Configurations.** Pi worker setups are public text under `configurations/pi/`. A run validates
its folder before starting containers, records each file's SHA-256 and the SHA-256 of the compact
JSON encoding of the sorted `(path, sha256)` pairs, the declared Pi version and the inspected
image ID. The image ID pins all containers in that run. The runner clones this repository's
`origin/main` independently of the stack and task checkouts and archives `configurations/` from
its fetched commit; that commit is recorded with the run. Manual runs default to `pi/base` in
the stack checkout and can select another folder with `--configuration`. Explicit purpose and
limit options override configuration defaults; the review keeps its own restricted tools and route.
Prompt files are public and their content is never logged by the host scripts.
An eval-marked queued task uses the selected configuration's purpose instead of the worker
role's purpose; the host runner passes its set, case and configuration mark to the run.
The run refuses any eval purpose without a valid mark, both before execution and when issuing
a token. Eval purpose identifiers are lower-case letters/digits plus `.`, `-`, `_`, start with
a letter/digit, and have 1–96 characters each; the other purpose rule is unchanged.
The token format, reviewer route, 100-request default and configuration minutes are unchanged.

**The store.** Postgres has no host port, so the runner uses `docker exec homelab-worker-1 python -m
homelab.tasks.claim` and `homelab.tasks.finish`, as the image-cost timer does. Claiming calls
Procrastinate's own `fetch_job` in the same transaction as the task's `running`, so a job goes to
one caller and the two states cannot differ. Finishing moves the job to `succeeded` or `failed`
with the task. Every `finish` event can be repeated; the runner repeats one until it lands. On
start the runner removes containers and copies a dead runner left and calls `claim --recover`,
which marks its attempt `runner_died` and retries its job, so the task is `queued` again.

**Working copies** are under `/srv/homelab/task-work/<task id>/`, owned by the unprivileged system
user `homelab-task`, who runs the clone. Each attempt clones the slice branch's tip over HTTPS with
no template, so an attempt inherits nothing from the last. The folder is removed when the task
ends. `base_commit` is kept as a record and does not choose the start.

**Attempts.** Routes come from `homelab_roles`, role `worker`, and an attempt uses one route from
start to end. A failed check gets one more run on the same route, in a new clone, with the end of
the check's output in the ticket. Any other failure, or a second failed check, moves to the next
route; when they are used up the task ends `escalated` with the last reason. A `cli` route is
skipped with a logged reason. Each attempt writes an attempts row and a `lead` trace with its
steps, tokens and seconds. `cost_usd` is what the proxy billed. `homelab-task-run` clears the cost folder at the start of a
run and reads and removes each model container's file after it ends. A container's cost is the sum
of its lines, and null if there is no file or any line is null; a partial sum is never a cost. The
record's `cost_usd` is the worker's total over its runs, null if any is null, and each review has its
own. An unknown cost stays empty in the traces, and `cache_write` tokens stay in `run.json`.

The runner always asks for a review. Its route is the first usable `litellm` route of role
`ticket-reviewer` (`claim` returns it as `review_routes`); a task with none ends `escalated`,
`no_usable_review_route`, before any attempt. A review rejected twice gets the same retry as a failed
check, in a new clone with the notes in the ticket under their own heading; `review_failed` and
`review_diff_too_large` go straight to the next route. Each review becomes one `review` trace of role
`ticket-reviewer`, written in the same transaction as the attempt's close and only when that close
changed a row, so a repeated event writes nothing twice.

**The phone.** When a task ends `escalated` or `failed`, after `end_task` is recorded, the runner
pipes one line to `homelab-queue-notice --task`: the task id, its slice, status and reason, with the
prefix "Task: ". It carries no goal, title, notes or diff. A notice that fails is logged as one line
and changes nothing.

**The push** happens only here, as root, after the commit step. `git` runs against the copy's `.git`
from outside the copy, which only git itself has written since the clone, with hooks and global
config off. The URL and refspec (`<commit>:refs/heads/<branch>`) are given on the command line, and
the key `/root/.ssh/task-push-<repository name>` through `GIT_SSH_COMMAND`, checking GitHub's host
keys from `/etc/homelab-task/github_known_hosts`. It never forces. It refuses `main` and the
default branch, which it asks the remote for, and an unknown default counts as refused. A task on
such a branch fails before any attempt. No key is ever mounted in a container. A push git rejects
because the branch moved reruns the task once from the new tip, then ends `escalated`. A missing key
or any other push failure ends it `escalated` at once, and its `diff.patch` stays in the run folder.

**Endings.** `done` defers each dependent with nothing else unmet. `escalated` means the attempts
ran and a person is needed; `failed` means the task could not start (a refused repository or
branch) or the runner hit a fault. After either, dependents stay queued with no job and the slice
stops: a claim discards the job of any other task in it, leaving that task `queued`.

### Task store

A planner outside this repository cuts work into ordered tasks and hands them over as a file.
`python -m homelab.tasks.submit` reads one from stdin, in any container built from this image:

```json
{
  "slice": "free text naming the piece of work",
  "repository": "owner/name",
  "branch": "main",
  "base_commit": "40 to 64 lowercase hex digits",
  "tasks": [
    {
      "position": 1,
      "depends_on": [],
      "title": "Short title",
      "goal": "What the task must achieve",
      "allowed_files": ["src/example.py"],
      "check_command": "uv run pytest",
      "pinned_paths": ["tests/test_example.py"],
      "priority": 0,
      "severity": "normal"
    }
  ]
}
```

`depends_on`, `pinned_paths`, `priority` and `severity` are optional, with the defaults shown;
every other field is required and an unknown field is refused. `position` is a whole number from 1,
and orders the slice; only `depends_on` gates a task. It lists positions of the same slice, in
this file or in one submitted earlier for the same repository and branch. `allowed_files` is not
empty. It and `pinned_paths` are repository paths, under `homelab-task-run`'s rule for a pin.
`priority` is an integer, higher first, and becomes the job's Procrastinate priority. `severity`
is free text, for the planner and later stages.

The file is validated whole: types, repeated positions, and cycles. Then one transaction writes
the tasks and defers one job for each task whose dependencies are all `done`, through
Procrastinate's external-connection option, so a refused or failed submission leaves neither tasks
nor jobs. An unknown dependency is refused, and so is a slice and position that already exist. On
success it prints the new ids as a JSON list in position order. A refusal exits 1 and names the
field or position on stderr, never its content.

The queue is `tasks`, and a job carries only the task id. `run_coding_task` is registered so the
job is valid and raises if run. The `worker` service serves only `local` and `default` and has no
Docker access; the [task runner](#task-runner) on the host is the consumer. A task waiting for a
dependency has a row and no job.

`python -m homelab.jobs.schema`, the worker's start step, applies `src/homelab/tasks/schema.sql`
after Procrastinate's schema. Every statement is idempotent, and a failure stops the worker
starting. The coding-task and run records use five tables:

| Table | One row per | Holds |
|---|---|---|
| `homelab_runs` | application-owned run | request hash, goal, metadata, executor/hash, task link, status, change revision |
| `homelab_roles` | legacy role | rollback host runner routes; unused by `runs` |
| `homelab_tasks` | task | the file's fields, status, reason, timestamps |
| `homelab_task_attempts` | attempt of a task | number, role, route, status, why it ended, run id, check exit code |
| `homelab_task_traces` | model-using job | task, attempt, role, route, kind, steps, tokens, cost, seconds, result |

A route is `{"kind": "litellm", "purpose": ...}` or `{"kind": "cli", "tool": ..., "model": ...}`.
Start inserts `worker`, `ticket-reviewer` and `side-job` if they are missing, each with one route
to `task-worker`, and leaves existing rows alone. Only the rollback host runner reads these
routes; `runs` selects executors from `config/executors.yaml`. The legacy table is edited
directly; a trigger keeps `updated_at` true. Attempts and traces copy their role and route,
so history survives an edit. Runs compare in plain SQL:

```sql
SELECT role, route, kind, count(*), sum(output_tokens), sum(cost_usd), avg(seconds)
FROM homelab_task_traces GROUP BY role, route, kind;
```

## Operating the runs service

### Runs service

`runs` is the container version of the task runner, in the platform image, running as
`21020:21020`. Its command is `python -m homelab.runs`. It is defined under the Compose
`runs` profile, with `restart: "no"`; the node's private environment sets `COMPOSE_PROFILES=runs`,
so ordinary `homelab.service` starts include it. It is the execution service. The old `homelab-task-runner.service` is stopped and disabled, retained only
as the rollback until its removal. The host's two task scripts and runner unit remain installed.

**Ticket path.** `config/executors.yaml` selects real ticket workers; eval marks select an exact entry.
Its `ticket` row matches the `slice` row at every level; the order per level is in that file
and in [run-api.md](run-api.md#executors-and-current-capability). Tickets are always available on the serial `tasks` queue, independently of
`HOMELAB_RUN_ROLES`. Both harness workers use `slices.command` and the launcher `cli-run`
profile, passing the intact ticket in the existing stdin envelope's `work.ticket` field so
contract-sized goals and assets fit; no Codex-specific command wrapper remains. The first
usable entry runs; fallback is only for a usage limit, using the slice thresholds and the shared hourly Claude probe cache.
With none usable the run waits `usage_limit`, records `resume_at`, and Procrastinate schedules
the task's retry at the earliest reset. Unknown readings wait an hour.

A ticket keeps its fresh clone, prepare, pins, allowed-file import, check, commit and push.
A failed check gets one retry from a fresh clone with its output as feedback, then escalates;
other execution failures escalate without trying the next entry. The existing one rerun after
a rejected push remains. There is no review step, review purpose, reviewer route or review
feedback in this service. The client reviews the slice before merge. `homelab_roles` is never
read by `runs`; it and the installed host runner remain for rollback. No schema migration is
needed. Pi ticket entries run their named configuration. Eval tickets use the supplied executor
and the same pipeline, delivering only to `eval/` branches after the normal private-content check.

Private ticket files live under `/srv/homelab/runs/<run id>/`, owned by uid 21020, mode 0700:

| File | Holds |
|---|---|
| `run.json` | Run id, base/working copy, purpose, pins, check command and exit code, `check_log`, commit, ending and times. Pi retains its configuration manifest/hash/source commit, `steps`, `tokens`, `compactions`, `retries`, `end_event`, `last_stop` and measured `cost_usd`. Harness runs add `executor`, hash that selection in `configuration_hash`, report available harness requests/tokens/cost and `cli_ignored_changes`; unknown cost is absent. No `review_purpose`, `reviews` or `review_notes`. |
| `prepare.log` | Dependency installation |
| `diff.patch`, `check.log` | Diff before pin restoration, and check output |
| `events.jsonl`, `agent.log` | Pi events/stderr, only for Pi workers |
| `commit.log` | Commit refusal details, when present |

Each retry has a fresh run folder. There are no review files or review fix-round files.
Harness stdout/stderr and transfer files are removed after each turn. The plain CLI copy is
removed after the turn, and the task copy after the task finishes or waits. Only allowed,
unpinned regular files and `.task-commit-message` return; links, deletions, `.git` and `.venv`
do not. The commit-message file is metadata and need not be listed in `allowed_files`.
Run and attempt executor/hash fields reflect the worker that ran, including fallback.
After merge, **Restart the platform on the merged commit**. The client verifies one eval slice
and one sealed check on a public repository; this implementation does not run those node steps.

**Whole-slice, cut and review path.** The node's private environment sets
`HOMELAB_RUN_ROLES=slice,cut,review`, passed independently to the API and `runs`. Review
requires `mode: read`; slice and cut support either mode. A write cut can commit the ticket
flow’s failing tests. Explicit `limits` remain unsupported.

These roles use Procrastinate's separate `runs` queue, two CLI worker slots and repository
locks, across all three roles. Tickets retain their independent one-at-a-time loop. Ordered
dependencies and cancellation remain application-owned. Real executor choice comes from the
role/level list in `config/executors.yaml`; eval marks supply an exact executor with no fallback.

| Role | Light | Standard | Strong |
|---|---|---|---|
| `review` | Claude sonnet, then Codex gpt-6.1-sol high | Claude sonnet, then Codex gpt-6.1-sol high | Claude sonnet, then Codex gpt-6.1-sol high |

Claude uses its default effort. Codex's launcher `usage` reading
permits five-hour usage below 99%. Claude permits below 90% from a reading less than an hour
old, or after the reset; its short probe is lazy, only when needed, after 30 minutes of age,
and at most hourly. Weekly windows accompany both readings. Probe attempt times survive a
restart, including failures. No usable choice means `waiting: usage_limit` with the earliest
reset as `resume_at` and a scheduled automatic retry. Missing windows fail closed and retry
in an hour. A turn ending at a usage limit waits for its executor's reset in the same session.

The launcher creates each `run-<uuid>` work/run pair under the existing Factory roots, owned
by uid 21000; `runs` mounts neither tree. There is no retained mirror. A fixed clone, without
hardlinks, uses the existing branch, or the current remote default tip for a new branch.
Its one-repository App token is an environment-only Git authorization header, scoped to
GitHub HTTPS with redirects disabled. Neither URL, argv nor copy configuration stores it;
the clone has no harness logins. Offline data containers set the branch, fixed committer and
final-message schema. The harness gets its configured executor and the work policy/assets.
`cli-run` alone has the logins. `read_only: true` makes the complete `/work` bind read-only;
the run folder `/run/factory` stays writable for final-message output. Absent or false retains
today's writable copy. Read turns never bundle or push.

Whole-run prompts use `cli-run`'s optional `stdin: true`, with a third readable regular-file
descriptor containing JSON `{goal, assets, work?}`. The launcher snapshots and validates it:
at most 200,000 goal characters, 32 assets totalling at most 200,000 UTF-8 JSON bytes, and
4 MiB for the complete envelope. NULs, malformed JSON and excess limits are refused before
container creation. It renders the goal, work policy and assets into an anonymous memory
file used only as Docker stdin. Codex reads `-`; Claude reads its piped prompt. No prompt
enters request argv, launcher records or logs. Existing callers without the flag retain
their two-descriptor, no-stdin behavior. The socket request limit remains 64 KiB.

Write turns export through `bundle` with `stream: true`, which returns Git bundle bytes on
stdout instead of writing `branch.bundle`. Existing bundle callers keep their file output.
`runs` verifies outside the copy: exactly `HEAD` and the branch at the same tip, successful
`git bundle verify`, and descent from `base_commit`. Every changed path in the base-to-tip
diff must be within `allowed_files` (null means unrestricted, empty means none) and outside
`pinned_paths`, including deletions and rename sources. Refusal ends
`failed: outside_allowed_files` before push. Delivery rechecks the authenticated remote
default, scans every outgoing public commit with the deny list/canary, and pushes with a
fresh App token and no force. That token also carries workflows-write only when the
base-to-tip diff adds, changes or deletes a path under `.github/workflows/`; every other
token, and every other caller, keeps contents-write/metadata-read. Deny hits, an unreadable deny list and rewritten branches
end `failed: push_refused`, without matched content, filenames or lines in the reason.

Final kinds map to resumable states: `handover` to `done`, `question` to `question`,
`escalation` to `escalated`, and `waiting` to `waiting: client_step`. A message resumes the
same executor/session and retained copy and adds one attempt. `message_id` is unique per
run, so retries
add no extra turn. A completed run cannot resume onto a branch occupied by its successor.
Usage waits resume automatically; client messages do not bypass that reset. Delivered tips
are recorded in `commits`; final text is stored only in the application-owned run record.
Transient CLI captures are removed with each private turn folder. Retained copies belong
to the harness account, never to `runs`. The whole-run loop asks the launcher’s
`cli-remove` to remove both work and run folders immediately on failure or cancellation
(and retries on each queue poll), and sweeps other end states hourly, once three days have
elapsed since the last turn ended. Queued/running copies are excluded; the store lock
excludes a racing claim/message, and a launcher refusal means try later. Incomplete
preparations are tracked for cleanup too. Removing a copy sets `resumable: false` and
clears copy readiness, while keeping the harness session in its login. A later message to
an uncancelled conversation queues `prepare` again from the branch, then resumes that
session with its original executor. Cancellation still prevents messages. Usage-reset
scheduling and message deduplication remain in force even after copy removal.

At startup, launcher recovery removes this caller's task, CLI, bundle and usage containers
before database recovery. Each interrupted attempt ends `failed: interrupted`; the run queues
once. A second interruption fails it permanently and cancels queued descendants. The lifetime
runner locks and watchdog remain in force while workers stop. No new uid, network, host bind,
Docker socket access or login access for `runs` is added.

For tickets, the queue/store, one retry with check output, check, commit, costs and
attempt records use the task machinery. CLI workers keep the
existing launcher `cli-run` profile and their 20-minute turn limit. Networkless `data` task
containers export/import regular files through pipes; only allowed, unpinned regular files
return. Deletions, links, Git metadata and `.venv` do not return. The launcher creates the
CLI's `ticket-<id>-<attempt>` work/run folders as uid 21000 and removes them after a turn.
Neither those folders nor task copies ever belong to `runs`.
Whole-run CLI turns retain the profile's absent deadline, with prompt cancellation on
shutdown; clone, probe and ticket/check deadlines remain separate.

`runs` asks the launcher to create each fresh task folder as the existing `homelab-task`
account (neither root nor uid 21020). The `task-work` parent must remain root-owned 0755.
The only work mount in `runs` is read-only. The launcher rejects overlapping shapes and
folder lifecycle operations on one work id, including CLI transfers, so another container
cannot swap a bind source during validation. Clone happens in a fixed `task` clone shape:
only `/work` is bound, no logins, on the existing `homelab-fence` network through
`http://cli-proxy:8888`. The launcher validates repository and branch and invokes system Git
without a shell, using `--template= --no-tags --single-branch --branch`, an explicit GitHub
HTTPS URL and `/work`; hooks and credential helpers are off. The other five shapes retain
their networks, mounts, limits and commands. The launcher also offers authorized
`task-create`/`task-remove`, `cli-create`/`cli-remove`, `task-image`, `task-recover` and `ping`
operations. Task removal is fd-based and does not follow links; Factory provisioning/data
access is restricted to `ticket-<id>-<attempt>` or `run-<uuid>` names and requires both
`task` and `cli-run` permission.

For pushes, a short-lived RS256 App JWT is signed with OpenSSL. Each push requests a fresh
installation token restricted to that one repository with contents-write/metadata-read
permissions (plus workflows-write for a delivery whose diff touches `.github/workflows/`). Git receives the authorization header only through its environment. Its URL
and refspec are explicit, credential helpers/hooks are off, and a temporary Git directory
exposes only the protected clone's objects/refs, never its config. The remote default branch
and `main` are refused. The existing deny-list/canary scan runs before each push; it still
fails closed for unknown visibility or an unreadable list. Marked writes push only to `eval/`
branches; unmarked writes cannot push there. Both paths enforce this before delivery.
See [GitHub's JWT contract](https://docs.github.com/en/apps/creating-github-apps/authenticating-with-a-github-app/generating-a-json-web-token-jwt-for-a-github-app)
and [installation-token parameters](https://docs.github.com/en/rest/apps/apps#create-an-installation-access-token-for-an-app).
No Python JWT dependency is added.

Private logs and run records live under `/srv/homelab/runs`, owned by uid 21020, mode 0700;
eval tickets use the same attempt folders as real tickets. The active runner no longer exports
eval diffs, and the API no longer reads them. Old mounts stay installed and unused.
The `runs` journal contains ids and fixed reasons only. Failure notices use the same bounded
`Task: <id> <slice> <status> <reason>` text, sent directly to the existing allowlisted chats.
Notice failures do not change task status; no host/root helper is called.

`runs` holds Postgres session advisory lock `(157442, 1)` for its lifetime; legacy claim and
recovery commands take the same lock for their process. Before taking the lock, again after
acquiring it, and before each queue turn, `runs` reads the host service's `cgroup.events` from
a read-only bind of `/sys/fs/cgroup/system.slice`. A populated
`homelab-task-runner.service` refuses startup with `legacy_runner_active`; unreadable/missing
host state fails closed. The parent bind observes service recreation, unlike a file bind.
If the old service starts later its claims cannot acquire the database lock. A session
watchdog stops live containers on database connection loss; startup cleans up the caller's
recorded launcher containers before recovering queue claims. Neither runner may bypass this
check by unsetting or replacing the cgroup mount.

The service mounts these paths, with `create_host_path: false` throughout:

| Host path | Container path | Access / preparation |
|---|---|---|
| `/run/homelab-launcher.sock` | same | Socket; supplementary group from `HOMELAB_LAUNCH_GID` |
| `/sys/fs/cgroup/system.slice` | `/run/host-services` | Read-only service state; no systemd API |
| `/run/lock` | `/run/host-locks` | Read-only parent of the unchanged manual-run flock |
| `/etc/homelab/runs` | `/run/secrets` | Read-only per-service credential copies |
| `/srv/homelab/task-work` | same | Read-only, root-controlled parent, task-owned children |
| `/srv/homelab/runs` | same | New private state, uid 21020, 0700 |
| `${HOMELAB_TASK_TOKENS_PATH:-/srv/homelab/task-tokens}` | `/srv/homelab/task-tokens` | Token files; switch preparation below |
| `${HOMELAB_TASK_COSTS_PATH:-/srv/homelab/task-costs}` | `/srv/homelab/task-costs` | Cost files; switch preparation below |
| `${HOMELAB_TASK_RUNS_PATH:-/srv/homelab/task-runs}/eval-diffs` | `/srv/homelab/runs/eval-diffs` | Legacy mount, unused by active runner/API; retained for rollback |

The orchestrator creates `/etc/homelab/runs` as uid/gid 21020, mode 0700. Every file below
is owned by uid/gid 21020, mode 0400; never commit their values or bake them into an image.

| File in that directory | Source / purpose |
|---|---|
| `runs-app.pem` | Copy of `/etc/homelab/runs-app.pem`; App signing key |
| `runs-app.json` | Copy of `/etc/homelab/runs-app.json`, exactly `app_id` and `installation_id` integer fields |
| `deny-list` | Read-only copy of `/etc/homelab/deny-list`, including its current entries; code adds the canary |
| `bot-token` | Per-service copy of the existing notifier's Telegram token, supplied by the orchestrator |
| `bot-allowlist` | Copy of `/etc/homelab-telegram-bot/allowlist`, the same notice recipients |

A refreshed deny list requires refreshing its service copy before any run. Obtain the bot
token from the owner's provisioned credential source; do not put it in a command argument,
environment file or journal. The App's original root-only key remains 0600. App ids are
provisioned on the node, never in repository configuration. Set `HOMELAB_LAUNCH_GID` in the
node's existing `.env` to `getent group homelab-launch`'s gid; the closed default 65534 gives
no launcher access. Supplementary group 10001 supplies API/gateway shared-file access.
The database login comes from the same `HOMELAB_DB_PASSWORD` interpolation as the API.

**After merge and owner review, installation only:**

1. In `/srv/homelab/homelab-v2`, run `git pull --ff-only` by hand. Wait for launcher
   clients to be idle. Save the installed launcher, profile metadata and launcher unit for
   rollback. Show each diff against the committed `node/` file before
   installing. Stop `homelab-launcher.service` before copying the launcher and metadata.
   Parse the Python/JSON and run `systemd-analyze verify` on the launcher unit
   and socket. Install root:root, 0755 for the script and 0644 for metadata/unit.
2. The launcher sandbox needs `/srv/homelab/task-work` in `ReadWritePaths` so it can create
   task-owned folders. Keep every other sandbox setting. Run `systemctl daemon-reload`,
   stop `homelab-launcher.service`, then restart `homelab-launcher.socket`. Restart Factory's
   runner and sessions containers with their existing deployment commands so their file
   binds see the new socket inode.
3. Create the private state directory and per-service files listed above. Record the launcher
   group gid in the private `.env`. Do not change token, cost or eval-export permissions
   while the old runner is active; they are switch prerequisites.
4. With the checkout updated, run `sudo systemctl restart homelab.service`. Verify that
   `runs` has no running container.
   Validate Compose with the profile off and on. Build the optional service explicitly:
   `sudo docker compose --profile runs build runs`.
5. Run `sudo python3 tests/launcher_task_probe.py` (the original boundary checks, ownership/clone
   checks and both writable/read-only CLI probes), and `sudo homelab-check-fence --launcher`.
   Require `PASS: cli-run without read_only runs with a writable /work mount`,
   `PASS: read-only cli-run runs with a read-only /work mount; run folder stays writable`, and
   `PASS: cli-run accepts a bounded prompt descriptor without a model call`.
   The CLI probe runs `codex exec --help` and checks mounts from the installed launcher’s
   arguments; it makes no model call. Also run the `--private` probe described below.
   The ownership probe
   creates and removes only its random scratch folder, clones the public platform `main`
   through the fixed shape and makes no model call or push.
6. With `systemctl is-active homelab-task-runner.service` reporting `active`, run
   `sudo docker compose --profile runs run --rm --no-deps runs`. Require exit 2 and
   `runner refused reason=legacy_runner_active`, with no recovery or claim. Verify again
   that no `runs` container is running. Leave the old runner enabled and active.

**Switch prerequisites:** token and cost directories need owner 21020, group 10001,
mode 0750 and 0770 respectively. Final diff exports need owner 21020, group 10001,
mode 0750 (files 0640). The lock is root:10001, mode 0640. `runs` holds an exclusive
flock through its read-only bind; a missing or unreadable lock fails closed. The tmpfiles
rule below recreates it at boot without truncating or replacing an existing lock inode.
These prerequisites were checked as part of the completed switch. The steps remain useful when
reconstructing the node or preparing a future rollback; the live profile and role settings are on.

### Runs rehearsal on the node

After merging, the orchestrator runs this procedure as root in the merged checkout. It
makes paid light-level turns, creates only random `rehearsal/` branches in the public
platform repository and private Factory repository, and deletes those branches with the
platform App’s repository-scoped token. Keep other submissions idle for the rehearsal.
The key file contains the raw client key and must already be root-only with `runs:write`,
`runs:read`, `runs:eval` and the repository policy described above. The script reads that
file; neither commands nor output contain the key, goals, finals or session IDs.

Meet the existing switch prerequisites above first, including the shared-directory modes
and the live flock inode. No secret refresh or host-file installation is part of this
rehearsal. In a root Bash shell run these commands. The roles are command-line overrides
only; neither `HOMELAB_RUN_ROLES` nor `COMPOSE_PROFILES` is written to `.env`.

```bash
sudo -i
cd /srv/homelab/homelab-v2
set +x
set -e
# Pull by hand, then build the merged application images through the installed unit.
git pull --ff-only
systemctl restart homelab.service
export HOMELAB_REHEARSAL_API_URL=http://127.0.0.1:8000
read -rp 'Root-only client key file path: ' HOMELAB_REHEARSAL_KEY_FILE
export HOMELAB_REHEARSAL_KEY_FILE
export HOMELAB_REHEARSAL_POSTGRES_CONTAINER="$(docker compose ps --format '{{.Name}}' postgres)"
export HOMELAB_REHEARSAL_DATABASE=homelab
export HOMELAB_REHEARSAL_DATABASE_USER=homelab

restore_runner() {
    rehearsal_status=$?
    trap - EXIT
    # Stop and verify runs before allowing the host runner to start.
    COMPOSE_PROFILES= docker compose --profile runs stop runs
    test -z "$(COMPOSE_PROFILES= docker compose --profile runs ps --status running -q runs)"
    HOMELAB_RUN_ROLES= COMPOSE_PROFILES= docker compose up -d --wait --wait-timeout 120 --no-deps --force-recreate api
    systemctl start homelab-task-runner.service
    test -z "$(COMPOSE_PROFILES= docker compose --profile runs ps --status running -q runs)"
    systemctl is-active --quiet homelab-task-runner.service
    unset HOMELAB_REHEARSAL_KEY_FILE HOMELAB_REHEARSAL_RUNS_CONTAINER
    exit "$rehearsal_status"
}
trap restore_runner EXIT
systemctl stop homelab-task-runner.service
if systemctl is-active --quiet homelab-task-runner.service; then exit 1; fi
HOMELAB_RUN_ROLES=slice,cut,review COMPOSE_PROFILES= docker compose --profile runs up -d --build --wait --wait-timeout 120 --no-deps --force-recreate api runs
export HOMELAB_REHEARSAL_RUNS_CONTAINER="$(docker compose --profile runs ps --format '{{.Name}}' runs)"
python3 tests/runs_rehearsal.py
# The EXIT trap restores the API without roles, stops runs and starts the old runner,
# on both success and failure. Keep this exit when using an interactive root shell.
exit
```

Require eight `PASS:` lines and no `FAIL:`. Checks cover a single pushed slice commit,
unchanged read-review tip, question/message idempotency with exactly two attempts and the
same saved session, a private write cut with a failing test, allowed-path refusal, public
canary refusal, ordinary ticket delivery and eval branch delivery, then
cleanup. Session equality is verified with a read-only `psql` transaction through
`HOMELAB_REHEARSAL_POSTGRES_CONTAINER`, selecting only that run’s `session_id`; the value
never enters printed output. Cleanup separately reads only the numeric task IDs for this
invocation’s ticket runs to check their `/srv/homelab/task-work` folders and containers. Database/user and the `runs` container are settings
from the environment variables above. App credentials remain in `runs`; its token helper’s
stdout is captured privately. No token is supplied in argv.

The script stops at its first failed check and still tries all cleanup. Cleanup cancels
its own runs so the loop removes their work/run pairs immediately; if necessary it stops
only containers mounted on those exact random work IDs, waits for the service to settle,
and checks that no associated folder or container remains. It attempts every created
branch deletion even when another cleanup step fails. Ticket work copies are removed by
the existing task path; eval records retain the existing task retention.
Any cleanup failure prints `FAIL:` and returns nonzero. Investigate it before a permanent
switch; the restore commands still leave roles disabled and the old runner active. The
script never changes enablement policy. If restoration itself fails, keep the old runner
stopped until `runs` is confirmed stopped; repeat the restoration commands manually.

### Switch to runs

The switch is done: `runs` and the roles are enabled, the old runner is stopped and disabled,
and Factory sends all slice work through the run API. The steps below remain the procedure for
restoring that configuration on a rebuilt node (steps 4 and 5) and for rolling back (step 8).
Run them as root, in order; stop on any failed check and resolve it before continuing, and keep
submissions idle throughout.

1. Check that no Factory turn or fence run is running, then record Factory's current release
   and check that its image is available for rollback:

   ```bash
   test -z "$(docker ps -q --filter name=^/factory-session-turn-)"
   test -z "$(docker ps -q --filter name=^/homelab-fence-run-)"
   python3 -c 'import json; print(json.load(open("/var/lib/homelab-deployer/factory.json"))["current"])' > /root/factory-before-runs-switch
   grep -Eqx '[0-9a-f]{40}' /root/factory-before-runs-switch
   docker image inspect "homelab-deployer-factory:$(cat /root/factory-before-runs-switch)" > /dev/null
   ```

2. Run [the rehearsal](#runs-rehearsal-on-the-node) unchanged. Require all eight checks,
   including cleanup, to print PASS; its trap restores the old runner.
3. In a root Bash shell, stop and disable the old runner. Require an absent or unpopulated
   cgroup and no running task containers:

   ```bash
   cd /srv/homelab/homelab-v2
   systemctl stop homelab-task-runner.service
   systemctl disable homelab-task-runner.service
   ! systemctl is-active --quiet homelab-task-runner.service
   ! systemctl is-enabled --quiet homelab-task-runner.service
   cg=/sys/fs/cgroup/system.slice/homelab-task-runner.service/cgroup.events
   test ! -e "$cg" || grep -qx 'populated 0' "$cg"
   test -z "$(docker ps -q --filter name=homelab-task-)"
   ```

4. Review the installed-file diff, then install and apply the boot rule. A diff exit of 1
   means differences; inspect them before continuing. Check the existing lock's inode and
   permissions remain correct:

   ```bash
   if test -e /etc/tmpfiles.d/homelab-task-run.conf; then
       diff -u /etc/tmpfiles.d/homelab-task-run.conf node/etc/tmpfiles.d/homelab-task-run.conf || test "$?" -eq 1
   else
       diff -u /dev/null node/etc/tmpfiles.d/homelab-task-run.conf || test "$?" -eq 1
   fi
   lock_inode=$(stat -c '%i' /run/lock/homelab-task-run.lock)
   install -d -m 0755 -o root -g root /etc/tmpfiles.d
   install -m 0644 -o root -g root node/etc/tmpfiles.d/homelab-task-run.conf /etc/tmpfiles.d/homelab-task-run.conf
   systemd-tmpfiles --create /etc/tmpfiles.d/homelab-task-run.conf
   test "$(stat -c '%i' /run/lock/homelab-task-run.lock)" = "$lock_inode"
   test "$(stat -c '%u:%g %a' /run/lock/homelab-task-run.lock)" = '0:10001 640'
   unset lock_inode
   ```

5. Back up the private environment file and append the two settings. Require neither setting
   already exists, so rollback removes only these lines. Restart the platform and check the
   service and the API's actual role parser (the API has no HTTP role-list endpoint):

   ```bash
   cd /srv/homelab/homelab-v2
   ! grep -Eq '^[[:space:]]*(export[[:space:]]+)?(COMPOSE_PROFILES|HOMELAB_RUN_ROLES)=' .env
   test ! -e .env.before-runs-switch
   cp -p .env .env.before-runs-switch
   printf '\nCOMPOSE_PROFILES=runs\nHOMELAB_RUN_ROLES=slice,cut,review\n' >> .env
   systemctl restart homelab.service
   docker compose up -d --wait --wait-timeout 120 runs
   test "$(docker inspect --format '{{.State.Status}} {{.State.Health.Status}}' "$(docker compose ps -q runs)")" = 'running healthy'
   docker compose exec -T api python -c 'from homelab.tasks.runs import enabled_roles; assert set(enabled_roles()) == {"slice", "cut", "review"}; print("API roles: slice, cut, review")'
   ```

6. Install Factory's new image through its deployer using Factory's documented command.
   Check the deployer reports the intended image running and healthy.
7. Record the gate start before Factory starts either delivery:

   ```bash
   date -u +%FT%TZ > /root/runs-switch-gate-start
   ```

   Have Factory deliver one whole-slice slice and one ticket slice through the platform,
   then inspect the runs created since the gate started:

   ```bash
   cd /srv/homelab/homelab-v2
   docker compose exec -T postgres psql -U homelab -d homelab -At -c "SELECT id, request->>'role', status, coalesce(status_reason, ''), cardinality(commits) FROM homelab_runs WHERE created_at >= '$(cat /root/runs-switch-gate-start)' ORDER BY created_at"
   ```

   Require every row to be `done` with an empty reason. The whole-slice slice must have a
   `slice` row with one or more commits; the ticket slice must have a `cut` row and `ticket`
   rows. A ticket run records zero commits even when it pushes, so do not require commits in
   those rows. Check that Factory merged both slices' PRs. Investigate any failure before
   accepting the switch.
8. If rollback is needed, keep submissions idle and let active work finish. In a root Bash
   shell remove the two exact lines and restart the platform, stop `runs`, then restore the
   old runner. Then restore Factory with the terminal command below:

   ```bash
   cd /srv/homelab/homelab-v2
   sed -i '/^COMPOSE_PROFILES=runs$/d; /^HOMELAB_RUN_ROLES=slice,cut,review$/d' .env
   ! grep -Eq '^(COMPOSE_PROFILES|HOMELAB_RUN_ROLES)=' .env
   systemctl restart homelab.service
   COMPOSE_PROFILES= docker compose --profile runs stop runs
   test -z "$(COMPOSE_PROFILES= docker compose --profile runs ps --status running -q runs)"
   systemctl enable --now homelab-task-runner.service
   systemctl is-enabled --quiet homelab-task-runner.service
   systemctl is-active --quiet homelab-task-runner.service
   ```

   In a root terminal (connect with `ssh -t`; the deployer requires a terminal), roll back
   Factory and check that its current release matches the recorded release. The deployer
   prints one JSON result and exits nonzero on failure:

   ```bash
   homelab-deployer rollback factory
   test "$(python3 -c 'import json; print(json.load(open("/var/lib/homelab-deployer/factory.json"))["current"])')" = "$(cat /root/factory-before-runs-switch)"
   ```

   The switch prerequisite ownership and modes may stay: the old runner runs as root. Preserve
   the backup and run records. Do not start both runners together.

The private clone probe needs an existing private repository/branch covered by the platform
App and a fresh installation token restricted to that repository. Supply the token in a
root shell with tracing off and silent input, never a command argument, env file or journal:

```bash
sudo -i
cd /srv/homelab/homelab-v2
set +x
export HOMELAB_PROBE_REPOSITORY='<private owner/repository>'
export HOMELAB_PROBE_BRANCH='<existing branch>'
read -rsp 'Repository-scoped installation token: ' HOMELAB_PROBE_TOKEN; echo
export HOMELAB_PROBE_TOKEN
python3 tests/launcher_task_probe.py --private
unset HOMELAB_PROBE_TOKEN HOMELAB_PROBE_REPOSITORY HOMELAB_PROBE_BRANCH
```

The probe confirms GitHub reports a private repository, clones into a launcher-created
uid-21000 copy using the environment-only header, scans regular files without following
links for the token/header, and removes only its random scratch folders and containers.
Require `PASS: private clone uses an environment-only header and leaves no token in its copy`.
The launcher client container also receives its request through an environment variable,
so its argv cannot expose the header. No real slice turn or push is part of these probes.

**Launcher socket restarts must be followed by restarting `runs` whenever it is enabled:**
`sudo docker compose --profile runs restart runs`. Its healthcheck sends `ping` through the
bound socket; a retained dead inode becomes `unhealthy` rather than appearing ready. Docker
health does not restart it automatically. Restart Factory's runner and sessions containers
for the same reason. A failed socket probe prevents work; it does not grant a fallback path.

Rollback: stop/remove any optional `runs` container before starting the old runner. Leave the
profile disabled, restore the previously installed launcher/metadata/unit after visible diffs,
validate the unit, run `systemctl daemon-reload`, stop the launcher service and restart its socket, then
restart the Factory socket clients. No task data or secrets need deletion. The shared-directory ownership used by the switch may remain when re-enabling the root host runner. Preserve private new run
records and existing API exports. Never recover the queue with two active runners.

### Eval tasks

Use the [run API](run-api.md), with `runs:write`, `runs:eval` and the key's repository policy.
The mark is `eval: {set, case, executor}`: one validated Codex, Claude or `pi/<name>` entry.
There is no table lookup or fallback; usage limits wait for the selected executor. Pi is only
available on `ticket`. Slice, cut and review use their enabled whole-run paths; tickets use
the serial task path. Review remains read-only. The client composes the whole-slice or
cut/ticket attempt and review. Write results are read from the delivered `eval/` branch and
recorded commits, with the same private-content scan as real work. Unmarked writes cannot
push to `eval/` branches.

For grading, submit `role: check`, `mode: write`, `base_commit` and an eval mark with executor
omitted. Set `work.check_command` and `work.overlay: {commit, paths}` to the hidden test files
in another commit of the same repository. The platform imports the base into a fresh
task copy, prepares dependencies, then imports hidden objects offline and writes only the selected
regular overlay files,
and runs the command on the existing networkless `check` shape. Git and `.venv` are read-only;
there are no agent logins, commits or pushes. The completed run is `done` regardless of command
exit code, with `check: {exit_code, tail}` in GET/list records. The tail is the last 16 KiB of
combined output, decoded with replacement. Setup/launcher failures fail the run. Overlay on
another role is refused. Branch can be omitted for a check. The copy and temporary grading
files are removed after completion; result metadata stays in the existing run record.

`POST /v1/tasks/eval` and `GET /v1/tasks/eval/{id}` are removed (404), as are `eval_kept`,
`diff_base64` and active eval export handling. Old exports/mounts and the disabled host runner
remain installed for later removal; do not use the old runner for the new contract.
No migration or host installation is needed. After review and merge, **Restart the platform
on the merged commit**, then the client completes one eval slice and one check on a public
repository. See [deployment steps](run-api.md#node-steps-after-merge-and-owner-approval).

Pi eval configurations may still name `eval/<provider>/<model>` through the single `eval/*`
gateway route. Only marked Pi runs can issue a live exact-purpose task token; client keys,
chat and bot callers cannot use it. Configuration snapshots retain their hashes, image digest
and source commit. Unknown model identifiers fail upstream and normal execution limits apply.
`homelab eval results --set T` remains an attempt aggregate (successful delivery, not hidden-test
grading); Factory grades using the separate check result. Bare `homelab eval` is the knowledge eval.

## Coding tasks in a container

The installed host scripts in this section are the stopped rollback path. Active ticket runs
use the [runs service](#runs-service), which selects its worker from the executor table and
has no per-ticket review. The host scripts below retain their optional review unchanged.

`homelab-task-run` gives one ticket to the Pi agent and judges the result, one run at a time. By
hand nothing is committed or pushed. See [Coding tasks](#coding-tasks) for the
boundary; [the task runner](#task-runner) drives the same script for queued tasks.

```bash
sudo homelab-task-run --working-copy <path> --ticket <file> \
  --check '.venv/bin/python -m pytest -q' --pin tests --pin pyproject.toml \
  [--purpose task-worker] [--time-limit 30] [--max-requests 100]
```

The working copy is a clean uv project on Python 3.13 with a `uv.lock`, a real `.git` directory
(not a worktree), an unprivileged owner, and no secrets: the model reads what the agent reads.
The containers run as that owner, so changes stay in that account's ownership. HEAD is the base
commit. `--pin` is repeatable. A pinned path must exist in the base commit and returns to it before
the check, a directory exactly, so added files go too. Pin everything that decides the verdict,
including `conftest.py` and the test configuration in `pyproject.toml`, or the agent can change it.
`--time-limit` is minutes per container, 30 by default. `--max-requests` is the run's model request
budget. Dependencies cannot change after prepare: the agent has no package index, and `.venv` is
read-only. `uv run` needs no sync there. Prepare needs the stack's `package-proxy`. It retries `uv sync --locked`
up to 3 times, 5 seconds apart, with a 120-second download timeout; after the third failure the run
ends `prepare_failed`.

`--allow <path>`, repeatable, with `--author-name` and `--author-email`, adds the commit step after
a passing check. The agent must leave the message in `.task-commit-message`.

`--review-purpose <purpose>` puts a review between a passing check and the commit step; it is
refused without `--allow`, since a review only guards a commit, and without it no review runs. A
second agent, with the model of that purpose and only `read`, `grep` and `ls` tools, reads the
ticket, the allowed and pinned paths, the check command and the diff, and the repository with the
working copy mounted read-only. It ends its reply with `VERDICT: PASS` or `VERDICT: FIX`, and a Fix
carries short notes above that line. On a Pass the run goes on to the commit step. On a Fix the
notes go to the worker in the same run and working copy: one more agent run, check and review.
A second Fix ends the run `review_rejected`. Any reply that is not a clean verdict, a timeout,
a spent budget or a non-zero exit ends it `review_failed`, and a diff over 256 KiB ends it
`review_diff_too_large`; in all of these nothing is committed. The reviewer has its own token, for
the review purpose only and 20 requests; the fix round's agent gets the request budget minus the
steps already used, and with none left the run ends `agent_budget_spent`. **A review is not a
security boundary.** The model reads untrusted text and can be wrong or talked round; only the
commit step's rules on allowed paths and pins enforce anything.

Each run leaves `/srv/homelab/task-runs/<run id>/`, root-only. A run's working copy keeps the
agent's changes, with the pins restored, so use a fresh copy for the next run.

| File | Holds |
|---|---|
| `run.json` | Base commit, purpose, pins, Pi turns as `steps`, `tokens` (`fresh`, `output`, `cached`, `cache_write`, as Pi reports them, including available compaction usage; both sum the worker's runs), `compactions` (starts), `retries` (automatic retry starts and summarization retry attempts), `end_event` (the last Pi end event and its retry or abort flag), `last_stop` (the last assistant stop reason), seconds, `check_exit_code`, `check_log` (the last check's log), `ended` (`model_error` for an assistant error); with a review also `reviews` (per review: `result` `pass`, `fix` or `failed`, `steps`, `tokens`, `seconds`) and `review_notes` (the last Fix's) |
| `diff.patch` | Everything changed from the base commit except `.venv`, before pins were restored |
| `check.log` | Output of the check |
| `events.jsonl`, `agent.log` | Pi's JSON event log and its stderr |
| `prepare.log` | Dependency installation |
| `commit.log` | Why the commit step refused, if it did, naming the path |
| `review.jsonl`, `review.log` | The reviewer's events and stderr |
| `*-2.*` | A fix round's own `events`, `agent`, `diff`, `check` and `review` files; the first round's names stay |

`ended` is `check_passed`, `check_failed`, `check_setup_failed`, `check_timeout`, `agent_failed`,
`agent_budget_spent`, `agent_timeout`, `prepare_failed` or `prepare_timeout`, or `<step>_output_limit`
after 256 MiB of output. With `--allow` a passing check goes on to `committed`, `commit_failed`,
`commit_timeout` or `commit_refused_<reason>`, where the reason is `outside_allowed`, `pinned`,
`guarded`, `submodule`, `no_message` or `no_change`. With `--review-purpose` a passing check can
instead end `review_rejected`, `review_failed` or `review_diff_too_large`, as above, and a fix
round ends as its steps do (`agent_*`, `check_*`). `interrupted` means the script was stopped.
The command prints the same record and exits `0` only for `check_passed` and `committed`, `1` for
any other finished run and `2` for a refused input, such as
another active run or a locked volume. An agent that failed or timed out still gets a diff; its
check result is kept but `ended` names the agent.

A run needs the data volume, Docker, the stack and the image. Update the image deliberately:
change the Dockerfile, install it as in [Rebuilding the node](operations.md#rebuilding-the-node) and run
`sudo docker build -t homelab-task:3 /opt/homelab-task`. The build needs Internet access and fails
if the pinned base has Node below 22.19. Tokens live in `/srv/homelab/task-tokens`, `root:10001`
`0750`, and are hashes; `sudo rm` of a file there revokes a token at once. LiteLLM appends what
each request cost to `/srv/homelab/task-costs/<token hash>` (`root:10001`, `0770`, cleared at the
start of every run); the run reads and removes each file and reports `cost_usd` in its record, null
when any part is unknown. Nothing but the numbers is in those files.

### The legacy host task runner (rollback)

`homelab-task-runner.service` is stopped and disabled. It remains installed for rollback. Do not start it while `runs` is
active. If restored for rollback, it consumes the queue; see [Task runner](#task-runner).
It follows the data volume and the stack. A stopped runner's task returns to the queue at its
next start and runs again from a new clone.
Its journal holds task ids, attempt endings and reasons, never task content. When a task ends
`escalated` or `failed` it sends one phone notice, "Task: <id> <slice> <status> <reason>", through
`homelab-queue-notice --task`; if that fails, the journal says `notice failed` and the queue goes on.
Attempt and review traces carry `cost_usd`, the amount LiteLLM billed, empty when it could not be
measured in full.

Every run it makes asks for a review, by the first usable `litellm` route of the `ticket-reviewer`
role in `homelab_roles`; a task with none ends `escalated`, `no_usable_review_route`, before any
attempt. A `review_rejected` attempt gets one more try on the same route from a fresh clone, with
the reviewer's notes in the ticket; `review_failed` and `review_diff_too_large` go to the next route.
Each review is a `review` trace of role `ticket-reviewer`.
The seeded reviewer route uses `task-worker`. The `task-reviewer` purpose uses the same model
with reasoning disabled; selecting it for the reviewer is an operator update to `homelab_roles`.

The worker also accepts `{"kind":"cli","tool":"codex","model":"<model>","effort":"high"}`.
The model and effort come from the role table; supported effort values are `none`, `minimal`,
`low`, `medium`, `high` and `xhigh`. Unsupported tools or malformed routes are skipped. A CLI
route is always refused for the reviewer. The seeded worker route stays on LiteLLM: selecting
CLI execution is an operator update of `homelab_roles`, not a deployment default.

A CLI route replaces only the worker turn, through the installed `homelab-fence run --tool codex`.
Prepare, the done check, LiteLLM ticket review, the commit, push and retry rules stay the same.
The CLI gets the same ticket text, including earlier check or review feedback; a review Fix
runs another CLI turn with the notes against the current task tree. Each CLI turn has a fixed
20-minute timeout, independent of `--time-limit`. A timeout ends the attempt as `cli_timeout`,
a nonzero exit or a failed CLI invocation as `cli_failed`; both advance to the next route
without a retry on that route. A failed check still retries once from a fresh clone.

The CLI sees a plain copy at `/srv/factory/work/ticket-<task id>-<attempt>`, with no `.git`
anywhere in it and no prepared `.venv`. Its run folder is the same name under
`/srv/factory/runs`. Both folders are `21000:21000`, mode `0700`; the platform fence
owns the sealed container, tool login and allow-list proxy. The runner passes only those two
host folders, the configured model and effort, and the ticket. It passes no gateway token,
key or inherited credentials. It discards CLI stdout and stderr.

After a successful turn, only regular files within `allowed_files` return to the task copy.
Pins never return, even when also allowed. Neither source nor destination links are followed;
Git metadata and `.venv` never return. Other file changes (including deletions) are ignored,
with only their count in the journal and `run.json`'s `cli_ignored_changes`. Returning bytes
creates new regular files owned by the task user; executable bits are preserved. The plain
copy and CLI run folder are removed after each turn, including failure or timeout.
**Include `.task-commit-message` in a CLI ticket's `allowed_files`** if it is to commit: this
metadata follows the same import rule, and the existing commit step still requires it and
excludes it from the commit. A missing message ends `commit_refused_no_message`.
CLI worker steps and tokens stay zero and `cost_usd` is absent: subscription usage was not
measured. Review usage and cost remain separate and measured through LiteLLM.

```bash
sudo systemctl status homelab-task-runner.service
sudo journalctl -u homelab-task-runner.service -n 50
sudo docker exec homelab-postgres-1 psql -U homelab -d homelab -c \
  "SELECT id, slice, position, status, reason FROM homelab_tasks ORDER BY id DESC LIMIT 20"
```

Each repository it pushes to needs a deploy key with write access at
`/root/.ssh/task-push-<repository name>`, `root:root` `0600`, and each task a branch that exists
and is not the default. Without the key a task runs through its commit and ends `escalated`,
`push_key_missing`. The commit author is set in the unit; change it with a drop-in.

A slice stops at its first `escalated` or `failed` task. To go on, fix the cause, then requeue that
task and any other task of the slice that lost its job, one id at a time:

```bash
sudo docker exec homelab-postgres-1 psql -U homelab -d homelab -c \
  "UPDATE homelab_tasks SET status = 'queued', reason = NULL, finished_at = NULL WHERE id = <id>"
sudo docker exec homelab-worker-1 procrastinate --app homelab.jobs.app.app defer \
  run_coding_task '{"task_id": <id>}'
```

Clear the status of every stopped task in the slice before deferring, or the claim discards the job.
Dependents need neither: they are released as their dependencies finish.

`package-proxy` is built by Compose with the rest of the stack. Its journal lists the hosts prepare
asked for, and what it refused. To allow another host, add a line to
`node/opt/homelab-package-proxy/package-hosts`.

For eval catalogue routing updates, install both task scripts together and rebuild the gateway
from the merged checkout. Wait for the task runner to be idle, then run on the node:

```bash
cd /srv/homelab/homelab-v2
git pull --ff-only
sudo diff -u /usr/local/sbin/homelab-task-run node/usr/local/sbin/homelab-task-run
sudo diff -u /usr/local/sbin/homelab-task-runner node/usr/local/sbin/homelab-task-runner
# Review both diffs (diff exits 1 when files differ) before copying.
sudo install -m 0755 -o root -g root node/usr/local/sbin/homelab-task-run /usr/local/sbin/homelab-task-run
sudo install -m 0755 -o root -g root node/usr/local/sbin/homelab-task-runner /usr/local/sbin/homelab-task-runner
sudo systemctl restart homelab.service
sudo systemctl restart homelab-task-runner.service
```

The gateway configuration and hooks are mounted from the checkout; no host copy or unit change
is needed. These script-install steps apply only to the stopped rollback runner. For an active
Pi eval use `/v1/runs` with `executor: {configuration: "pi/eval-deepseek-v4.1-flash"}` on an
`eval/` branch; confirm delivery and `cost_usd`, and check its private
`run.json` purpose and configuration hash. With an existing client key, request
`eval/deepseek/deepseek-v4.1-flash` and confirm 403. Run a normal `task-worker` task and `chat`
request as regression checks. Use existing private credentials without printing or changing them.
To roll back, restore both scripts and gateway config/hooks from the preceding commit, review
the diffs, reinstall both scripts and restart the same two services; no stored data changes.

The installed task scripts use configurations from merged `origin/main` for queued runs; no
configuration installation is needed. After changing either script, compare it with the installed
file before installing it using the commands below. After a configuration change, run a task with
`base` and check its `run.json` for the configuration hash and source commit.
