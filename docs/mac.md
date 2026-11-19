# Mac worker

This document covers what runs on the owner's Apple Silicon Mac: the installed tools, the
llama.cpp model router behind `vision` and `embed-large-bulk`, usage profiles and the menu bar
app, the job queue, the action listener and vision measurements, with their install, update and
acceptance steps. Read it before changing anything under `mac/` or running a Mac step. The
interfaces other systems call are summarised in [contracts](contracts.md).

## Install and update the Mac tools

Run these commands outside the sandbox. Mac agents run a private installed copy under
`~/Library/Application Support/homelab/mac/` (directory `0700`), independent of the working
checkout. Install Homebrew Python at `/opt/homebrew/bin/python3` first (Python 3.11 or later).
This fixed interpreter is recorded in `install.json`; no agent uses the checkout's `.venv`.
The tools use only the standard library. Swift's SDK is needed to build the menu app.

Before installing or updating, check med-ask for active vision or bulk embedding requests,
as for a node deploy. A router code/preset update cuts `vision` and `embed-large-bulk` for a
few seconds. Wait for any running Mac job to finish; the installer refuses to unload an active
job worker. It never switches profiles or stops dictation.

```bash
cd ~/Code/homelab-v2
git switch main
git pull --ff-only
mac/install-mac-tools
export MAC_TOOLS="$HOME/Library/Application Support/homelab/mac"
```

The installer fetches `origin/main` and refuses dirty trees (including untracked files) or
commits not contained in `origin/main`. It copies only the commands, their runtime dependencies
and tracked menu sources. It rewrites existing listener, router, profile and profile-job plists
in place, preserving flags, calendars, job identities and each job's own command. Existing
loaded agents restart only when their paths or code/dependencies changed; unloaded agents stay
unloaded. It also builds/reinstalls the menu app when its code or recorded paths change.
A refresh waits up to 10 seconds for each unloaded service to disappear before loading its
replacement, then retries bootstrap exit 5 for up to 5 seconds. A failed refresh retains its
pending agents and can be retried with the same command, including after a manual recovery.
Private profiles, jobs and state stay under `~/.config/homelab/` and `~/.local/state/`.

For a first installation, after installing the privileged profile helper as described below:

```bash
"$MAC_TOOLS/homelab-router" start
"$MAC_TOOLS/homelab-listener" install
"$MAC_TOOLS/homelab-profile" install
```

Updating is the same pull-and-install command sequence above. To see the installed commit and
interpreter, and confirm the live command paths:

```bash
cat "$MAC_TOOLS/install.json"
for label in listener router profile profile.job.dictation-recheck menu; do
  plutil -p "$HOME/Library/LaunchAgents/org.homelab.$label.plist"
done
plutil -p "$HOME/Applications/HomeLab.app/Contents/Info.plist"
```

Listener/router/profile and all later `job add` agents use the installed command and recorded
Python. The menu agent runs `~/Applications/HomeLab.app/Contents/MacOS/HomeLabMenuBar`; its
bundle's `HomeLabProfileCommand` points to the installed profile command. Changing branches in
`~/Code/homelab-v2` cannot change these agents' code. The examples below assume `MAC_TOOLS`
was exported as above.

## Model router

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
The largest sampled Darwin process-family physical footprint plus the unique configured
GGUF/projector file sizes, rounded up once to MiB, fills `memory_mb`. The file charge covers
mapped weights excluded from physical footprint; it is a conservative budget allowance when
backends copy weights into charged allocations, rather than an exact GPU-wired counter;
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
footprint. The router itself installs no wake lock. Its startup bind probe uses `SO_REUSEADDR` so closed
health-check sockets in TIME_WAIT do not delay a restart; active listeners still block it.

### Mac vision serving

Vision and bulk embeddings share one native Homebrew llama.cpp router on the Mac. Use the
installed tools outside the Codex sandbox (which cannot see Metal):

```bash
"$MAC_TOOLS/homelab-router" start
"$MAC_TOOLS/homelab-router" status
"$MAC_TOOLS/homelab-router" stop
```

`start` installs a user LaunchAgent (`org.homelab.router`) and waits up to 120 seconds for
health. It runs at login and launchd restarts it after exit. It discovers the Tailscale IPv4
using the CLI (checking `/usr/local/bin` and `/opt/homebrew/bin` even under launchd's minimal
PATH) or an unambiguous `utun` address, binds only that address on TCP 8090, and
refuses an occupied port. The owner's existing node-only tailnet grant gates access without
an API key. No new port is needed; 8092 is freed. Allow incoming connections for `llama-server`
if macOS asks. No wake lock is installed. The action listener (8091) and dictation cleanup
server (8095) keep their independent services and lifecycles.

For an *on request* service, the native router loads the requested model on the first call.
With no requests, llama.cpp
unloads its model weights and KV cache after `IDLE_SECONDS` in `mac/homelab-router` (600 seconds
by default). `/models` then reports `sleeping` (or `unloaded` before the first request); a
sleeping child process remains and reloads on demand. Startup loads neither preset. The
initial preset template is `mac/router-models.ini.in`; migration copies its exact flags and
local paths into the private model registry. Current presets are rendered from the active policy. A private cache under router state keeps unrelated cached
models out of native discovery. llama.cpp owns model management; the usage profile command
can restrict which presets are available (see below).

`status` prints only the available preset names/states and current memory in bytes: router RSS,
router plus descendants' RSS, and their summed Darwin physical footprint (including Metal).
It calls `/health` and `/models`, which do not wake models. Compare the current family
physical footprint after inference with the same field after the idle interval; lifetime peak
memory cannot establish release. `status` exits 0 only for a healthy managed process and
checks PID start time. Output excludes addresses, paths and upstream arguments.
`stop` unloads the LaunchAgent, waits up to 30 seconds for the managed process, and removes
its plist so it stays stopped across login; `start` reinstalls it. Commands serialize with a
lock. State and generated presets are private under `~/.local/state/homelab-router/`; the
private plist is under `~/Library/LaunchAgents/`. Native content logging is disabled and
stdout/stderr go to `/dev/null`. Launcher failures append one fixed, content-free stage line
per failed invocation to `startup-failures.log` in router state (creation mode `0600`). For
example, `tailscale address unavailable` records discovery failure without CLI output,
exception text, addresses or paths. The record survives failed-start cleanup and `stop`;
launchd retries can produce additional lines. A failed health wait records a fixed timeout
line, covering native startup failures without retaining native output. No model download is
attempted.

Vision keeps the existing files under
`~/vision-bench-outputs/models/Qwen3-VL-8B-Instruct-GGUF/`:
`Qwen3-VL-8B-Instruct-Q4_K_M.gguf` and `mmproj-F16.gguf`. Its `vision` preset retains Metal
`-ngl 99`, context 16,384, one slot, flash attention and cache RAM 0. Temperature, completion
limit, prompt and page DPI belong to clients. Image inputs must be `data:` URLs.

The retired `mac/homelab-vision` and `mac/homelab-embed` commands only point here. Before
installing the router for the first time, stop both old processes with the previous launchers
(their `stop` commands check PID start time), then leave their old state unused.
The old vision state folder has been removed; retain `~/.local/state/homelab-embed/` because
it also contains historical download/compare files outside this slice. `start` refuses
any live legacy process. Model files stay in place. The vision pointer preserves address
discovery for the unchanged action listener.

Set **both** `HOMELAB_MAC_VISION_URL` and `HOMELAB_MAC_EMBED_URL` in the node clone's private,
root-owned `.env` to the **same** Mac Tailscale HTTP URL on port **8090**, ending in `/v1`.
Never put the address or tailnet name in git, logs or PRs. Empty or unset values retain
Compose's closed loopback default, so LiteLLM starts independently of the Mac. Both purposes
keep the 3-second connect limit and 300-second request limit, without retries, fallback or
stopped-Mac alerts. Cold model loading is included in the request limit.
The Mac profile agent publishes its offer through the router; the worker reads it every minute.
Run `homelab providers` to see the offer and its freshness.

### Mac bulk embedding serving

`embed-large-bulk` uses the same router described in [Mac vision serving](#mac-vision-serving).
The native preset name remains `Qwen3-Embedding-4B`, matching the gateway's upstream model.
Both node URLs point at port 8090; nothing listens on 8092 for bulk embeddings anymore.

Download the official GGUF once to `~/homelab-models/Qwen3-Embedding-4B-GGUF/` from
`https://huggingface.co/Qwen/Qwen3-Embedding-4B-GGUF/resolve/f4602530db1d980e16da9d7d3a70294cf5c190be/Qwen3-Embedding-4B-Q8_0.gguf`.
Verify SHA256 `b60ae5ce2dd6a0b77f82cadf21def1f310a3e10cde380ad0081b07a9d416949d`
before starting. Its preset preserves last-token pooling, L2 normalisation, context 8192,
batch/microbatch 2048, four threads and Metal `-ngl 99`. The purpose reports
`Qwen3-Embedding-4B` like `embed-large`, with identical optional `input_type` wording
(plain passages). Bulk ingest and node queries stay in the same vector space.

Before any stack restart, inspect med-ask processes and recent audit metadata:

```bash
ssh homelab-agent 'sudo docker ps --format "{{.Names}}"'
ssh homelab-agent 'sudo ps -eo pid,comm,cgroup | rg "med-ask|ingest"'
ssh homelab-agent 'sudo journalctl --since "10 minutes ago" -o cat | rg "model_call purpose=(vision|embed|embed-large|embed-large-bulk) " | tail -n 30'
```

Inspect the med-ask container's process names with `sudo docker top <container> -eo pid,comm`
and its ingest status without printing arguments or content. Recent continuous model calls
or a running ingest mean wait for completion; do not restart `homelab.service`.
Before merge, prove a cold synthetic-image vision call directly from the node, compare
20 generic sentence embeddings against the node's `embed-large` (lowest cosine at least
0.999), then record status and physical footprint before/after the full idle interval.
The direct probe uses the container's private vision URL for both models, before changing
node configuration:

```bash
ssh homelab-agent 'sudo docker exec -i homelab-litellm-1 python -' < tests/mac_router_probe.py
```

It requires both models initially unloaded or sleeping, sends only synthetic inputs, verifies
the real embedding model and dimensions, and prints timings/cosine without URLs or vectors.
Run `mac/homelab-router status` on the Mac immediately after the probe and again after at
least the idle interval with no inference calls; neither status call wakes a model.

After the approved merge and deployment, test vision and bulk through `homelab-models`
without starting a model by hand; stop the router and time both failures (about 3 seconds).
Restart the router outside the sandbox, then check `embed-large`, `embed`, `chat` and image
routes. Leave the router running.


## Usage profiles

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
refusal, `waiting`, `usage`, `schedule`), holding no profile rules. Its Profiles window reads through `profiles-show` and previews
validation, registry memory and budgets through `profiles-preview`; whole-document stdin saves
use `profiles-set`, preserving fields outside services and budgets. Current-profile service
edits reuse native preset rendering/reload and on-model loads without re-entering Inference;
other-profile edits wait for a switch. Validation precedes side effects, apply failures retain
the old profiles file and attempt service rollback, and paused Inference remains paused.
It is ad hoc signed into
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
Procrastinate jobs. When a queue command is running, a due scheduled job stays pending;
when that command releases its slot, pending scheduled work takes priority and the queue
waits until it completes. Calendar jobs otherwise retain their existing eligibility and
coalescing behaviour.

### Mac usage profiles

`mac/homelab-profile` is the command interface for usage profiles. Run it outside the
sandbox. `status` emits one JSON object: `profile` (`working`, `inference`, or `custom`),
`services`, `settings`, `apps`, `running_jobs`, `settings_skipped`, `return_pending`,
`paused` and `pause_reason`. A normal pause reports `profile: inference`, `paused: true` and
`pause_reason: on_battery`; manual differences can still make the observed profile custom.
It observes launchd, running apps, power settings and native router presets rather than
trusting the last selected profile. Starting/stopping a service or editing a setting makes
it `custom` unless it exactly matches a configured profile. Working excludes the owner’s
system settings from matching. The JSON `settings` power mode/sleep describe AC settings,
even while on battery; GPU limit is global. Model loading/sleep is activity,
not a profile change. An unavailable router reports its model services disallowed.

```bash
"$MAC_TOOLS/homelab-profile" install
"$MAC_TOOLS/homelab-profile" status
"$MAC_TOOLS/homelab-profile" switch working
"$MAC_TOOLS/homelab-profile" check-work
"$MAC_TOOLS/homelab-profile" switch inference
"$MAC_TOOLS/homelab-profile" start vision
"$MAC_TOOLS/homelab-profile" stop vision
"$MAC_TOOLS/homelab-profile" job list
"$MAC_TOOLS/homelab-profile" job add example --calendar '{"Hour":4,"Minute":0}' -- "$PWD/mac/profile-example-job"
"$MAC_TOOLS/homelab-profile" job run example
"$MAC_TOOLS/homelab-profile" job remove example
```

To change a registered job's command, remove it and add it again with the same calendar. Removal
refuses an unknown or currently running job and waits for its LaunchAgent to unload.

Installation creates a private `~/.config/homelab/profiles.json` with editable defaults and
`org.homelab.profile`, a user LaunchAgent that checks every 30 seconds. It does not switch
profiles. Install/start the router first. The repository's `mac/profiles.example.json` has an
empty placeholder app list; `install` defaults Inference's list to Chrome and VS Code bundle
identifiers. Keep the owner's edited app list and paths only in the private file, never git.
Profiles contain `services`, `close_apps` (bundle identifiers), `settings` and `jobs` (boolean).
`return_seconds` defaults to 900; `idle_seconds` for jobs defaults to 300.

Working allows bulk embeddings, cleanup and VoiceInk and closes no apps. Its `settings`
are empty: it applies no sleep or power-mode default and does not match the owner’s system
settings. The owner keeps separate choices for AC and Battery. Legacy Working power-mode,
sleep and GPU fields remain readable: they are checked against the numeric bounds, then
ignored in memory; explicit migration removes those obsolete Working settings. Vision and unregistered future image presets
are excluded. Inference permits both router purposes, stops dictation, politely quits the
configured apps, selects High Power and disables sleep **only for AC** (`pmset -c`), and
sets the global GPU wired limit to 45056 MB. Battery power mode/sleep and both sources’
display settings are untouched. Inference requires AC power: `pmset -g batt` is checked before any switching
side effect, with the clear refusal `Inference requires AC power; connect the charger before
switching`. The helper rechecks AC before applying Inference settings. Its power mode must
be 2 (High Power). Only an explicit owner `switch inference`, or the owner's
[night window](#menu-bar-app-usage-and-night-window), enters it. Before any side effect,
that command lists recognised `codex exec`, model servers and GPU jobs descending from
VS Code and refuses. Python and other possible GPU runtimes are conservatively included even
when their script names do not mention inference. `--confirm-kill` is the owner's explicit override. Never use that override
with the production app list from a chat terminal. Jobs inherit a marker that refuses entry
into Inference, even with the confirmation flag; worker ancestry also catches wrappers that
strip environment variables. Registered job executables are owner-trusted
code, not a sandbox for untrusted programs.

When every router model has remained unloaded/sleeping and no job has run for 15 minutes,
the LaunchAgent returns to Working, except while paused. A short unplug has no profile
effect. The existing 30-second check records the first battery observation; after 10 continuous
minutes on battery it pauses Inference, empties the router allow-list and unloads every model.
Apps and dictation stay closed. Starting a model service by hand is also refused while paused,
and no scheduled job starts. Sleep prevention and the GPU limit stay unchanged. Power-mode
changes on battery do not make the owner-entered Inference session custom.

Paused Inference waits for power or the owner; it never returns to Working merely because the
idle timeout elapses. Replugging resets the battery timer and automatically resumes the same
Inference session, applying High Power before restoring its allow-list. Resumption starts a
fresh 15-minute idle window without replacing the original app/service or settings record.
If replugging is observed before ten minutes, the grace timer resets; the ordinary idle rule
still applies during this grace period. The owner can `switch working` at any time, including
while paused on battery, to restore everything. If that return fails partway through, the
existing check retries Working restoration instead of resuming Inference on AC. A private,
atomic recovery record at `~/.local/state/homelab-profile/session.json` records apps and
services stopped by the switch, `previous_powermode`, `previous_sleep` (both AC), and
`previous_gpu_limit_mb` (global) before any change. AC values come from `pmset -g custom`,
not the active-source `pmset -g` view. The return reopens only those apps/services and restores
all three recorded values exactly. An ordinary Working switch with no pending return writes
no power settings; it only resets a nonzero orphan GPU limit to zero. The root helper reads the record while it is pending; it is cleared only after
settings and app/service restoration succeed. Apps/services already stopped remain stopped
(so the resulting profile may be `custom`). A failed/cancelled quit retains the recovery record; explicitly `switch working`
to recover. The command relaunches apps; window and tab restoration follows each app’s own
settings. In Chrome, choose “Continue where you left off” if desired. macOS’s “Close windows
when quitting an application” controls session restoration for apps using the system behaviour.
VS Code restores windows by default. The owner chooses these settings; the command changes
none of them and never force-quits an app. The listener on 8091, the cleanup plist and VoiceInk settings are untouched.

The active policy is private router `allowed.json`; rendering omits *off* services and unallowed
models. Native `/models?reload=1` reconciles presets and unloads removed or changed children.
An unchanged model keeps its process. A switch explicitly loads every allowed model of an *on*
service with native `/models/load` and verifies it is loaded; its preset disables idle sleep.
*On request* models retain 600-second native idle sleep. `load-on-startup` preserves *on* across
router restarts. Idle policy must be in presets, since router CLI flags override preset values.
Router capacity covers the complete registry (at least four), preventing LRU eviction of *on*
models. Restart the router after adding registry entries; registration is a separate operation.
This requires llama.cpp native aliases, reload and load APIs (tested against build 11429,
commit `d81235049`). No process supervisor or request rewriting is added. A mismatch fails the switch.
The gateway checks the native model list before Mac inference, using the existing private URL
and connection limit. A dedicated private `public/` directory under router state holds only
`homelab-profile.json`, served by llama.cpp’s native `--path` option on the existing router
address and port. It publishes exactly one boolean, `paused_on_battery`, never private profile
contents or paths. See the [native static-path option](https://github.com/ggml-org/llama.cpp/blob/master/tools/server/README.md).
The updated router must be restarted once to enable that path; do so outside the sandbox when
no ingest needs its models. This adds no listener or request proxy. A missing preset returns OpenAI-shaped HTTP 503 with, for example,
`vision is not available in this profile`. Its existing audit line adds
`profile_blocked=true`, with purpose and `upstream=false`, so waiting requests can be counted
without content. During battery pause the 503 instead says, for example,
`vision is paused because the Mac is on battery`, with `profile_paused=true`. Only the native
metadata boolean selects this fixed message. Missing/invalid metadata falls back to the normal
availability refusal; upstream bodies and diagnostics are never passed through. Other upstream errors retain sanitising. Clients retain their own queues;
requests never change profiles. A manual stop of a purpose has the same refusal wording.

Each registered job has its own launchd `StartCalendarInterval`. It runs only in an exact
profile permitting jobs, on AC power, after keyboard/mouse idle time. A missed run leaves one
pending bit; the next eligible idle check asks launchd to run it. Missed occurrences coalesce;
there is no work queue. Jobs execute under launchd, outside VS Code, with output discarded and
only PID identity, pending state and exit code recorded privately. The included example exits
successfully without doing work. `job run` also uses launchd and the same gates. Reinstalling
the main agent preserves job agents and state.

**One-time privileged installation, after merge, by the owner:** inspect the script and
sudoers template first. Verify `/usr/local` is root-owned and not writable by the owner.
The first command creates `/usr/local/sbin` if absent; verify it is root-owned and not writable
by the owner before installing the helper. Do not install this grant in a user-writable directory. From the merged
clone, typing the password:

```bash
sudo install -d -o root -g wheel -m 0755 /usr/local/sbin
sudo install -o root -g wheel -m 0755 mac/homelab-profile-apply /usr/local/sbin/homelab-profile-apply
PROFILE_SUDOERS=$(mktemp)
sed "s/@OWNER@/$(id -un)/g" mac/homelab-profile.sudoers.in > "$PROFILE_SUDOERS"
sudo visudo -cf "$PROFILE_SUDOERS"
sudo install -o root -g wheel -m 0440 "$PROFILE_SUDOERS" /private/etc/sudoers.d/homelab-profile
rm "$PROFILE_SUDOERS"
sudo visudo -c
```

The grant permits exactly `/usr/local/sbin/homelab-profile-apply working` and the same command
with `inference`, as root without a password. No other arguments, shell, arbitrary sysctl or
pmset command is granted. The root-owned script uses `/usr/bin/python3 -I` and fixed absolute
executables, no imports from the checkout and no shell. It reads only the invoking sudo user’s
private profile file and (for a pending Working return) `session.json`, rejecting final symlinks,
foreign ownership, nonregular/oversize files and nonnumeric settings. Inference always sets
High Power; a return accepts only recorded integer power modes 0–2, AC sleep 0–180 minutes
and GPU wired limits 0–45056 MB. Inference requires a complete pending recovery record
before writes. Ordinary Working does not apply configuration defaults; without a pending
return it only clears a nonzero orphan GPU limit. All pmset writes use `-c`; Battery settings
are never written. Zero GPU limit restores the system default; it also resets at reboot.
A partial system-setting failure retains the recovery record; returning to Working retries.
Older pending records containing only a power mode are refused, rather than inventing sleep
or GPU recovery values. Finish an existing return before updating the command/helper.
The sudoers file needs no change when reinstalling the corrected helper:

```bash
sudo install -o root -g wheel -m 0755 mac/homelab-profile-apply /usr/local/sbin/homelab-profile-apply
```

After reinstalling from the merged checkout, the orchestrator runs this **outside the sandbox**
as the owner, on AC, with Working services and no pending return or running jobs:

```bash
uv run python tests/mac_profile_privileged_acceptance.py
```

This uses the helper directly, never a production Inference switch or app quit. It checks
that the installed helper matches the checkout before any writes, pauses the profile agent,
saves the original private session and per-source settings, and creates a complete bounded
recovery record. `sudo -k -n` verifies both names without cached credentials or a prompt;
missing, unknown and extra arguments must fail. Readback uses `pmset -g custom` and sysctl:
Inference must select AC mode 2, AC sleep 0 and GPU 45056, leaving every Battery setting and
other AC setting unchanged. Working must restore the captured AC mode, AC sleep and GPU
limit exactly, with the full per-source readback identical to the original. The proof restores
private session state and reloads the agent in cleanup. A restoration failure retains the
recovery record and `~/.local/state/homelab-profile/privileged-acceptance-backup.json`, leaves
the profile agent paused, and must be relayed to the orchestrator. Never use a production
Inference switch from a chat terminal while VS Code must survive.

**Unprivileged acceptance test before merge:** save the private profile file first. Use only
TextEdit (`com.apple.TextEdit`) in the test Inference app list and copy the observed settings
into the test Inference profile, keeping Working settings empty and retaining Inference’s
fixed High Power requirement. Keep the Mac on the charger throughout the cycle. The test skips
settings; if the owner’s observed mode is not High Power, the inference phase correctly
reports `custom` while services and restoration are still verified. Set `return_seconds` to 15. Open
TextEdit with a disposable saved document, start VoiceInk and cleanup, then switch Working
with `--skip-settings`. From the orchestrator terminal, `check-work` and an unconfirmed
Inference switch must list the running chat and refuse without changing anything. After that
proof, only with the test app list verified, use `switch inference --skip-settings --confirm-kill`;
VS Code and Chrome are excluded. Check the stopped apps/services, wait for the automatic
return and confirm TextEdit, VoiceInk and cleanup relaunch. Acceptance checks app relaunch,
not document/window restoration; that follows the owner’s app and macOS settings.
Restore the private configuration afterwards; leave services in Working and report actual
settings (which may make the production profile `custom` until privileged installation).
`--skip-settings` persists across the automatic return and is reported explicitly in status.
To rerun only the cycle from production configuration, use
`uv run python tests/mac_profile_acceptance.py cycle`; it prepares the safe TextEdit-only test
configuration and restores the original configuration afterwards, without repeating the guard proof.
It verifies the captured prior mode and unchanged observed settings, quits TextEdit if the
test launched it, and removes its fixture. Before the first cycle using pause metadata,
restart the router outside the sandbox:

```bash
"$MAC_TOOLS/homelab-router" stop
"$MAC_TOOLS/homelab-router" start
uv run python tests/mac_profile_acceptance.py cycle
```

The cycle checks the native metadata path before changing test configuration. Unit tests cover
battery entry refusal, the full grace period, timer reset, pause/AC resume, suspended idle return
and manual Working restoration. Pinned gateway tests cover both purposes’ paused messages,
audit flags and invalid metadata. Actual privileged restoration is verified after owner installation.

#### Menu bar app, usage and night window

The SwiftUI `MenuBarExtra` app is a thin client of `mac/homelab-profile`: every state, plan,
guard and switch comes from the command's JSON, and the app holds no profile rules. Its menu
shows *Working*, *Inference* (or *Inference, paused* with the reason), or *Custom*, and each
service's state with a Start/Stop button (one service changes the state to *Custom*, as the
command does). *Working* is one click. *Inference…* first runs `plan-inference` and shows the
configured apps it would quit, the dictation it would stop and the VS Code guard's running work,
then asks once. A battery refusal is shown as such, without side effects. The app then runs an
unconfirmed `switch inference`; if the guard refuses, the app repeats the guard's warning and its
work list and offers **Switch anyway** (`--confirm-kill`) or **Cancel**. Never use *Switch
anyway* while a chat or job under VS Code must survive.

**Profiles…** opens a separate editor in the same system style. Select Working or Inference;
each service has a state picker (off/on request/on for router services, off/on for dictation).
Routed services show available registered models with measured MiB, allowed-model checkboxes,
and a default picker containing only allowed models. Removing the default selects the first
remaining allowed model; an empty allowed list is a validation error, including for off services.
The editable memory budget sits beside required memory, explicit unknowns and any warning.
**Over budget still saves.** Budget previews and validation come from `profiles-preview`;
**Save profiles** sends the whole document on stdin to `profiles-set`, preserving power settings,
closed apps, jobs and timings. Command errors appear in the editor and the draft stays available
for correction. Reload discards unsaved edits. Saving the current profile applies its services
without re-entering Inference or recapturing recovery settings; the other profile waits until
its next switch. Native reload can briefly interrupt requests with the existing 503 response.
No file paths, downloads or model registration controls appear in the editor.

After installing an update, the orchestrator can run `tests/mac_profiles_screen_acceptance.py`
with the installed Python, from exact Working with `Qwen3-Embedding-0.6B` already registered.
It exercises the editor’s stdin save path, verifies status plus native model ids/default aliases,
and restores the original profiles in `finally`, printing only pass/fail information.

**Build, install and start** (after installing the tools, outside any sandbox; it switches nothing):

```bash
"$MAC_TOOLS/install-menu-bar.sh"
```

It builds the installed `MenuBar` sources in release mode, replaces `~/Applications/HomeLab.app`, signs it ad hoc
(`codesign --sign -`) and loads the `org.homelab.menu` user LaunchAgent, which starts the app now
and at every login. A LaunchAgent works with an ad hoc signature; no Developer ID is needed. The
bundle's `Info.plist` records the installed `homelab-profile` and fixed interpreter, so the
app runs the same command as the `org.homelab.profile` agent. The tools installer refreshes
the app when needed; the command above explicitly reinstalls it. The menu bar icon shows
the state: laptop (*Working*), bolt
(*Inference*), pause (*paused*), sliders (*Custom*).

The commands it uses, all JSON on stdout:

```bash
"$MAC_TOOLS/homelab-profile" plan-inference      # apps/dictation to stop, guard work, AC refusal
"$MAC_TOOLS/homelab-profile" waiting             # refusals per purpose, last 24 hours
"$MAC_TOOLS/homelab-profile" usage               # Codex and Claude 5-hour/7-day usage
"$MAC_TOOLS/homelab-profile" schedule show
"$MAC_TOOLS/homelab-profile" schedule set --start 23:00 --end 07:00 --days mon,tue,wed,thu,fri
"$MAC_TOOLS/homelab-profile" schedule off
```

**Waiting work.** `waiting` runs two read-only commands over `ssh homelab-agent` (batch mode,
10-second connect limit): `journalctl CONTAINER_NAME=homelab-litellm-1 --since '24 hours ago'`
filtered to `profile_blocked=true` audit lines, and the public `GET /v1/model/info` from inside the LiteLLM
container (it publishes no host port). It counts `model_call` lines per purpose. An estimate is
count × the purpose's `seconds_per_image`; purposes publishing none (today `vision` and the
embeddings) show the count with “estimate unavailable”. Nothing is written on the node or
locally, and there is no queue. An empty journal query (exit 1) means no refusals. The container
selector excludes proof containers and sudo audit entries. If a node read fails, the command
and menu name the failing read without displaying its output.

**Usage, on menu opening and *Refresh*.** Opening the menu requests fresh account-wide
Claude and Codex limits once; reopening within 60 seconds reuses the reading. *Refresh* makes
another request, and a request already running is not duplicated. The two small CLI requests
run in parallel with 60-second limits in empty temporary directories. Each consumes a small
part of its five-hour window and starts a window if none is open. Claude's rate-limit event
supplies its figures; Codex's fresh session log supplies its figures. If either request fails,
the menu shows its error and falls back independently to the local reading, if available.

Plain `usage` makes no requests: Codex reads the newest complete five-hour and seven-day
`rate_limits` entry in `~/.codex/sessions/**/*.jsonl`, and Claude reads the status line's
`~/.claude/usage-now.json`. Missing readings are unknown. `usage --fresh` makes the requests
above. Each reading includes its own timestamp and age; request output and replies are not
included in the report.

**Night window.** Off by default. Set it in the menu (start, end, days, *Save*; the switch turns
it on or off) or with `schedule`; it is stored in private `~/.config/homelab/night-window.json`.
The existing `org.homelab.profile` agent (installed by `mac/homelab-profile install`) evaluates
each selected night once, within five minutes after its start time. It enters Inference only when
the observed state is exactly *Working*, the Mac is on AC and the VS Code guard finds no running
work; the switch itself repeats the AC, job and guard checks. Otherwise, including a Mac that was
asleep at the start time, that night is skipped and the menu shows the reason. Saving the window
while inside it never enters it that night. The usual 15-minute idle return and battery pause
still apply. At the end time the agent returns to Working only if the window entered the session
and no manual switch, Start/Stop or `schedule off` has happened since; those end its ownership.
Days name the night by its start day. Jobs and gateway requests still never enter Inference; a
job cannot set the window, and the window's entry is refused in job context.

**Turning it all off:**

```bash
"$MAC_TOOLS/homelab-profile" schedule off
launchctl bootout "gui/$(id -u)/org.homelab.menu"
rm "$HOME/Library/LaunchAgents/org.homelab.menu.plist"
rm -rf "$HOME/Applications/HomeLab.app"
```

The profile agent and its idle return and jobs keep working without the app. The app wrote
nothing for usage, so there is nothing else to remove.

### Per-profile service and model policy

Use the installed command for all profile file reads and writes. `profiles-show` emits the
resolved configuration. `profiles-set` accepts a complete version-2 JSON object on stdin,
validates the whole document before any side effect and writes atomically. Edits to the
currently observed profile’s services apply immediately through the switch’s native preset
renderer, reload and on-model loading; edits to the other profile apply at its next switch.
Custom state is left alone. An apply failure leaves the profiles file unchanged and attempts
to restore the previous services; if that restoration fails, the command asks for a switch
to the previous profile. Paused Inference keeps its router models paused until AC resumes it.
Settings, app lists and job fields edited through the CLI still take effect at the next switch. Both Working and
Inference must be present; their settings/app/job fields retain the existing rules.

```bash
"$MAC_TOOLS/homelab-profile" migrate
"$MAC_TOOLS/homelab-profile" profiles-show
"$MAC_TOOLS/homelab-profile" status
# Export privately, edit, then validate/save through the same command:
PROFILE_EDIT=$(mktemp)
chmod 600 "$PROFILE_EDIT"
"$MAC_TOOLS/homelab-profile" profiles-show > "$PROFILE_EDIT"
# Edit "$PROFILE_EDIT" with the owner's editor.
"$MAC_TOOLS/homelab-profile" profiles-set < "$PROFILE_EDIT"
rm "$PROFILE_EDIT"
```

`profiles.json` has `"version": 2`. Each profile's `services` changes from a list into a map
with exactly `vision`, `embed-large-bulk`, `cleanup`, and `voiceink`. A routed service looks
like this (ids are placeholders):

```json
{"state": "on request", "allowed": ["model-a", "model-b"], "default": "model-a"}
```

The states are `off`, `on request`, and `on`. Cleanup and VoiceInk accept only
`{"state": "off"}` or `{"state": "on"}`. Even an *off* routed service retains its nonempty
allowed list and default for later use. Lists must contain distinct registered ids for that
service, and the default must be in the list. Unknown services, states, models, versions,
missing fields and line-break injection are rejected.

A private sibling `~/.config/homelab/models.json` is the available-model registry:

```json
{
  "version": 1,
  "services": {
    "vision": {
      "model-a": {
        "model": "/private/example/model.gguf",
        "flags": {"mmproj": "/private/example/projector.gguf", "ngl": "99", "c": "16384"},
        "memory_mb": null
      }
    },
    "embed-large-bulk": {
      "model-b": {
        "model": "/private/example/embedding.gguf",
        "flags": {"embeddings": "true", "pooling": "last", "embd-normalize": "2"},
        "memory_mb": null
      }
    }
  }
}
```

Registry ids are unique ASCII letters/digits with dots, underscores or hyphens (1–96 chars).
Paths are absolute and single-line; only the supported model/preset flags are accepted, never
listener, logging or download flags. Memory is null or a positive measured integer in MiB; see
[registration and measurement](#registering-and-measuring-mac-models) below.

Migration is explicit and idempotent. It saves original bytes once as
`profiles.json.v1.bak` (`0600`), resolves old defaults, converts each allowed routed service
to *on request* and each disallowed one to *off*, and converts dictation services to *on/off*.
The registry starts with ids `vision` and `Qwen3-Embedding-4B`, their exact current paths and
flags from the template, and unknown memory. Each is its service's only allowed model and
default, including when off. App lists, job permissions and intervals survive. Legacy Working
power fields are validated then removed. No model is loaded by migration and no profile is
switched. The legacy running allow-list remains observable until the next switch; Working and
Inference preserve their current permissions and model behaviour.

A purpose request selects its default; `<purpose>-<id>` selects that allowed model. For example,
`vision-vision` and `embed-large-bulk-Qwen3-Embedding-4B` select the migrated models. Native
aliases share the same child with the purpose name. The legacy `Qwen3-Embedding-4B` upstream
name remains an alias for the embedding default. The gateway has two bounded native LiteLLM
patterns, `vision-*` and `embed-large-bulk-*`; a new registered model needs no gateway code or
configuration entry. Other names retain their existing refusal. Embedding responses retain
the real registered model id, and the default Qwen vector space and role prefixes stay intact.

`status` adds `profiles` (each profile's states, allowed models and defaults) and per-service
`configured_state`, `allowed_models`, `default`, and observed `models`. The existing `state`
remains the native default-model load state for the menu client. Sleeping is normal for *on
request*; an *on* service whose model is no longer loaded makes the observation custom.
Native aliases and idle settings are checked too. The existing `allowed`/`running` fields
remain compatible with the menu app. Manual `start` permits an on-request routed service;
`stop` makes it off in runtime policy without editing either saved profile.

After merging and installing from main, deploy the gateway only after the med-ask ingest
check, then run this outside the sandbox, on AC, starting from exact Working:

```bash
"$MAC_TOOLS/homelab-profile" migrate
"$MAC_TOOLS/homelab-profile" status
uv run python tests/mac_profile_models_acceptance.py cycle
"$MAC_TOOLS/homelab-profile" status
```

The acceptance script saves a private recovery file, reads/writes profiles through the installed
command, temporarily permits vision in Working, and checks both existing purposes **from the
node through LiteLLM** with a synthetic red image and embedding cosine ≥0.999 against the node.
It adds a second small-context embedding preset using the already-installed weights, makes
both models *on*, and verifies both are loaded immediately after the switch, before inference.
It then checks purpose/default and both explicit model names from the node and requires an
unallowed model to return the fixed 503. Finally it restores the real profiles, registry and
Working policy, even on a failed probe. It never changes apps or system-setting expectations.
If interrupted, run `uv run python tests/mac_profile_models_acceptance.py restore`; the private
backup is retained until restoration succeeds. Do not start an ingest during the cycle.

#### Registering and measuring Mac models

The router's startup port probe sets `SO_REUSEADDR`, matching the native listener, so
TIME_WAIT from its own closed health-check connections does not delay a stop/start.
An active listener still refuses startup; `SO_REUSEPORT` is not enabled.

The installed command downloads and test-loads a new model before making it available:

```bash
"$MAC_TOOLS/homelab-profile" model add embed-large-bulk MODEL_ID \
  --like EXISTING_ID --url https://huggingface.co/OWNER/REPO/resolve/main/MODEL.gguf
# Vision also requires --projector-url https://huggingface.co/OWNER/REPO/resolve/main/PROJECTOR.gguf
"$MAC_TOOLS/homelab-profile" model measure vision vision
"$MAC_TOOLS/homelab-profile" model measure embed-large-bulk Qwen3-Embedding-4B
"$MAC_TOOLS/homelab-profile" status
```

`--like` names an existing entry in the same service. Its exact preset flags are copied;
vision's projector path is replaced with the new download. Files go into a new `MODEL_ID/`
directory beside that entry's configured model file. The private registry supplies this path;
there is no checkout path, download directory or model URL embedded in the application.
Existing directories are never overwritten. Both files' published sizes are checked together
before downloading: less than 20 GiB remaining refuses the operation. Each file must have a
published SHA-256 in Hugging Face's linked ETag (or a SHA-256 ETag for a direct response).
Missing hashes stop registration; a mismatched hash/size removes the partial download.

The test load runs a separate llama-server on an ephemeral loopback port with those flags,
a temporary isolated cache and disabled content logging. It does not unload, reload or edit
any live router model. Once native health reports ready after warmup, the largest of three
current process-family Darwin physical-footprint samples, 200 ms apart, is added to the
configured GGUF and projector file sizes, then rounded up once to MiB and stored in
`memory_mb`. Each distinct file is charged once, including when paths are hard links.
Physical footprint includes charged Metal allocations but omits clean mapped weights; the
file charge accounts for those weights instead of treating them as free. This is conservative:
backends that copy weights into charged allocations can count some weights twice. It is a
budget allowance based on a real test load, not an exact resident-page or GPU-wired counter,
and does not guarantee memory needed by every later request. Test loading can need additional
memory alongside live models. Run measurements when the Mac is idle; a failed load registers
nothing. Its child is terminated on success, failure or interruption. `model measure` uses the
same load to update an existing entry, leaving its old measurement intact on failure.

The registry is still version 1 with the same entry fields; `memory_mb` now means accounted MiB
(the historical field name is retained). Registration writes it atomically under the profile
state lock, after re-reading and validating current entries. Network and test-load work happens
outside that lock. A failed operation removes only the new directory it created. A new entry
is available in `status.model_registry`, with its memory, **allowed in no profile**. No live
preset changes. The owner can later select it in profile policy. Restart the router after
registry additions before using them, so native capacity covers the full registry.

There is no remove command. To retire a model, first export profiles with `profiles-show`,
remove its id from every allowed list (select a different default), save with `profiles-set`,
and switch to the current desired profile through the normal command. Do not remove the last
available/allowed model for a service. Back up `~/.config/homelab/models.json` privately, edit
out the unused entry with the owner's editor, then run `profiles-show` to validate it and
`status` to confirm it is absent. Retain its downloaded files until the owner chooses to
remove them. Never remove an entry while a registration/measurement is in progress.

#### Profile memory budgets

Version-2 profiles accept an optional positive integer `memory_budget_mb` in MiB. Omitted
values resolve to 16384 for Working and 40960 for Inference. `migrate` explicitly adds these
defaults to existing version-2 files, preserving the original bytes once in
`profiles.json.budget.bak` with mode `0600`; repeat migration is a no-op. Version-1 migration
retains its existing backup and also includes the budgets. No version bump is needed for
this optional addition. For example (all ids and paths above are placeholders):

```json
{"memory_budget_mb": 16384, "services": {"vision": {"state": "on request", "allowed": ["model-a"], "default": "model-a"}}}
```

This fragment belongs inside a complete profile, retaining the other service/app/settings/job
fields. Use `profiles-show` and `profiles-set` for edits. The checked amount is the sum of all
allowed models of *on* services plus the largest allowed model across *on request* services;
*off* services contribute nothing. `status.memory_budgets` reports each profile's limit,
`required_mb`, `known_mb`, `unknown_models`, `over_budget` and `warning`. `required_mb` is null
when any included model has unknown memory. `known_mb` is then only a lower bound; it can
still prove a budget exceeded. No unknown model is guessed. Over-budget saves warn on stderr
but succeed, and status carries the same warning for the menu client. Budgets never block
switching or inference.

The gateway translates the Mac router's native `model name=… failed to load` error to HTTP
503: “The Mac could not load this model now (often for lack of memory); try later”. This is
an honest generic load failure, not a diagnosis of memory pressure. At llama.cpp commit
`d81235049`, child load failures share exit code 1; router HTTP errors do not distinguish
allocation failure from other causes. The translation adds no memory precheck, retry, proxy
or fallback. Existing transport behaviour is preserved: router retries are disabled, but the
pinned upstream SDK can repeat 5xx attempts before the failure hook runs. Unavailable-profile and battery-pause refusals retain their existing wording;
unrelated upstream failures retain the sanitised generic error. Deploy this gateway change
only after the orchestrator's med-ask ingest check.

The post-merge memory-error acceptance test uses a disposable native router with the same
presets, injecting `ENOMEM` only into native children's file mappings of at least 256 MiB.
It exercises the real native load failure without exhausting RAM or changing a GPU limit.
The normal login router is briefly unavailable; require exact Working, no running jobs,
unloaded/sleeping models and the med-ask clearance. It verifies both an injection marker and
the node's HTTP 503 answer, then restores the normal router in `finally`. Nothing is installed
in launchd for the test. If interrupted, rollback first:

```bash
uv run python tests/mac_model_memory_acceptance.py restore
# Only after clearance, run the test (it restores itself even on failure):
uv run python tests/mac_model_memory_acceptance.py cycle
"$MAC_TOOLS/homelab-profile" status
```

The shim and marker stay in private `~/.local/state/homelab-model-memory-acceptance/`;
`recovery.json` is removed only after the normal router starts again. This simulates an
allocation failure; it does not measure behaviour under actual system-wide pressure.


## Job queue

The Mac also has an explicit owner-operated FIFO of resumable absolute argv commands with
optional working directories (`homelab-profile queue add/list/play/stop/remove/top/requeue`).
The persistent `org.homelab.profile.queue` launchd agent uses the installed copy, refuses a
VS Code ancestor or a non-launchd parent, and starts one process group at a time. Queue state
is atomically replaced under the existing profile lock, before acknowledging commands.
A pipe gate permits exec only after the child identity is persisted; a crash before that
acknowledgement closes the gate without executing the command. It ignores profile jobs flags and idleness, never switches profiles, and counts a running
command as activity for idle return. Stop sends group SIGTERM, then SIGKILL after ten seconds,
puts the command first as waiting and turns play off. Battery pause uses the same stop but
keeps play on; continuous battery and AC observations each debounce for ten minutes. No
command starts on battery. `caffeinate -is -w` holds wake assertions only for a running command;
lid sleep remains available. Nonzero exits retry once immediately (scheduled work still has
priority), then retain a failed record and exit code. Finished records remain until removal.
`top` reorders waiting work; `requeue` resets failed or stopped attempts and appends the command.

This small Mac-only list deliberately uses no node Postgres or queue dependency. Its private
state directory holds 0600 per-command logs until removal; this owner-requested command output
is an explicit exception to default content-free logging. Only the last five log lines of a
twice-failed job leave the Mac, in an owner-requested private Telegram notice with its name
and exit code. The other notice is "Mac queue empty;
the Mac is free", emitted when a completion leaves no waiting or running jobs. Stops, battery
pauses and intermediate successes are silent. After persisting the completion, the worker
launches a separate sender outside the lock with a ten-second SSH timeout; failed sends log
only a fixed failure line and are dropped, never replayed. The root-owned
`homelab-queue-notice` SSH command uses a transient systemd service as `homelab-bot`, with the
existing TPM-sealed `LoadCredentialEncrypted` and `homelab-notify.sh` send path. Stdin text
has controls stripped, is prefixed "Mac queue: " and capped at 1,000 characters including
the prefix. Notice text is never stored on the node or in unit arguments. No token, privilege
or listener is added.
Commands receive `HOMELAB_QUEUE_PROGRESS`, naming an optional one-line progress file. The queue JSON
in status includes play, battery pause, running metadata/progress/log path, summed process-group
physical footprint (`physical_footprint_mb`, including charged Metal allocations but excluding
clean mapped model-file pages) and available memory (`free_memory_mb`) in MiB, plus ordered
waiting, failed and finished records. Available memory is total RAM times the whole-percent
free figure from `memory_pressure -Q`, including immediately reclaimable memory. Failed
measurements are null. The menu shows this queue between services and refused requests,
refreshes status every five seconds while open, and sends play/stop/remove/top/requeue through
the same command, reloading status after each action. On worker
restart an interrupted attempt is stopped and returned to waiting before any new work starts.
The deployment hook keeps each purpose's 3-second connect and 300-second request limits,
without router retries, fallback or stopped-Mac alerts. The pinned upstream SDK can still
repeat 5xx attempts before the failure hook; this transport behaviour is unchanged.

## Mac action listener

The owner's launchd user agent accepts only `POST /backup` with an empty body on TCP 8091,
bound to the Mac's runtime Tailscale IPv4. Extend the tailnet grant so only the node can reach
this port before installing it. There is no listener key; the tailnet rule is the gate.
The agent runs as the logged-in owner, using the same SSH key and `homelab-agent` configuration
as the manual copy. Install the Mac tools first; the listener and its copy dependency run
from the installed directory with the recorded interpreter:

```bash
"$MAC_TOOLS/homelab-listener" install
"$MAC_TOOLS/homelab-listener" status
"$MAC_TOOLS/homelab-listener" stop
"$MAC_TOOLS/homelab-listener" start
"$MAC_TOOLS/homelab-listener" uninstall
```

Install writes `~/Library/LaunchAgents/org.homelab.listener.plist` with mode `0600`. launchd
starts it at login and restarts it after exit; `stop` unloads it until `start` or the next login.
Uninstall removes the plist and unloads the agent, retaining private logs in
`~/.local/state/homelab-listener/listener.log` (directory `0700`, creation umask `0077`).
`status` reports the launchd process state; confirm its actual binding with
`lsof -nP -iTCP:8091 -sTCP:LISTEN`. Only the Tailscale address must appear. Tailscale being down
prevents binding; launchd retries. The agent does not keep the Mac awake.
Click Allow if macOS asks about removable-volume access or incoming connections. If an Allow
click cannot resolve volume access, stop and investigate before changing the security setup.

Requests carry no arguments, paths or commands; other actions or request bodies receive 404.
One copy runs at a time; concurrent requests receive 409 (`busy`). The listener checks for
`/Volumes/SD Card`, runs the existing copy command with a 180-second timeout, and returns JSON
status `no-card`, `done` (file name and bytes only), or `failed` (fixed short reason).
A timeout kills the copy and its SSH child; partial files remain and are never overwritten.
Default HTTP access logging is disabled; the private log contains result and byte metadata.
No copy content passes through this channel.

After merging, install the changed bot files and reminder from the node clone after showing
`sudo diff`, using the modes in [Installing host files](host-files.md#installed-modes). Configure the private listener URL as described in
[Rebuilding the node](operations.md#rebuilding-the-node), then restart only
`homelab-telegram-bot.service`. Validate `/backup` with the card ejected, inserted, and the
agent stopped; restart the agent afterwards. Verify the card file and
`sudo homelab-copy status` timestamp/bytes on the node. From the node, an empty POST to an
unknown listener path must return 404. The privileged router gate also applies to `/backup`.

## Mac vision measurements

`mac/vision_bench.py` benchmarks native Metal llama.cpp on macOS. It starts each supplied
GGUF with its projector on `127.0.0.1`, measures launch-to-health readiness and Darwin's
lifetime peak physical footprint plus the GGUF and projector file sizes, then stops it,
including on interruption. This sum estimates memory including mapped weights; it may
double-count resident mappings and is not a direct resident-set measurement. No service is
installed and no node or model route is changed. Install `brew install llama.cpp`, then run:

```bash
uv run --group benchmark python mac/vision_bench.py --help
uv run --group benchmark python mac/vision_bench.py \
  --model '<NAME>' '<WEIGHTS-GGUF>' '<MMPROJ-GGUF>' \
  --sample '<PDF-OUTSIDE-GIT>' '<ONE-BASED-PAGE>' en prose score \
  --results-dir "$HOME/vision-bench-outputs/numbers" \
  --outputs-dir "$HOME/vision-bench-outputs"
```

Repeat `--model` and `--sample` for identical samples across sequential candidates.
Languages are `en`/`es`; kinds are `prose`/`table`/`figure`/`scan`; modes are `score`/`judge`.
Defaults: 150 DPI, 16384 context tokens, 4096 completion tokens, temperature/seed zero,
no prompt cache. Page timings cover HTTP image prefill and generation; rendering is separate.
The default prompt and figure descriptions remain available. Use `--prompt exact` for the
verbatim transcription prompt and `--no-figures` to omit figure descriptions. Reconciliation
uses `--reconcile '<OUTPUT-A-DIRECTORY>' '<OUTPUT-B-DIRECTORY>' --dpi 300`: each indexed
`NNN.text.txt` pair and one image go into one chat request with the fixed reconciliation prompt.
Missing/empty inputs record a page failure. Supply identical ordered samples for both inputs.
Use `--run-name v2-r1` (one model per invocation) to name both the output subdirectory and
numeric JSON file; existing result files or output directories are refused.

Normalisation applies Unicode NFKC, removes soft hyphens, joins words hyphenated at line ends,
casefolds and collapses whitespace. Word bags use whitespace tokens, retaining punctuation
and repeated-word counts. Primary recall, precision and F1 aggregate matched token counts over
all valid reference/output tokens, independent of reading order. CER/WER remain secondary
Levenshtein scores after the same normalisation. References are `invalid_reference` when
fewer than 80% of whitespace tokens are alphabetic after stripping Unicode punctuation
(an empty reference is invalid). Invalid references are reported and excluded from accuracy
aggregates; transcription still runs for visual review. Judgement pages have no accuracy score.
Failure codes: 0 success, 1 request/render/input error, 3 truncated/non-stop completion,
4 server startup failure. Startup failures record every sample. Latency includes failures;
token means include successful calls only. Results report total invocation duration, cold load,
file-size bytes, peak footprint bytes and their sum as `peak_memory_bytes`.

Results contain numeric measurements only (language codes: en=0/es=1; kind codes:
prose=0/table=1/figure=2/scan=3), with sample indices instead of paths or page numbers.
Optional outputs retain images, reference text, transcriptions and figure descriptions for
local review. All inputs and output directories must be outside every Git repository;
nothing the benchmark reads or writes is committed. Keep private sample arguments locally.
PDF rendering and scoring dependencies live only in the `benchmark` group, outside app images.
Cold load means a fresh process, without evicting the operating system's file cache.
