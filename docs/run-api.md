# Run API and node provisioning

The loopback API at `127.0.0.1:8000` accepts version-one run requests. Postgres owns run records
beside the existing tasks; Procrastinate owns their queue. The launcher and gateway are unchanged.

## Requests and observations

`POST /v1/runs` requires `runs:write`. Submit a stable application-local `request_id`; the first
submission returns `201 {"run_id":"<uuid>"}`, and a retry of the same validated request returns
`200` with that id, without another task or job. A changed request returns
`409 request_conflict`. Omitted optional fields and their explicit defaults compare equally. Real request retries
retain their pre-overlay fingerprints across this upgrade.
Unknown fields and invalid types return a redacted `422 invalid_request`; a bad asset digest
returns `400 asset_hash_mismatch`. Asset digests cover UTF-8 text. Serialized assets together
are limited to 200,000 UTF-8 bytes, with at most 32 entries. Requests are bounded to 4 MiB.

```json
{
  "contract": 1,
  "request_id": "example-ticket-1",
  "attribution": {"client": "factory"},
  "repository": "owner/scratch-repo",
  "base_commit": "aaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaa",
  "branch": "work/example-ticket",
  "mode": "write",
  "role": "ticket",
  "level": "standard",
  "work": {
    "goal": "Make the requested change",
    "allowed_files": ["docs/example.md"],
    "pinned_paths": ["tests/check.sh"],
    "check_command": "test -f docs/example.md",
    "history": "shallow"
  },
  "after": [],
  "priority": 0
}
```

`GET /v1/runs/{run_id}` requires `runs:read`. It returns application identity, attribution,
executor selection and its configuration hash, attempts, times and usage. Goals and asset
content are omitted from observations. Unknown ids and another application's ids both return
`404 run_not_found`. Applications with multiple keys share their runs.

`GET /v1/runs?request_id=example-ticket-1` returns the application's matching run in `runs`.
`GET /v1/runs?changed_after=<cursor>` returns up to 100 changed records, oldest change first,
and `cursor` for the next call. Omit the cursor for an initial listing. Cursors are opaque,
application-scoped watermarks; empty pages retain their watermark. Updates to attempts and
usage also make a run appear again. Transaction locks serialize bridge writes and cursor
reads so an uncommitted change cannot be skipped. This serialization does not apply to
unlinked legacy tasks.

`after` names previously submitted requests of the same application, repository and branch.
Their task ids become `depends_on`. A chain names its immediate predecessor; its transitive
ancestors count as part of the same chain for branch exclusion. An unrelated active run on
the branch returns `409 branch_busy`. A predecessor must finish `done`; any other terminal
outcome cancels queued descendants with `predecessor`, including descendants submitted later.

`POST /v1/runs/{run_id}/cancel` cancels queued work and its queued descendants without removing
records. Existing queue jobs are discarded by the normal task claim path. A repeated cancel
returns the terminal record. The current host protocol cannot interrupt a running writer;
that request returns `409 not_resumable` and leaves its state honest.

`POST /v1/runs/{run_id}/messages` accepts `{contract:1,message_id,text}` after authorization and
ownership checks. Ticket and check runs return `409 not_resumable`. Whole-run roles can
resume a question, escalation, client wait or completed conversation, retaining the harness
session and executor; message ids deduplicate turns. Content stays in the private store and
never enters logs. See the [service runbook](runs.md#runs-service) for copy retention.

## Executors and current capability

`config/executors.yaml` maps each of `ticket`, `slice`, `cut` and `review` to `light`, `standard`
and `strong`, each an ordered, nonempty list. An entry is either `{configuration: pi/base}` or
`{harness: codex|claude, model: ..., effort: ...}`; Codex requires effort. Unknown fields,
missing rows, mixed kinds and unavailable Pi folders fail API startup. The first entry is
recorded at submission; dispatch records the executor that actually runs and its hash.
`runs` does not read `homelab_roles`; that table remains for the stopped rollback host runner.

Pi hashes use the existing configuration manifest: SHA-256 of compact JSON encoding of sorted
`[relative path, file SHA-256]` pairs, covering all regular UTF-8 files. CLI hashes cover canonical
JSON of the selected harness, model and effort. Attempt hashes remain the actual hashes
reported by the runner, separately from the selection recorded at submission.

A normal `ticket` write becomes one task on the serial task queue. The runner uses
`usage.choose` on the `ticket` entries at the requested level, in order:

| Level | First | At its usage limit |
|---|---|---|
| light | Codex gpt-6-luna, low | Claude sonnet |
| standard | Claude sonnet | Codex gpt-6.1-terra, medium |
| strong | Claude opus | Codex gpt-6.1-sol, high |

Slice uses the same order as tickets. Cuts use Claude opus first and Codex gpt-6.1-sol,
high second at every level; reviews use Claude sonnet first at every level, with Codex gpt-6.1-sol, high second at every level.

Fallback uses the same subscription windows, unknown-reading policy and Claude probe cache
as slices. With none usable the run waits `usage_limit`, with `resume_at` set to the earliest
reset and a Procrastinate retry scheduled for that time. Both harness workers use
`slices.command` through the launcher's `cli-run` profile. Tickets keep the fresh clone,
pinned paths, allowed files, check, commit step and push. A failed check gets one retry in a
fresh clone with its output as feedback, then escalates. Other failures do not advance the
executor list. No review runs inside a ticket; the client reviews the slice before merge.
Ticket runs work on private repositories the GitHub App is installed on. Remote reads,
including the default-branch lookup and fresh clone, use repository-scoped installation tokens
as authorization headers, never credentials in URLs or logs. `main` and the remote default
branch remain refused.
A `configuration:` entry remains valid and uses its Pi worker without a review.
`slice`, `cut` and `review` without an eval mark return `409 role_not_available` unless enabled
as described below.
An `eval` mark requires `runs:eval` in addition to the normal write/read scopes:

```json
"eval": {"set": "T", "case": "example",
         "executor": {"harness": "codex", "model": "<model>", "effort": "high"}}
```

`set` and `case` are 1–64 letters, digits, dots, underscores or hyphens, starting with a
letter or digit. `executor` is validated exactly like a catalogue entry: Codex requires
model and effort, Claude requires model, or use `{configuration: "pi/<name>"}`.
The old `configuration` field on the mark is removed. Marked runs use exactly the supplied
entry, with no catalogue lookup or fallback. Usage limits wait `usage_limit` with the normal
scheduled retry. A Pi entry is accepted only for `ticket`; other roles return
`409 role_not_available` before storage.

Eval `slice`, `cut` and `review` use their normal whole-run path and require the role to be
enabled; eval `ticket` uses the normal ticket path. Review remains read-only. The client
composes an attempt as a slice followed by review, or a cut followed by ordered tickets on
one branch and then review, with each request naming its own executor. Copy preparation,
prompts, checks, commit and delivery follow the same paths as real work.
A marked write may push only to `eval/…`; an unmarked write may never push to `eval/…`.
Both delivery paths enforce this rule and keep the existing default-branch guard and
private-content scan. Refused pushes report `push_refused`. Eval commits are delivered and
recorded like real commits; the API no longer returns `diff_base64` or reads diff exports.

### Sealed check runs

`role: check` requires `mode: write`, an eval mark with **executor omitted**,
`base_commit` naming the commit to grade, `work.check_command`, and
`work.overlay: {commit, paths}`. Other common request fields still apply, including goal
and level; branch may be omitted because a check never delivers. No executor runs.
`work.overlay` must be omitted on every other role; explicit null is also refused. Example work and mark:

```json
"role": "check", "mode": "write",
"base_commit": "aaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaa",
"level": "standard",
"eval": {"set": "T", "case": "example"},
"work": {"goal": "Grade the attempt", "check_command": "uv run pytest tests/hidden",
         "overlay": {"commit": "bbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbb",
                     "paths": ["tests/hidden"]}}
```

The platform fetches both exact commits from the same authorized repository into an isolated
Git store and imports only the base objects into a fresh launcher-owned task copy. It checks out
`base_commit`, prepares dependencies as a ticket does, then imports the overlay objects offline and copies only the selected overlay
files from `overlay.commit`. Paths may name files or directories (recursively); missing paths,
symlinks, submodules and paths through destination directory links are refused. Git metadata
and `.venv` cannot be overlaid. Replacement files use new inodes and preserve executable mode.
The command runs on the existing `check` shape: no network or agent logins, `/work` writable,
and `.git` and `.venv` read-only. Nothing is committed or pushed; the copy is removed afterward.
A completed command ends `done` even for a nonzero exit, with
`check: {exit_code, tail}` in GET/list records. `tail` contains the last 16 KiB of combined
output decoded as UTF-8 with replacement. Setup/launcher failures fail the run instead.
Before completion, `check` is null. Check records expose null executor/hash and empty commits;
the result uses existing private record metadata, without a database migration.

Read mode for tickets/checks or disabled whole-run roles, and any request containing `limits`, are refused at create with HTTP 409,
`error:role_not_available`, `status_reason:role_not_available`, and the message
"Read mode and per-run limits arrive with the execution service." This includes an empty
`limits` object or explicit null: omit the field for today's executor. The refusal leaves no
run, task or queue job. The executor table's shape is unchanged. Pi configurations supply
their execution limits; harness ticket turns retain their 20-minute deadline.

The host clones the selected branch's complete single-branch history for either history hint.
Null `allowed_files` uses the existing sandbox's unrestricted default; a null check uses
`true`. Assets are appended to the ticket as JSON in memory at claim time. The goal is stored
only in `homelab_runs.goal`, not in task rows, request JSON or queue arguments. Legacy tasks
keep their existing goal storage and behavior. Logs never include goals, assets or final
messages.

`commits` lists the pushed SHAs for write `slice`, `cut` and `ticket` runs.
For whole-slice write runs, `commits` is empty when the executor made no commit
(the checked bundle tip equals `base_commit`); otherwise it contains only the pushed tip.
A pushed ticket appends the
successful attempt's `base..commit` SHAs, oldest first and without duplicates, to any already
recorded, in the transaction that ends the run. Rejected or discarded attempts, other endings
leave `commits` unchanged. Tickets keep `final_message:null`; usage and attempt
outcomes come from the existing task traces. Unknown costs remain null. Run status reasons are
mapped to the fixed version-one vocabulary.

## Key file and repository policy

Only the API bind-mounts `/etc/homelab/run-keys.json`, read-only, at the same path. Compose refuses
to create a directory if the file is absent. The API runs as UID 10001; install the directory as
`root:root 0755` and the file as `root:root 0644`. It holds bearer **digests**, never bearer keys.
Startup and each request reject symlinks, non-root ownership and group/other write permissions.
Malformed files fail startup; a file becoming unavailable at runtime fails authorization closed.
Do not replace it with a plaintext key file. Rotation may add another digest for the same
application, then remove the old one; keep the owner and mode when replacing the file.

The shape is a JSON list of `{application, sha256, scopes, repositories}`. `repositories` maps
each allowed `owner/name` directly to its default branch, for example
`{"owner/scratch-repo":"main"}`. Each key has its own policy: another key for the same application
does not extend its repository permissions. An empty map denies every repository. There is no
Postgres repository-policy table.

The initial entry is `factory`, the SHA-256 of the existing `HOMELAB_EVAL_TASK_KEY`, scopes
`runs:write`, `runs:read`, `runs:eval`, and the owner's approved repository/default-branch map.
The [checked-in example](run-keys.example.json) contains only an all-zero dummy digest and a
placeholder repository. The API does not fall back to its environment key for authorization.

A new submission for a repository absent from the authenticated key's map returns
`403 repository_not_allowed`; a delivery branch equal to its listed default branch or `main` returns
`409 branch_is_default`. Refusals leave no run, task or job. Keep each map current when changing
repository settings. The host's independent remote default-branch guard still applies before
execution. Rotated keys must carry their intended repository policy explicitly. If the host
file is replaced atomically, restart the API to remount the new inode; edits to the existing
mounted inode are read on the next request.

The old `POST /v1/tasks/eval` and `GET /v1/tasks/eval/{id}` routes are removed and return 404.
Use `/v1/runs` for submission and observations. Existing task data and old export mounts are
left intact; the API and active runner no longer use those exports.

## Node steps after merge and owner approval

**Restart the platform on the merged commit.** In `/srv/homelab/homelab-v2`, pull by hand with
`git pull --ff-only`, then `sudo systemctl restart homelab.service` to build the images.
No host-file, configuration, credential or schema change is needed. Old mounts and the stopped
rollback runner remain installed. The client, after merge, completes one eval slice run and
one check run on a public repository; confirm delivered commits and the check exit/tail.
Do not log authorization headers, goals, tokens or file contents. CI and owner review precede
merge because the run contract and push rules changed. For rollback, restore the preceding
checkout and restart the platform; preserve stored records and do not submit the new check
or executor-mark requests to the older API.
