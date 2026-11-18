# Run API and node provisioning

The loopback API at `127.0.0.1:8000` accepts version-one run requests. Postgres owns run records
beside the existing tasks; Procrastinate owns their queue. The host runner, launcher and gateway
are unchanged.

## Requests and observations

`POST /v1/runs` requires `runs:write`. Submit a stable application-local `request_id`; the first
submission returns `201 {"run_id":"<uuid>"}`, and a retry of the same validated request returns
`200` with that id, without another task or job. A changed request returns
`409 request_conflict`. Omitted optional fields and their explicit defaults compare equally.
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
ownership checks. Today's executor cannot resume a conversation, so it returns
`409 not_resumable`; records report `resumable:false`. No message content is stored or logged.

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
| standard | Codex gpt-6.1-terra, medium | Claude sonnet |
| strong | Codex gpt-6.1-sol, high | Claude opus |

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
An `eval` mark requires `runs:eval`, chooses its named Pi configuration (`base` or `pi/base`),
and populates the existing task eval fields. It runs Pi without a review. The runner keeps its change as a diff and
never calls its push step. GET includes `diff_base64` from the existing read-only eval export
mount, with the same 2 MiB bound and symlink/path checks as the legacy endpoint.

Read mode and any request containing `limits` are refused at create with HTTP 409,
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

`commits` lists the pushed SHAs for `slice` and `ticket` runs.
For whole-slice write runs, `commits` is empty when the executor made no commit
(the checked bundle tip equals `base_commit`); otherwise it contains only the pushed tip.
A pushed ticket appends the
successful attempt's `base..commit` SHAs, oldest first and without duplicates, to any already
recorded, in the transaction that ends the run. Rejected or discarded attempts, other endings
and kept evals leave `commits` unchanged. Tickets keep `final_message:null`; usage and attempt
outcomes come from the existing task traces. Unknown costs remain null. Run status reasons are
mapped to the fixed version-one vocabulary while the legacy endpoint preserves original task
reasons.

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
`403 repository_not_allowed`; a branch equal to its listed default branch or `main` returns
`409 branch_is_default`. Refusals leave no run, task or job. Keep each map current when changing
repository settings. The host's independent remote default-branch guard still applies before
execution. Rotated keys must carry their intended repository policy explicitly. If the host
file is replaced atomically, restart the API to remount the new inode; edits to the existing
mounted inode are read on the next request.

`/v1/tasks/eval` authorizes through this same key file, requiring write/eval for POST and
read/eval for GET. It creates a run and returns the original `201 {task:<integer>}` response;
a duplicate attempt still returns `409 {error:"Attempt already submitted",task:<integer>}`,
even if the retry's content changes. Its GET shape, attempts, usage and diff are unchanged.
The alias retains its historical task-body validation and allows independent eval attempts
on the same branch. New submissions share the authenticated key's repository/default-branch
checks. Its slice names include the application.
Factory's pre-upgrade `eval:<attempt>` tasks remain readable and deduplicated; no other
application can use that compatibility path. The `branch_busy` exclusion applies to `/v1/runs`;
independent legacy eval attempts keep their existing scheduling behavior.

## Node steps after merge and owner approval

These steps touch the existing credential and guard configuration. The owner must approve
before the orchestrator performs them. Do not run them from a client container. Review the
capability boundaries above before approving this deployment.

1. Record the previous commit and keep its checkout and `compose.yaml` available for rollback.
   New tables are additive. Before a rollback, pause submissions, let claimed run tasks finish
   and stop `homelab-task-runner.service`. Mark queued run-linked tasks failed with `interrupted`
   rather than handing their empty bridge goals to the old claim code. Preserve all run rows.
   Restore the previous checkout/Compose and restart `homelab.service`, then the task runner.
   No table or volume deletion is needed. Cancelled/failed requests need new request ids to run
   again. Existing unlinked task rows do not need conversion.
2. While the old API still runs, create the key file on the node from its existing environment,
   without printing either key or digest. Replace the example map below with every approved
   Factory repository and its actual default branch before running. Run as root, with no shell
   tracing; the script refuses an unchanged placeholder:

   ```sh
   sudo install -d -o root -g root -m 0755 /etc/homelab
   sudo python3 - <<'PY'
   import hashlib
   import json
   import os
   import subprocess

   repositories = {"owner/scratch-repo": "main"}
   if "owner/scratch-repo" in repositories:
       raise SystemExit("Replace the placeholder repository policy; nothing written")
   raw = subprocess.check_output(["docker", "inspect", "homelab-api-1"])
   environment = json.loads(raw)[0]["Config"]["Env"]
   values = dict(item.split("=", 1) for item in environment if "=" in item)
   key = values.get("HOMELAB_EVAL_TASK_KEY", "")
   if not key:
       raise SystemExit("Existing eval key is unavailable; nothing written")
   entries = [{"application": "factory", "sha256": hashlib.sha256(key.encode()).hexdigest(),
               "scopes": ["runs:write", "runs:read", "runs:eval"],
               "repositories": repositories}]
   fd = os.open("/etc/homelab/run-keys.json", os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o644)
   with os.fdopen(fd, "w") as stream:
       os.fchown(stream.fileno(), 0, 0)
       os.fchmod(stream.fileno(), 0o644)
       json.dump(entries, stream)
       stream.write("\n")
   PY
   ```

   This refuses to overwrite an existing file. Inspect only owner/mode with `stat`, never `cat`
   the installed file into a transcript. Keep the existing bearer key for Factory unchanged.
3. In `/srv/homelab/homelab-v2`, pull by hand with `git pull --ff-only`; then
   `sudo systemctl restart homelab.service` builds and starts the images. The worker's existing
   schema start step installs the additive run table, indexes and triggers. A malformed table
   or unreadable key file prevents the API from starting; inspect metadata-only service logs.
4. Confirm `/health`, and use Factory's unchanged bearer key to GET a known existing
   `/v1/tasks/eval/<id>` and submit/read a scratch eval through the legacy calls. Confirm the
   response shape, completed diff and absence of a push. Do not print authorization headers,
   goals, assets or final messages into service logs or the deployment transcript.
5. Submit one scratch `ticket` write through `/v1/runs`, without explicit limits, on an existing
   non-default branch. Poll to completion on today's runner; confirm one task/job, the actual
   attempt hash and usage, and the delivered change on the scratch branch. Repeat the identical
   request and confirm the same id with HTTP 200 and no second job. Do not declare the rollout
   verified until both Factory's legacy path and this new path have completed.
