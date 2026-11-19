# Telegram bot

This document covers the host Telegram bot: its commands, the planning mirror and notices,
free-text conversations and order proposals, the inbox and its SSH helper, the order endpoint
on TCP 8110 and the `/backup` channel to the Mac, with their install, check and rollback steps.
Read it before changing `node/opt/homelab-telegram-bot/`, `homelab-inbox` or the bot's units.
Its provisioning during a rebuild is step 10 of [Rebuilding the node](operations.md#rebuilding-the-node).

## Behaviour

The Telegram client is a host service using python-telegram-bot, running as `homelab-bot`
with its existing sandbox and TPM-sealed token. Its own `/opt/homelab-telegram-bot/.venv`
is built with host Python from hash-pinned requirements; the library owns Telegram transport,
polling and registration. It long-polls Telegram; its order listener is described below. The main
allowlist gates dispatch; the privileged allowlist is a subset and gates `/backup` and
`/restart`, which also checks its unit allowlist. Polkit independently permits only restarting `chrony.service`.
`/ask` posts to the loopback answer API, returns only text and never dispatches model output.
Host metrics, restart and help do not depend on the knowledge service or unlocked volume.
Answers include numbered source titles, refusals omit sources, and replies are split within
Telegram's length limit. The router also supplies startup `setMyCommands` and `setMyDescription`;
registration failures are nonfatal.
User IDs remain audit metadata in the node's journal; `/ask` logs its argument length only.
Handler errors log the registered command and exception type, never exception text.

An optional private `planning.json` enables a read-only Git mirror and outbox notices.
Git runs as a bounded subprocess, with SSH restricted to a root-owned deploy key and pinned
GitHub host key under `/etc/homelab-telegram-bot/`; that key grants no write access. The clone
and durable outbox marker live only under the bot-owned `/srv/homelab/frontline/` on the
encrypted volume. The sole new sandbox write exception is that directory. A new
`homelab-bot-data-ready.service`, wanted by and part of `homelab-data.target`, restarts the
boot-independent bot after unlock to refresh its mount namespace. On target stop, it stops
the bot to release private volume binds; `homelab-bot-data-released.service` waits for the
host unmount and starts the bot against the locked root filesystem. The bot is briefly
unavailable during lock. If unlock is retried first, its hook owns the restart. Existing
unlock units and script stay unchanged. The bot queries PID 1's data-target state before accessing planning
data, including after a lock when its namespace might retain a mount.

The mirror pulls about once a minute and before `/pending`, `/recap [hours]` and `/slice <id>`.
These are read capabilities behind the existing main allowlist; they use no model and issue
no orders. Missing configuration disables planning without stopping the bot. While locked,
commands report the locked volume and notices wait. The first successful outbox read creates
a marker at its current end without replay. New events go to each allowlisted user, with
notice text cut to 600 characters and a `/slice` pointer. Each confirmed recipient send is
followed immediately by an atomic, flushed marker update. Uncertain deliveries retry, so a
crash or network ambiguity can cause a rare duplicate; notices are never deliberately skipped.
Git output, configuration values, arguments and event content stay out of the journal.

Allowlisted free text asks the `frontline` purpose over loopback, using the configured
`context_files` plus the computed pending summary. One optional second pass can read a
named regular UTF-8 file inside the clone (128 KiB maximum); paths cannot escape the clone
or expose Git internals. The model can propose an order but cannot execute or write.
Context is capped at 256 KiB,
each completion at 4,000 tokens, and each question at two calls with 60-second HTTP timeouts.
Only the active episode's question/answer pairs are history. Episodes live in bounded memory,
close on a changed topic, command or 30-minute silence, and reopen on an explicit reply to
a retained bot answer. Restarting the bot clears them. Logs contain only success/failure metadata.
Locked volumes or unavailable models get a short availability reply; commands remain independent.
`frontline` has the same gateway route and low reasoning effort as `chat`. Zero data retention
was requested but the current Hobby team rejected it with HTTP 403; the option remains off.

The bot validates one structured order proposal against the current mirror before showing
Yes/No buttons. Only an allowlisted callback queues it; either choice closes the episode.
Answers are verbatim excerpts of the owner's current message. Merge approval requires an
orchestrator review for every slice and displays its full verdict and PR. Orders record the
slice's state, paused flag and attention kind/since; every successful mirror pull reconciles
open orders, including attention already answered or approved. Changed expectations or lost
preconditions mark an order overtaken and remove its buttons. The model's output never enters
the command router or an executor.

The model classifies free text as a question, order or something to keep. Keeping uses a
separate clarification-only call, with at most two short questions about intent, relationship
or time constraints. “save” forces keeping; “just save it” persists known fields immediately.
“new” starts a fresh episode. Persisting removes that episode and its reply mappings so stored
items never become subsequent conversation context. Pending drafts remain memory-only.

The inbox lives at `/srv/homelab/frontline/inbox/`, beside orders and outside the clone.
Each opaque id directory atomically publishes `item.json` and, for a file, `file/<safe-name>`;
files and directories are flushed before acknowledgement. Attachments are opaque bytes through
python-telegram-bot, with 20 MiB per-file and 2 GiB aggregate admission caps, checked again at
commit. Nothing opens, transcribes or interprets them. `/inbox [id]` lists the latest ten or
shows one record. Reads and writes require the data target active, independently of the model.

A standard-library link fetch admits HTTP(S) public destinations only. All resolved addresses
must be public; the socket connects to a checked numeric address, preserving the hostname for
TLS/SNI and Host. Each of at most three redirects repeats the checks. Loopback, private,
link-local, CGNAT, multicast, reserved, unspecified and IPv6 transition/site-local destinations
are refused. Fetches have a five-second deadline and 1 MiB cap, with HTML/plain text only.
Raw page text reaches only an isolated summarizing call with no action path. Its bounded
summary/title can inform the clarification-only call, never an order-capable call. Failure
keeps the URL with null metadata and tells the owner. Orchestrator messages validate target
against state `repo` names or `development`; delivery is future work. No route, dependency,
sandbox, credential, allowlist or order contract changes. Inbox logs contain only metadata.

`orders.json` lives beside the clone under `/srv/homelab/frontline/`, with atomic replacement
and file/directory fsync before acknowledgements. Telegram message edits are durable pending
work and retry after failures or restart. Closed orders are retained; no automatic deletion
is introduced. Only order id, action, slice id and status enter order audit lines.
A standard-library HTTP server hands out queued orders oldest first and accepts idempotent
receipts; it runs no orders. It binds only the node's runtime Tailscale IPv4 on TCP 8110,
retrying when Tailscale is absent, with no fallback address. Before unlock the two routes
return 503. It has no key: the owner's Tailscale policy gates tailnet access, and ufw accepts
TCP 8110 from `homelab_default` (`172.18.0.0/16`) so platform containers can reach the same
Tailscale address. The node already permits tailscale0 traffic. This is a host listener, leaving the
container API boundary unchanged. Existing AF_INET and writable-directory permissions suffice;
no sandbox directives, credentials, polkit rules or allowlists change. The HTTP contract is in
[operations](#bot-order-endpoint); the Mac runner is built separately.

## Operating the bot

The Telegram bot is a host service using python-telegram-bot, running as `homelab-bot`.
It uses `/opt/homelab-telegram-bot/.venv/bin/python` with hash-pinned requirements, keeps
its existing systemd sandbox and long polling, and needs no Docker. Its order listener
binds only the runtime Tailscale IPv4 on TCP 8110 (see [the contract](#bot-order-endpoint)).
Its code lives at
`node/opt/homelab-telegram-bot/`, installed at `/opt/homelab-telegram-bot/`. `/ask <question>`
calls the loopback answer endpoint and replies with the answer and numbered source titles;
refusals have no sources. Long replies split at paragraph boundaries within Telegram’s UTF-16 limit; oversized
paragraphs fall back to sentence, line or word boundaries, then characters. When the volume
is locked or the API is unavailable, `/ask` reports that the knowledge service is down.
`/status`, `/disk`, `/uptime`, `/restart`, `/backup`, `/help` and `/start` keep working independently.
`/spend` and `/model` are no longer commands. Startup registers the router's command list and
description with Telegram; registration failure is logged and polling continues.

Optional planning reads use `/pending` (needs attention, running, scheduled, ready; planned
only as a count), `/recap [hours]` (default 24, positive hours, grouped by
slice), and `/slice <id>` (all fields including the full attention body, displayed as labeled
paragraphs rather than escaped JSON). They share `/status`'s
main allowlist and use no model. Accepted slices are omitted from pending; unresolved attention
and review/escalated states take precedence over running, slots and ready. Scheduled entries
sort by slot. Configuration is read at startup from private
`/etc/homelab-telegram-bot/planning.json`; see the provisioning steps under
[Rebuilding the node](operations.md#rebuilding-the-node). An absent file logs
`planning not configured; feature disabled` once and leaves existing commands working.

Allowlisted free text is classified as a planning question, an order or something to keep. The bot calls the `frontline` purpose at
`http://127.0.0.1:4000/v1`, sending the private `context_files`, computed pending summary and slice fields except `log`; attention bodies are capped at 2,000 characters.
The optional `context_files` list defaults to empty; the orchestrator supplies the owner's
summary-file paths in the private config, never in this repository. A question may request
one additional named clone file through a JSON second pass. Reads accept regular UTF-8 text
only, at most 128 KiB per file; absolute paths, parent traversal, Git internals, escaping
symlinks and binary files are refused. Base context is capped at 256 KiB. The model may
propose exactly one order for Yes/No confirmation; it has no command-dispatch capability,
execution or writes. Each completion is capped at 4,000 tokens,
with at most two 60-second HTTP calls per question and no retries.

Episodes are memory-only, scoped to chat and allowlisted user. Follow-up pronouns/openings
or shared topic words retain the episode; other questions start fresh. A command closes it,
as does 30 minutes of silence. Replying to any part of a retained bot answer explicitly
reopens its episode. Only that episode's messages accompany a question. Memory retains up
to 128 episodes and 1,024 answer-message mappings; a restart clears them. Long episodes
close after 16 exchanges or 48,000 characters; explicit reopening keeps only that bounded
recent history. Locked/unavailable answers say that the node
must be unlocked and the gateway up, and logs never include questions, answers or file text.
The model base URL and arbitrary client key use `HOMELAB_MODELS_BASE_URL` and
`HOMELAB_MODELS_API_KEY` if set (defaults are loopback and a nonsecret client label).
No unit sandbox or credential changes are needed.

Zero data retention is **off** for `frontline`: a synthetic request with
`providerOptions: {"gateway": {"zeroDataRetention": true}}` returned HTTP 403 because the
current Vercel team is on Hobby; it requires Pro or Enterprise. No account or billing change
was made. `frontline` otherwise shares `chat`'s model and low effort.

The bot pulls its read-only mirror about once a minute and on demand for these commands.
The mirror and outbox marker live under `/srv/homelab/frontline/`. While the data target is
inactive the new commands report a locked volume, notices wait, and no planning directory is
created. `homelab-bot-data-ready.service` restarts the bot automatically after unlock to refresh
its mount namespace. On target stop, the hook stops the bot to release the private writable
bind that would otherwise pin the encrypted filesystem. Its
`homelab-bot-data-released.service` helper waits for host unmount, then starts the bot again
against the locked root filesystem. Expect a brief interruption during lock. A retried
unlock takes over the restart. Neither the unlock script nor existing data units change.
The bot still starts at boot; its order endpoint returns 503 while locked. If lock fails
with the volume still mounted, the release helper keeps waiting: retry the owner lock/unlock operation. To restore
status while intentionally leaving the target inactive and the volume mounted, start
`homelab-telegram-bot.service` manually; stop it again before retrying unmount.

On the first successful read, the marker starts at the outbox's current end, with no historical
notices. Subsequent events are sent to every main-allowlist recipient; nothing is sent when
there is nothing new. Notice text is cut to 600 characters and points to `/slice <id>`;
`/recap` returns event text and `/slice` returns the full attention body. After each confirmed
recipient send the bot atomically saves and flushes its line/recipient marker. Uncertain
sends retry: a crash or network ambiguity before marker persistence can produce a rare
duplicate. Never delete the marker to retry a notice: a missing marker intentionally skips
to the current end. A bad marker or truncated outbox fails closed with a metadata-only warning.
Sync failures do not advance it; commands report failure rather than returning stale state.
The journal contains fixed sync outcomes, line/recipient counts and failure metadata, never
Git output, repository settings or event text.

### Bot inbox

Send an idea, a link, or a message to keep for an orchestrator; prefix with `save` to force
keeping. The bot may ask at most two short questions, only about missing intent (explore,
build, read later, discuss), project/backlog relationship, or time constraints. It suggests
relationships for confirmation and never designs or solves kept ideas. `just save it` during
clarification saves with what is known without another model call. `new` starts a fresh episode.
A stored episode and its Telegram reply mappings are removed. Restarting loses unfinished
drafts; committed items survive. Attachments are always kept immediately, with their caption.
Before unlock, keeping and `/inbox` reply that the inbox needs the node unlocked. Commands
continue to work. `/inbox` lists the latest ten records (id, kind, short title, status);
`/inbox <32-character hex id>` shows every stored field without opening an attachment.

The encrypted-volume layout shared with the Mac orchestrators is:

```text
/srv/homelab/frontline/
  clone/                         # read-only planning mirror; never an inbox destination
  orders.json                    # unchanged order contract
  inbox/
    <32-character lowercase hex id>/
      item.json
      file/<sanitised-name>       # only for kind=file; opaque original bytes
```

`item.json` is one UTF-8 JSON object: `id`, `created` (UTC ISO timestamp), `kind`
(`idea`, `link`, `file`, `message`), `text` (initial owner text/caption verbatim), `intent`,
`relates_to`, `due` (free text or null), `target` (message repository from state `repo` or
`development`, otherwise null), and `status` (`new`, or `read` after an orchestrator receipt).
`read_at` is added as a UTC ISO timestamp on the first receipt. Missing intent, relationship
and due are null. Text items also keep `clarifications`, a list of subsequent verbatim owner replies.
Links add `url`, `title` and `summary` (null on fetch/summary failure). Files add `name`
(sanitised), `type` (Telegram MIME type or an opaque default), `size` (actual bytes) and `file`
(the relative `file/<name>` path). Only read these fields as data; no consumer runs content.
The Mac orchestrators read through the root-owned `homelab-inbox` helper over existing SSH;
there is no inbox network endpoint or automatic collector. `/inbox` shows the updated status.

#### Inbox helper over SSH

From the Mac, use the existing agent account and passwordless root sudo:

```bash
ssh homelab-agent sudo -n homelab-inbox list development
ssh homelab-agent sudo -n homelab-inbox list med-ask
ssh homelab-agent sudo -n homelab-inbox file <id> > attachment
ssh homelab-agent sudo -n homelab-inbox done <id> [<id> ...]
```

`list` prints one JSON array of new items, oldest first (id breaks timestamp ties).
`development` receives every idea, link and file, plus messages addressed to development.
Other targets receive only messages addressed to that repository. Targets match
`[a-z0-9][a-z0-9._-]{0,63}`; ids are exactly 32 lowercase hexadecimal characters.
`file` streams only the attachment bytes unchanged to stdout; no attachment is an error.
Treat all returned content as data. The orchestrator calls `done` only after reading an item.
It atomically replaces the record with status `read` and `read_at`, preserving owner, group
and mode and flushing the file and directory. Repeated receipts leave read records unchanged.
Concurrent receipts serialize per item; a multi-id command commits each item independently,
so an error on a later id does not undo earlier receipts. Retry is safe.

Exit codes are `0` for success, `2` for usage, unknown ids or rejected/failed item operations,
and `3` for an absent or unreadable inbox (locked volume), with exactly `locked` on stderr.
Errors never include item content. The fixed-path, standard-library helper runs with
`/usr/bin/python3 -I`, opens every directory component without following symlinks, and rejects
symlinked items, records or attachments and non-regular or multiply linked files. Listing and
receipts never open attachment files. An empty inbox must exist to return `[]`; an absent
folder returns locked, without creating it. No sudoers grant, unit or listener is added.

Install from the node's committed, merged main checkout as described in
[Rebuilding the node](operations.md#rebuilding-the-node). Before copying, show the installed-file diff
(use `/dev/null` for a first installation). If replacing an existing helper, save it in a
dated root-only rollback directory first. Compile without writing bytecode, then install:

```bash
sudo diff -u /usr/local/sbin/homelab-inbox node/usr/local/sbin/homelab-inbox
/usr/bin/python3 -I -c 'from pathlib import Path; p = Path("node/usr/local/sbin/homelab-inbox"); compile(p.read_bytes(), str(p), "exec")'
sudo install -m 0755 -o root -g root node/usr/local/sbin/homelab-inbox /usr/local/sbin/homelab-inbox
```

Check both `list` targets from the Mac (an empty inbox returns `[]`) and verify `file` and
`done` for an unknown id exit `2`. Do not mark real items during installation checks. The
owner's new-item check belongs to the reading orchestrator. Roll back by restoring the saved
helper as `root:root` `0755`, or removing only the helper on a first installation; keep all
inbox data and receipts. No service restart or daemon reload is needed.

The bot owns directories (`0700`) and records/files (`0600`); service umask is `0077`.
Publication stages an entire record in `.item-*`, flushes files and directories, then renames
to its id and flushes the parent before acknowledgement. Readers ignore dot directories, which
can remain after power loss. There is no automatic retention/deletion. Keep this directory
when updating or rolling back; it is outside the current `*-data/originals/` weekly-copy scope.

Photos, documents, voice notes, audio and video are downloaded by python-telegram-bot and
stored unchanged. No file is parsed, transcribed, previewed or sent to a model. Admission
checks the declared sizes and actual bytes against **20 MiB** (Telegram's 20 MB file limit).
It refuses new files at **2 GiB** total, also refusing a file that would cross the cap.
The final size/quota check runs under the store lock. Text/link items remain available at the
file cap. The owner coordinates any future cleanup; do not delete files to retry a send.

Links allow only HTTP(S), verified TLS, public DNS results and checked numeric connections.
Every address returned by DNS must pass, including on each of at most three redirects.
Loopback, private, link-local, CGNAT/tailnet, multicast, reserved and unspecified IPv4/IPv6,
plus IPv6 site-local/transition destinations, are refused. About five seconds and **1 MiB**
bound the fetch; only uncompressed HTML/plain text is accepted. Raw page text goes exclusively
to an isolated summary call without orders; only its bounded metadata goes to clarification.
A refused/failed fetch or summary stores the URL with null title/summary and reports that no
summary was available. Journal lines contain only ids, kinds, sizes and outcomes, never owner
text, URLs, titles, summaries or file names.

For an update, back up installed bot Python files and the unit in a dated root-only directory,
show installed diffs, install all committed modules together at `root:root 0644`, and restart
only the bot. Check active state, restart count and unchanged `systemd-analyze security`
exposure (1.3). No dependency, unit sandbox, listener or credential changes are needed.
Rollback restores the pre-install Python files from that directory and restarts the bot;
remove only the three newly installed modules (`inbox.py`, `keeping.py`, `links.py`) if absent
from that backup. Preserve the encrypted inbox and orders; the prior bot simply ignores the
inbox. The orchestrator coordinates the remaining owner checks: an idea with clarification,
`just save it`, a public link, a photo, a message for a repository orchestrator, and `/inbox`
listing/detail. These phone checks are not replaced by fixture tests.

### Bot order endpoint

The owner must allow the Mac collector to reach **the node's TCP 8110** in the Tailscale
policy. The node accepts `tailscale0`, so tailnet peers allowed by that policy can reach it.
Platform containers on `homelab_default` (`172.18.0.0/16`) can also reach the endpoint at
the same Tailscale address: one ufw input rule accepts TCP 8110 from that source subnet.
Container traffic arrives through the Docker bridge rather than `tailscale0` and otherwise
hits the input drop policy. This source-subnet rule trusts containers on that network;
Tailscale policy gates tailnet traffic, not this local bridge path. There is no listener key.

The bot discovers its own IPv4 with `/usr/bin/tailscale ip -4` and binds only that address,
never loopback, LAN, wildcard or IPv6. Discovery/bind failures retry every five seconds.
Telegram polling and host commands continue while the interface is absent. There is no Mac
collector in this repository yet and the bot never executes an order.

In conversation, the model can propose one of these orders. The bot refreshes the mirror
and validates before sending the exact action with **Yes** and **No** buttons. Long
confirmations show every part before their final buttons, without truncating arguments or
reviews. No cancels; Yes queues; either closes the episode. Only main-allowlisted users can
tap. No cancels from stored state without a network pull; Yes refreshes before queuing.
A late tap says `already <status>` and makes no change.

| Action | `slice` target | `argument` | Required current state |
|---|---|---|---|
| `start` | slice id | null | `ready` |
| `slot` | slice id | ISO date-time or phrase for the Mac to parse | `ready` |
| `pause`, `resume` | slice id | null | anything except `accepted` |
| `answer` | slice id | owner's verbatim words from their current message | question/escalation attention with null `answer` |
| `approve` | slice id | null | merge attention, `approved: false`, and an orchestrator `review` |
| `orchestrator` | repository name from state `repo`, or `development` | task text | known repository, or the main orchestrator |

Approve always requires review, including slices without ⛔. Its confirmation shows the PR
number and the review's verdict verbatim. Optional private `github_owner` in `planning.json`
adds `https://github.com/<owner>/<repo>/pull/<number>`; without it the PR number still appears.
Arguments are never interpreted or executed by the bot. The Mac must enforce its own
preconditions before execution, including date parsing and merge safeguards.

The HTTP contract has **exactly two routes**. No authentication header or key is required;
Tailscale policy is the sole network gate. All other method/path combinations return 404,
including trailing slashes and query strings. Both valid routes return 503 before the data
volume is unlocked, when planning is unconfigured, or when their store is unavailable.

`GET /orders` returns 200 with `Content-Type: application/json`:

```json
{"orders":[{"id":"0123456789abcdef0123456789abcdef","created":"2026-10-20T10:00:00+00:00","action":"start","slice":"example/task","argument":null,"expect":{"state":"ready","paused":false,"attention":null}}]}
```

`id` is an opaque 32-character lowercase hex order identifier; `created` is a UTC ISO timestamp.
`slice` is the slice id (`<repo>/<name>`), or the repository name for `orchestrator`.
The six fields shown are the complete order payload. Only queued orders appear, oldest
`created` first, with id breaking ties. A GET neither reserves nor removes anything: repeated
GETs return the same orders until acknowledged or overtaken (new orders can arrive).
The empty result is `{"orders": []}`. The collector must retain order ids to avoid executing
the same order twice after an uncertain acknowledgement.

`expect` is copied when confirmation is prepared and is exactly
`{"state": "<state>", "paused": false, "attention": null}` or
`{"state": "<state>", "paused": false, "attention": {"kind": "<kind>", "since": "<timestamp>"}}`.
For `orchestrator`, `expect` is null. After every successful pull, a proposed or queued
slice order becomes `overtaken` if these fields differ, the slice disappeared, or the action's
precondition no longer holds. The latter catches an answer/approval recorded by the
orchestrator even when attention kind/since stayed the same. Overtaken orders leave the
queue and their confirmation is edited with what changed, removing buttons. The collector
must compare `expect` with its own current scheduler state before carrying out an order;
it must not treat receipt of an order as permission to bypass current preconditions.

`POST /orders/<id>/ack` requires a JSON body and a single `Content-Length` (chunked bodies
are refused). It accepts exactly these fields:

```json
{"outcome":"dropped","detail":"Cancelled by the owner on the Mac"}
```

`outcome` is `done`, `dropped` or `failed`; `detail` is a string (empty is permitted).
204 means the receipt is durably recorded, with no response body. The order leaves the
queue and its Telegram confirmation is edited to `Done`, `Dropped: <detail>` or
`Failed: <detail>`, with no buttons. Telegram delivery can lag: pending edits retry every
second and survive restarts. Repeating an ack of a known closed order returns 204 and does
nothing, even if outcome/detail differ. An ack cannot queue a proposal or change a cancelled
or overtaken order. Unknown ids return 404. Invalid JSON, missing/extra fields, wrong types,
invalid outcomes or body framing return 400. A body over **4096 bytes** returns 413.
HTTP connections close after each request, with a three-second socket timeout.

Orders persist in `/srv/homelab/frontline/orders.json`, outside the read-only clone, mode
0600. Writes atomically replace and fsync the file and its directory before success.
Transitions are `proposed → queued | cancelled | overtaken` and
`queued → done | dropped | failed | overtaken`. Closed records are currently retained
without automatic pruning, keeping late taps and repeated receipts meaningful. Do not
delete the store to retry delivery. Arguments and verdicts are private volume/Telegram
content: neither HTTP access logs nor exception text are logged; order audit lines contain
only order ids, actions, slice ids and statuses.

After installation, verify `systemctl is-active homelab-telegram-bot.service`, `NRestarts`,
`ss -ltn` (8110 only on the node's Tailscale IPv4), and `systemd-analyze security
homelab-telegram-bot.service`. The unit changes only a networking comment; its sandbox
score is unchanged at 1.3. From the Mac:

```bash
NODE_TS_IP="<node Tailscale IPv4>"
curl -s --max-time 5 "http://$NODE_TS_IP:8110/orders"
curl -s -o /dev/null -w '%{http_code}\n' --max-time 5 "http://$NODE_TS_IP:8110/unknown"
```

Expect `{"orders": []}` when no orders are queued and 404. A Mac timeout calls for checking
the Tailscale grant. On the unlocked node, check from an existing platform container
on `homelab_default` (the worker has Python; no image download is needed):

```bash
NODE_TS_IP="$(tailscale ip -4)"
docker exec homelab-worker-1 python -c '
import sys
import urllib.request
opener = urllib.request.build_opener(urllib.request.ProxyHandler({}))
with opener.open(f"http://{sys.argv[1]}:8110/orders", timeout=5) as response:
    print(response.status)
' "$NODE_TS_IP"
```

Expect HTTP 200 with the volume unlocked. For a container timeout, check its network and
source subnet and confirm `sudo ufw status` shows TCP 8110 allowed from `172.18.0.0/16`.
Keep the listener bound only to the Tailscale IPv4. The owner's remaining phone check:
request an order, inspect its exact confirmation, tap Yes, see it in GET, then discard it:

```bash
ORDER_ID="<id from GET /orders>"
curl -s -o /dev/null -w '%{http_code}\n' --max-time 5 \
  -H 'Content-Type: application/json' \
  --data '{"outcome":"dropped","detail":"Owner validation; do not execute"}' \
  "http://$NODE_TS_IP:8110/orders/$ORDER_ID/ack"
```

Expect 204, the order absent from the next GET, and `Dropped: Owner validation; do not execute`
on the phone. These checks do not run an order. Also check No, a late tap and an overtaken
proposal from the phone. The orchestrator coordinates these owner checks after deployment.

Rollback: restore the dated pre-install bot Python files and unit from the root-only rollback
directory described under [Rebuilding the node](operations.md#rebuilding-the-node), then `systemctl
daemon-reload` and restart only `homelab-telegram-bot.service`. The prior bot has no order
listener; verify 8110 is absent and `/status` works. Retain `orders.json`, clone, key and
private configuration for recovery. No stack, volume, polkit or credential rollback is needed.

Private bot configuration stays on the node under `/etc/homelab-telegram-bot/`: `allowlist`
and `privileged-allowlist` contain numeric Telegram user IDs, one per line; `restart-allowlist`
contains unit names, one per line (a missing `.service` suffix is added). All three ignore blank
lines and `#` comments, including trailing comments, and use owner/group `root:homelab-bot` with
mode `0640`. The main allowlist must be nonempty; privileged users must be a subset of it.
A missing or empty privileged or restart allowlist grants no capability. Paths can be overridden
by `HOMELAB_BOT_ALLOWLIST`, `HOMELAB_BOT_PRIVILEGED_ALLOWLIST` and
`HOMELAB_BOT_RESTART_ALLOWLIST`. Their contents never enter git, tests or logs. The token stays
in TPM-sealed `token.cred`; the bot reads only systemd's `$CREDENTIALS_DIRECTORY/bot-token`.

## Mac action channel

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
