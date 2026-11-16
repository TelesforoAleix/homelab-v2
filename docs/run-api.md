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
recorded; the table does not replace or edit `homelab_roles`.

Pi hashes use the existing configuration manifest: SHA-256 of compact JSON encoding of sorted
`[relative path, file SHA-256]` pairs, covering all regular UTF-8 files. CLI hashes cover canonical
JSON of the selected harness, model and effort. Attempt hashes remain the actual hashes
reported by the runner, separately from the selection recorded at submission.

A normal `ticket` write run using the baseline configuration becomes one task for today's
runner. `slice`, `cut` and `review` without an eval mark return `409 role_not_available`.
An `eval` mark requires `runs:eval`, chooses its named Pi configuration (`base` or `pi/base`),
and populates the existing task eval fields. The runner then keeps its change as a diff and
never calls its push step. GET includes `diff_base64` from the existing read-only eval export
mount, with the same 2 MiB bound and symlink/path checks as the legacy endpoint.

The current bridge cannot enforce read-only execution or explicit per-run limits over retries
and review. Such requests are persisted as `failed/no_executor_available`, with one failed
task and no queue job. A requested limit above the selected Pi configuration's request or
minute limit returns `422 invalid_limits` without a record. The executor table's shape is
unchanged; configuration limits come from its referenced `configuration.json`. No USD cap
is invented. A non-baseline table selection for a non-eval ticket also fails without a job,
because today's host always selects its baseline configuration for that path.

The host clones the selected branch's complete single-branch history for either history hint.
Null `allowed_files` uses the existing sandbox's unrestricted default; a null check uses
`true`. Assets are appended to the ticket as JSON in memory at claim time. The goal is stored
only in `homelab_runs.goal`, not in task rows, request JSON or queue arguments. Legacy tasks
keep their existing goal storage and behavior. Logs never include goals, assets or final
messages.

The host does not report delivered commit SHAs or a final message through its finish protocol.
These fields remain `commits:[]` and `final_message:null`; usage and attempt outcomes come from
the existing task traces. Unknown costs remain null. Run status reasons are mapped to the
fixed version-one vocabulary while the legacy endpoint preserves original task reasons.

## Key file and repository policy

Only the API bind-mounts `/etc/homelab/run-keys.json`, read-only, at the same path. Compose refuses
to create a directory if the file is absent. The API runs as UID 10001; install the directory as
`root:root 0755` and the file as `root:root 0644`. It holds bearer **digests**, never bearer keys.
Startup and each request reject symlinks, non-root ownership and group/other write permissions.
Malformed files fail startup; a file becoming unavailable at runtime fails authorization closed.
Do not replace it with a plaintext key file. Rotation may add another digest for the same
application, then remove the old one; keep the owner and mode when replacing the file.

The shape is a JSON list of `{application, sha256, scopes}`. The initial entry is `factory`, the
SHA-256 of the existing `HOMELAB_EVAL_TASK_KEY`, and scopes `runs:write`, `runs:read`, `runs:eval`.
The [checked-in example](run-keys.example.json) contains only an all-zero dummy digest. The API
does not fall back to its environment key for authorization.

Repository access for new run requests is deny-by-default. An operator provisions
`homelab_run_repositories(application, repository, default_branch)` in Postgres. A missing row
returns `403 repository_not_allowed`; a write to the configured default branch returns
`409 branch_is_default`. Keep default branches current when changing repository settings. The
host's independent remote default-branch guard still applies before execution. The host also
refuses a branch named `main` when it is not the default; such a run has no available executor.

`/v1/tasks/eval` authorizes through this same key file, requiring write/eval for POST and
read/eval for GET. It creates a run and returns the original `201 {task:<integer>}` response;
a duplicate attempt still returns `409 {error:"Attempt already submitted",task:<integer>}`,
even if the retry's content changes. Its GET shape, attempts, usage and diff are unchanged.
To preserve existing clients, the alias retains its historical submission validation and
allows independent eval attempts on the same branch. Its slice names include the application.
Factory's pre-upgrade `eval:<attempt>` tasks remain readable and deduplicated; no other
application can use that compatibility path. New run repository/branch exclusion rules apply
to `/v1/runs`, rather than changing historical alias behavior.

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
   without printing either key or digest. Run as root, with no shell tracing:

   ```sh
   sudo install -d -o root -g root -m 0755 /etc/homelab
   sudo python3 - <<'PY'
   import hashlib
   import json
   import os
   import subprocess

   raw = subprocess.check_output(["docker", "inspect", "homelab-api-1"])
   environment = json.loads(raw)[0]["Config"]["Env"]
   values = dict(item.split("=", 1) for item in environment if "=" in item)
   key = values.get("HOMELAB_EVAL_TASK_KEY", "")
   if not key:
       raise SystemExit("Existing eval key is unavailable; nothing written")
   entries = [{"application": "factory", "sha256": hashlib.sha256(key.encode()).hexdigest(),
               "scopes": ["runs:write", "runs:read", "runs:eval"]}]
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
   schema start step installs the additive run tables, indexes and triggers. A malformed table
   or unreadable key file prevents the API from starting; inspect metadata-only service logs.
4. Provision each Factory repository and its actual default branch. For example, replace the
   public placeholders below before running against the node's Postgres:

   ```sh
   docker compose exec -T postgres psql -U homelab -d homelab <<'SQL'
   INSERT INTO homelab_run_repositories (application, repository, default_branch)
   VALUES ('factory', 'owner/scratch-repo', 'main')
   ON CONFLICT (application, repository) DO UPDATE SET default_branch = EXCLUDED.default_branch;
   SQL
   ```

5. Confirm `/health`, and use Factory's unchanged bearer key to GET a known existing
   `/v1/tasks/eval/<id>` and submit/read a scratch eval through the legacy calls. Confirm the
   response shape, completed diff and absence of a push. Do not print authorization headers,
   goals, assets or final messages into service logs or the deployment transcript.
6. Submit one scratch `ticket` write through `/v1/runs`, without explicit limits, on an existing
   non-default branch. Poll to completion on today's runner; confirm one task/job, the actual
   attempt hash and usage, and the delivered change on the scratch branch. Repeat the identical
   request and confirm the same id with HTTP 200 and no second job. Do not declare the rollout
   verified until both Factory's legacy path and this new path have completed.
