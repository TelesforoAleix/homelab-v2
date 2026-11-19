# Container launcher and CLI fence

This document covers the root socket launcher that starts every agent container, and the CLI
fence it builds on: the caller list, the wire protocol, the `task` profile shapes, and their
install, probe and rollback steps. Read it before changing `homelab-launcher`, `homelab-fence`,
`homelab-check-fence`, `node/etc/homelab-launcher/` or `node/opt/homelab-fence/`.

## Install the CLI fence

After merge and the owner's go, install from the committed tree on the node. Keep the old
`factory-cli` command, images, proxy and networks installed for rollback. The new fence keeps
uid/gid 21000 (proxy 21001), the existing `/srv/factory/{work,runs,logins}` roots and logins;
no credentials are copied or changed. Its internal and egress networks use `172.29.0.0/16`
and `172.30.0.0/16`, with no published ports.

Show the installed-file diffs before copying (a missing destination is a new file), then install
root:root directories at 0755, configuration/Dockerfiles at 0644 and scripts at 0755.
Also install `homelab-task-run` with its install command above after reviewing its diff:

```bash
sudo diff -ruN /opt/homelab-fence node/opt/homelab-fence
sudo diff -uN /usr/local/sbin/homelab-fence node/usr/local/sbin/homelab-fence
sudo diff -uN /usr/local/sbin/homelab-check-fence node/usr/local/sbin/homelab-check-fence
sudo diff -u /usr/local/sbin/homelab-task-run node/usr/local/sbin/homelab-task-run
# Review the diffs above before continuing; diff exits 1 when files differ.
sudo install -d -m 0755 -o root -g root /opt/homelab-fence /opt/homelab-fence/cli /opt/homelab-fence/proxy
sudo install -m 0644 -o root -g root node/opt/homelab-fence/compose.yaml /opt/homelab-fence/
sudo install -m 0644 -o root -g root node/opt/homelab-fence/cli/Dockerfile /opt/homelab-fence/cli/
sudo install -m 0644 -o root -g root node/opt/homelab-fence/proxy/* /opt/homelab-fence/proxy/
sudo install -m 0755 -o root -g root node/usr/local/sbin/homelab-fence node/usr/local/sbin/homelab-check-fence /usr/local/sbin/
sudo docker compose -f /opt/homelab-fence/compose.yaml config --quiet
sudo docker build -t homelab-fence:1 /opt/homelab-fence/cli
sudo docker compose -f /opt/homelab-fence/compose.yaml build cli-proxy
sudo docker compose -f /opt/homelab-fence/compose.yaml up -d cli-proxy
sudo homelab-check-fence
```

Require all four PASS lines, then run one CLI-route task through `homelab-fence` using the
existing route and login, and check its done check, review and commit. The worker default
remains LiteLLM. For rollback, restore the previous `homelab-task-run` command from its
commit so it calls the still-installed `factory-cli`; leave the existing roots and logins alone.

## Install the fixed fence launcher

The root helper runs fixed fence and task profiles for authorized callers.
The launcher, its data, host units/scripts, Compose files,
Dockerfiles, proxy allowlists, fence probe and `.github/` are guard files: changes require the
owner's review before merge. The permanent caller list is:

| uid | Caller | Allowed profiles |
|---|---|---|
| 0 | Root | `cli-run`, `chat`, `bundle`, `usage`, `task` |
| 21010 | Factory runner | `cli-run`, `bundle`, `usage` |
| 21011 | Factory chat service | `chat`, `cli-run`, `bundle` |
| 21020 | Execution service (`homelab-runs`) | `task`, `cli-run`, `bundle`, `usage` |

The chat service uses `bundle` to export a planning chat's commits after a turn.
Login remains an owner-terminal operation.

For a caller-list update on an existing installation, run from the node's committed,
merged tree. Show and review the installed-file diff before copying (exit 1 means a
difference; stop on an inspection error):

```bash
sudo diff -uN /etc/homelab-launcher/callers.json node/etc/homelab-launcher/callers.json
sudo install -m 0644 -o root -g root node/etc/homelab-launcher/callers.json /etc/homelab-launcher/callers.json
sudo systemctl restart homelab-launcher.service
```

After installing the chat service's `bundle` permission, verify that a planning chat
delivers a commit.

From the node's committed, merged tree, show these diffs before installing (exit 1 means a
difference; stop on an inspection error). The directory and configuration files are root:root
0755/0644; the locally generated `images` file is root:root 0600; scripts are 0755. Keep
`/srv/factory/{work,runs,logins}` as real directories, with root-controlled parent directories;
never replace an ancestor with a symlink. Keep logins at their existing 21000:21000, 0700.

```bash
sudo diff -uN /usr/local/sbin/homelab-launcher node/usr/local/sbin/homelab-launcher
sudo diff -uN /usr/local/sbin/homelab-check-fence node/usr/local/sbin/homelab-check-fence
sudo diff -ruN /etc/homelab-launcher node/etc/homelab-launcher
sudo diff -uN /etc/systemd/system/homelab-launcher.socket node/etc/systemd/system/homelab-launcher.socket
sudo diff -uN /etc/systemd/system/homelab-launcher.service node/etc/systemd/system/homelab-launcher.service
# Review the diffs before continuing. No existing login is copied or modified.
getent group homelab-launch >/dev/null || sudo groupadd --system homelab-launch
sudo install -d -m 0755 -o root -g root /etc/homelab-launcher
sudo install -m 0644 -o root -g root node/etc/homelab-launcher/profiles.json node/etc/homelab-launcher/callers.json /etc/homelab-launcher/
sudo install -m 0755 -o root -g root node/usr/local/sbin/homelab-launcher /usr/local/sbin/
# Install the updated homelab-check-fence using its CLI fence install command above.
for unit in homelab-launcher.socket homelab-launcher.service; do sudo install -m 0644 -o root -g root "node/etc/systemd/system/$unit" /etc/systemd/system/; done
# homelab-fence must already be installed beside the launcher; its pure builders are imported.
# Approve the image built by the CLI fence installation; repeat explicitly after a rebuild.
sudo docker image inspect --format '{{.Id}}' homelab-fence:1
sudo install -m 0600 -o root -g root /dev/null /etc/homelab-launcher/images
sudo sh -ec 'docker image inspect --format "{{.Id}}" homelab-fence:1 > /etc/homelab-launcher/images'
sudo systemd-analyze verify /etc/systemd/system/homelab-launcher.socket /etc/systemd/system/homelab-launcher.service
sudo systemctl daemon-reload
sudo systemctl enable --now homelab-launcher.socket
sudo homelab-check-fence
sudo homelab-check-fence --launcher
```

Require the original four PASS lines and ten launcher PASS lines. The new option is a root
orchestrator that starts short-lived offline containers as numeric uids 21998 and 21999,
with only the launcher socket mounted and its group added. It refuses to borrow an existing
account or configured caller. It temporarily allows uid 21998 all four profiles, restarts the
helper to load it, and uses disposable work/run folders and an empty Git commit. The four
profile probes print help, export the empty commit or discard the usage output; no model call
is made. It temporarily substitutes an unapproved id in `images` for the refusal probe,
restores both configuration files in `finally`, restarts the helper and removes its fixtures
and run metadata. No login contents are read by the orchestrator or changed. Run this probe
with no other launcher clients active. The permanent caller configuration is restored to
the caller list above afterward.

The service accepts one newline-terminated UTF-8 JSON object per Unix stream connection at
`/run/homelab-launcher.sock` (root:homelab-launch 0660). The complete line, including its
newline, is at most 65536 bytes. Duplicate keys, non-finite numbers, extra/missing fields and
wrong types are refused. Socket reads have a 30-second timeout. The kernel's `SO_PEERCRED`
uid selects allowed profiles from `callers.json`; a supplied uid is an unknown field. Profile
and caller changes take effect after restarting the service; image approval is checked per
launch. The tag must resolve to an approved sha256 id and Docker runs that id with pull disabled.
The configured layouts/limits must match the fixed builders; changing metadata cannot add a
profile, mount or network.

| Operation | Exact request fields besides `op` |
|---|---|
| `own`, `remove` | `role` (`work` or `run`), `folder_id` |
| `run`, profile `cli-run` | `profile`, `run_id`, `folders` (`work` and `run` ids), `tool` (`claude` or `codex`), `command` (argv), `exports` (list of repository export ids, possibly empty); optional boolean `read_only` and `stdin`, both default false |
| `run`, profile `chat` | The `cli-run` fields plus positive integer `turn` (at most 2^63-1) |
| `run`, profile `bundle` | `profile`, `run_id`, `folders` (`work` and `run` ids), `branch`; optional boolean `stream` (default false) |
| `run`, profile `usage` | `profile`, `run_id` |
| `run`, profile `task` | `profile`, `run_id`, `work_id`, `shape`, `env`, `arguments` (see [task contract](#task-launcher-profile)) |
| `status`, `stop` | `run_id` |

IDs match `[A-Za-z0-9][A-Za-z0-9._-]{0,127}`. Work folders select one or two identifier parts
(such as `<work>` or `session-N/planning`) under `/srv/factory/work`, including for `own`
and `remove`; run folders select one to three identifier parts (such as `<run>`,
`<turn>/io` or `<scope>/<run>/io`) under `/srv/factory/runs`
with each part matching the identifier rule, and export ids select `<work>/repos/<id>`. Login paths
come only from the fixed tool name. Existing directories and every ancestor must be real,
with no links. `own` walks without following links and changes the top folder last; `remove`
uses Python's descriptor-based, symlink-resistant tree removal, including unlinking leaf
symlinks without touching their targets. Neither operation accepts the login role.

For `run` only, send two writable descriptors (stdout, stderr) with `SCM_RIGHTS`,
for example using `socket.send_fds`. Task `agent`, `review` and `data` require a third readable
stdin descriptor. CLI runs with `stdin: true` require a third readable regular-file descriptor,
validated as described in [the whole-run path](runs.md#runs-service).
Every other shape/profile takes exactly two. Send no descriptors for other operations. The helper
waits and returns `{"ok":true,"exit_code":N}`. All container output, including usage, goes
directly to those descriptors, never to the response or journal. `usage` has no network and
only the Codex login mounted read-only; its fixed in-container Python command scans session
JSONL files newest first and prints only the last `rate_limits` object with a primary or
secondary window from the first matching file, or `null`. It never runs host-side code from
the login or work tree.

`cli-run`, `bundle` and `usage` containers use `homelab-fence-run-<run_id>`; chat retains
`factory-session-turn-<turn>`, a 1200-second timeout and forced removal (124 on timeout,
125 on cleanup failure). Other Docker arguments for existing profiles equal the CLI fence's
builders, apart from the stipulated name and immutable image id. `status` returns only
`ok`, `running` and `exit_code` (null until known); `stop` forcibly removes the recorded
container and returns `{"ok":true}`. It refuses to acknowledge a launch it could not stop.
Run ids are single-use until reboot. Root-only 0600 records under `/run/homelab-launcher`
retain the profile/name/caller/exit code and current run id per name across service restarts,
with no command or content;
after reboot runs are unknown. Reusing a chat turn name never lets an older run id
inspect or stop the newer container. Root may inspect/stop any recorded run; other authorized
callers may only inspect/stop their own runs and allowed profiles. Refusals return
`{"ok":false,"error":"<fixed reason>"}`; no exception text is exposed. The journal carries
only uid, operation, profile, safe identifiers, exit codes and fixed reasons.

Rollback: `sudo systemctl disable --now homelab-launcher.socket`, then
`sudo systemctl stop homelab-launcher.service`. This prevents authorized callers from
launching runs. To roll back only a caller-list update, restore the previous `callers.json`
and restart `homelab-launcher.service` instead. Leave the group, inactive files,
image approval and existing fence installation in place for inspection. No volume or login
rollback is needed.

### Task launcher profile

The `task` profile reproduces `homelab-task-run`'s `container()` arguments, including entry
commands, for these shapes. The `runs` service calls it for every ticket attempt; the stopped
host task scripts start their containers directly. The execution service's uid is 21020, with
Factory's `cli-run`, `bundle` and `usage` permissions as well as `task`.

| Shape | Network | Work mounts | Fixed entry | Required `env` names |
|---|---|---|---|---|
| `prepare` | `homelab-packages` | `/work` rw, `.git` ro | `sh -c` | `HOMELAB_PI_VERSION` |
| `agent` | `homelab-tasks` | `/work` rw, `.git` and `.venv` ro | `sh -c` | `HOMELAB_TASK_TOKEN`, `HOMELAB_TASK_PURPOSE`, `HOMELAB_PI_MODELS`, `HOMELAB_PI_SETTINGS` |
| `review` | `homelab-tasks` | whole `/work` ro | `sh -c` | same four names as `agent` |
| `check` | `none` | `/work` rw, `.git` and `.venv` ro | `sh -c` | `UV_NO_SYNC`, `PYTHONPYCACHEPREFIX` |
| `commit` | `none` | `/work` rw, `.venv` ro, `.git` writable | entrypoint `/usr/bin/python3`, args `-I -c` | `GIT_AUTHOR_NAME`, `GIT_AUTHOR_EMAIL` |

Each request has exactly `op: "run"`, `profile: "task"`, `run_id`, `work_id`, `shape`, `env`
and `arguments`. Both ids are single identifiers under the rule above; `work_id` selects an
existing directory directly under `/srv/homelab/task-work`. All ancestors and `.git` must
be real directories; separately mounted `.venv` must be real too. The container uses the
work folder's uid/gid and refuses uid 0. Keep the work root and its ancestors root-controlled.
No request selects a host path or ownership. The launcher does not run or read work-copy code
on the host. Existing `own`/`remove` operations still address only Factory's roots.

`arguments` is a list of 1–256 strings appended after the shape's fixed entry command,
covering today's scripts, check command, repository-relative pins, commit specification and
Pi configuration values. These arguments execute only inside the selected container, and
cannot become Docker options or host bind sources. `env` is an object containing exactly the
shape's listed names. Each value/argument is a NUL-free UTF-8 string of at most 32768 bytes;
their combined size is at most 49152 bytes, within the unchanged 65536-byte wire limit.
Environment values reach Docker by the same bare `--env NAME` arguments as today's runner;
the Docker subprocess receives only a fixed host `PATH` plus those values, so missing names
cannot inherit host values. Prepare fixes `HTTPS_PROXY=http://package-proxy:8888` and
`UV_HTTP_TIMEOUT=120`; commit fixes container `PATH=/usr/bin:/bin`.

All shapes use the approved immutable id of `homelab-task:3`, `--pull never`, `--rm`, `--init`,
`--cap-drop ALL`, `no-new-privileges`, a read-only root, 8g memory/swap, 2 CPUs, 512 PIDs and
no Docker logs. `/tmp` and `/home/task` are 4g tmpfs mounts with today's exec/nosuid/nodev,
ownership and modes. Among these five shapes, only agent/review open stdin. Names are
`homelab-launcher-task-<run_id>`, separate from both existing runner and fence containers.
The caller owns the deadline and uses `stop`; the launcher adds no shape timeout. It forces
removal of any remaining task container after the Docker client exits or fails. Status,
single-use ids, caller ownership and content-free audit records follow the existing contract.

The clone shape additionally accepts optional `workspace: cli` for a launcher-created
`ticket-<id>-<attempt>` or `run-<uuid>` copy. Its optional `env` must contain exactly
`GIT_CONFIG_VALUE_0`, a bounded, newline-free `Authorization: Basic …` header. The launcher
fixes `GIT_CONFIG_COUNT=1` and the GitHub-scoped header key, forwards the value only through
the Docker environment, and disables redirects. With no `env`, public clone callers retain
their previous command and environment. Neither clone mode mounts logins.

After merge and the owner's go, wait for launcher clients to be idle and run from the node's
committed tree. Prepare rollback first: retain the three files from the previous committed
version and the current approval list. The base below is the version before this profile;
use the actual prior installed commit if it differs.

```bash
cd /srv/homelab/homelab-v2
git pull --ff-only
previous_commit=218d58fc8e32dd1998ff32c2ad44bd197a4b6b7e
rollback=$(mktemp -d)
git show "$previous_commit:node/usr/local/sbin/homelab-launcher" > "$rollback/homelab-launcher"
git show "$previous_commit:node/etc/homelab-launcher/profiles.json" > "$rollback/profiles.json"
git show "$previous_commit:node/etc/homelab-launcher/callers.json" > "$rollback/callers.json"
sudo cp -p /etc/homelab-launcher/images "$rollback/images"

sudo diff -u /usr/local/sbin/homelab-launcher node/usr/local/sbin/homelab-launcher
sudo diff -u /etc/homelab-launcher/profiles.json node/etc/homelab-launcher/profiles.json
sudo diff -u /etc/homelab-launcher/callers.json node/etc/homelab-launcher/callers.json
# Review all three diffs; diff exit 1 means differences, not an inspection error.
python3 -c 'import ast, pathlib; ast.parse(pathlib.Path("node/usr/local/sbin/homelab-launcher").read_text())'
python3 -m json.tool node/etc/homelab-launcher/profiles.json >/dev/null
python3 -m json.tool node/etc/homelab-launcher/callers.json >/dev/null
sudo systemd-analyze verify /etc/systemd/system/homelab-launcher.socket /etc/systemd/system/homelab-launcher.service

# Review and explicitly approve this already-built task image id, preserving fence approvals.
sudo docker image inspect --format '{{.Id}}' homelab-task:3
sudo sh -ec 'task_image_id=$(docker image inspect --format "{{.Id}}" homelab-task:3); grep -qxF "$task_image_id" /etc/homelab-launcher/images || printf "%s\n" "$task_image_id" >> /etc/homelab-launcher/images'
sudo systemctl stop homelab-launcher.service
sudo install -m 0755 -o root -g root node/usr/local/sbin/homelab-launcher /usr/local/sbin/homelab-launcher
sudo install -m 0644 -o root -g root node/etc/homelab-launcher/profiles.json node/etc/homelab-launcher/callers.json /etc/homelab-launcher/
# Restarting a socket alone does not reload an already-running helper's configuration.
sudo systemctl stop homelab-launcher.service
sudo systemctl restart homelab-launcher.socket
sudo python3 tests/launcher_task_probe.py
```

Require the original seven PASS lines, four ownership/clone PASS lines and three CLI PASS lines.
The CLI probes run the write checks as the `cli-run` command, checking writable defaults,
read-only work with writable run output, and bounded prompt stdin without a model call.
With `--private`, require a fifteenth private-clone PASS.
The ownership/clone checks verify
launcher-created ownership, the fixed proxied clone, input refusal and scratch cleanup.
The root orchestrator creates a disposable task-owned scratch
folder with empty `.git`/`.venv` directories. Its test container is uid 21020, offline,
with only the launcher socket and its group added, never the Docker socket. It starts a
harmless `check` shape that verifies its non-root user and read-only nested mounts, then
requires refusal of an off-list environment name, unknown shape, path in an identifier,
unknown profile and uid 21010 without `task`. It checks that each client and task container
is gone. `finally` removes only this invocation's random scratch folder and run metadata,
and attempts removal of all probe containers even on failure. It makes no model calls and
does not change callers, approvals, logins or existing work. Run the existing
`sudo homelab-check-fence --launcher` regression probe separately while clients are idle.

For rollback, first review the installed-file diffs against the saved previous committed files,
then reinstall the previous launcher and both JSON files together. Restore the saved image
approval list if this installation added its task approval; existing fence approvals remain.

```bash
sudo diff -u /usr/local/sbin/homelab-launcher "$rollback/homelab-launcher"
sudo diff -u /etc/homelab-launcher/profiles.json "$rollback/profiles.json"
sudo diff -u /etc/homelab-launcher/callers.json "$rollback/callers.json"
sudo install -m 0755 -o root -g root "$rollback/homelab-launcher" /usr/local/sbin/homelab-launcher
sudo install -m 0644 -o root -g root "$rollback/profiles.json" "$rollback/callers.json" /etc/homelab-launcher/
sudo diff -u /etc/homelab-launcher/images "$rollback/images"
sudo install -m 0600 -o root -g root "$rollback/images" /etc/homelab-launcher/images
sudo systemctl stop homelab-launcher.service
sudo systemctl restart homelab-launcher.socket
```

This withdraws `task` and uid 21020's permissions. The retained host task runner is disabled and available only for rollback until its removal;
no data volume, secret, login or network change is needed.
