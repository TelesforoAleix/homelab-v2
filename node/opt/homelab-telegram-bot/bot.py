#!/usr/bin/env python3
"""
Home Lab status bot — Phase 07.

A deterministic, read-only Telegram bot. It reports this machine's own state
and can change nothing.

DESIGN CONSTRAINTS, and why each one is here
--------------------------------------------

1. NO INBOUND PORT.
   Telegram offers two ways to receive messages: webhooks (Telegram connects to
   you) and long polling (you connect to Telegram). This uses long polling, so
   the bot opens only OUTBOUND HTTPS. It listens on nothing.

   That is not a preference. This node has no firewall, and `:22` is the only
   port reachable off-box. Long polling is what makes running this safe here.
   Moving to webhooks would need a publicly reachable inbound HTTPS endpoint and
   would change the exposure model completely (ADR-023).

2. NO THIRD-PARTY DEPENDENCIES.
   The Telegram Bot API is JSON over HTTPS. The standard library does it. A
   dependency here would be a supply-chain risk and an upgrade obligation in
   exchange for saving about forty lines (AGENTS.md: prefer minimal,
   comprehensible implementations).

3. NO SUBPROCESSES. AT ALL.
   Every figure below comes from /proc, /etc/hostname or os.statvfs(). The bot
   never forks or executes anything, which means the systemd unit can forbid it
   outright. A bot that cannot execute a program cannot be talked into executing
   the wrong one.

4. READ-ONLY.
   There is no command that changes anything. A compromise leaks host metrics;
   it cannot act. Escalation is Phase 08's problem, deliberately (ADR-011).

5. THE ALLOWLIST IS CHECKED ONCE, BEFORE DISPATCH.
   Anyone who finds a Telegram bot can message it. The check lives in the
   dispatch loop rather than inside each handler, so a command added later
   cannot accidentally be unprotected.

THE TOKEN
---------
Supplied by systemd `LoadCredential=`, read from $CREDENTIALS_DIRECTORY. It is
deliberately NOT an environment variable: anything able to read the process's
/proc/PID/environ would see it, and environment variables have a habit of
turning up in crash dumps and logs.

It is never logged. `redact()` exists for the case where an exception message
would otherwise carry the URL it was built into.
"""

import json
import os
import socket
import ssl
import sys
import time
import urllib.error
import urllib.parse
import urllib.request

import executors
from router import Router, load_ids

API_ROOT = "https://api.telegram.org"

# Long-poll timeout. Telegram holds the request open this long waiting for a
# message, so the loop is idle rather than busy. Keep it below any intermediate
# proxy timeout.
POLL_TIMEOUT = 50

# Network read timeout must exceed POLL_TIMEOUT, or every quiet poll looks like
# a failure. This is the classic long-polling mistake.
HTTP_TIMEOUT = POLL_TIMEOUT + 15

# Backoff when Telegram is unreachable. Capped, so an outage does not turn into
# an ever-growing wait, and jittered nowhere because a single client does not
# need it. Bounded logging: see poll_forever().
BACKOFF_START = 2
BACKOFF_MAX = 300


def log(msg: str) -> None:
    """Log to stdout; systemd routes it to the journal. Never called with a token."""
    print(msg, flush=True)


def redact(text: str) -> str:
    """
    Remove the token from a string before it can reach the journal.

    Error messages from urllib often include the full URL, and the token is IN
    the URL for every Telegram API call. Without this, one connection error
    would write a permanent bearer credential into the system journal.
    """
    if _TOKEN and _TOKEN in text:
        text = text.replace(_TOKEN, "<redacted-token>")
    return text


def load_token() -> str:
    """
    Read the bot token from the systemd credential directory.

    systemd puts LoadCredential= material in $CREDENTIALS_DIRECTORY with mode
    0400, owned by the service user, on a tmpfs that is unmounted when the unit
    stops. It never appears in the environment or in the unit file.
    """
    cred_dir = os.environ.get("CREDENTIALS_DIRECTORY")
    if not cred_dir:
        sys.exit(
            "ERROR: CREDENTIALS_DIRECTORY is not set.\n"
            "This bot expects to be started by systemd with LoadCredential=.\n"
            "Refusing to look for the token anywhere else -- an environment\n"
            "variable or a world-readable file would be a downgrade, not a\n"
            "fallback."
        )
    path = os.path.join(cred_dir, "bot-token")
    try:
        with open(path, "r", encoding="utf-8") as fh:
            token = fh.read().strip()
    except OSError as exc:
        sys.exit(f"ERROR: cannot read the bot token credential: {exc}")
    if not token:
        sys.exit("ERROR: the bot token credential is empty.")
    return token


def load_allowlist() -> set[int]:
    """
    Read permitted Telegram user IDs, one per line. Blank lines and # comments
    are ignored.

    This is not a secret -- it is a list of numeric IDs -- but it is
    deployment-specific, so it lives on the node rather than in the repository.

    An EMPTY allowlist is a hard error, not "allow everyone". A misconfiguration
    must fail closed: the failure mode of an accidentally-empty file must never
    be a bot that answers strangers.
    """
    path = os.environ.get("HOMELAB_BOT_ALLOWLIST", "/etc/homelab-telegram-bot/allowlist")
    ids: set[int] = set()
    try:
        with open(path, "r", encoding="utf-8") as fh:
            for line in fh:
                line = line.split("#", 1)[0].strip()
                if not line:
                    continue
                try:
                    ids.add(int(line))
                except ValueError:
                    log(f"WARNING: ignoring non-numeric allowlist entry in {path}")
    except OSError as exc:
        sys.exit(f"ERROR: cannot read the allowlist at {path}: {exc}")

    if not ids:
        sys.exit(
            f"ERROR: the allowlist at {path} is empty.\n"
            "Refusing to start. An empty allowlist must never mean 'allow "
            "everyone' -- a status bot that answers strangers reports this "
            "machine's state to whoever finds it."
        )
    return ids


def load_privileged() -> set[int]:
    """
    Read the ids permitted to invoke PRIVILEGED executors.

    Absent or empty is FINE and means "nobody may escalate" -- a perfectly
    reasonable posture that must not stop the bot starting. That is the opposite
    of the main allowlist, where empty is fatal, and the difference is
    deliberate: empty-means-nobody is safe, empty-means-everybody is not.
    """
    path = os.environ.get(
        "HOMELAB_BOT_PRIVILEGED_ALLOWLIST",
        "/etc/homelab-telegram-bot/privileged-allowlist",
    )
    return load_ids(path, required=False)


def load_restart_units() -> set[str]:
    """Unit names the restart executor may target. Empty means it can do nothing."""
    path = os.environ.get(
        "HOMELAB_BOT_RESTART_ALLOWLIST",
        "/etc/homelab-telegram-bot/restart-allowlist",
    )
    units: set[str] = set()
    try:
        with open(path, "r", encoding="utf-8") as fh:
            for line in fh:
                line = line.split("#", 1)[0].strip()
                if line:
                    units.add(line if line.endswith(".service") else f"{line}.service")
    except FileNotFoundError:
        return set()
    return units


# --------------------------------------------------------------------------
# Telegram API
# --------------------------------------------------------------------------

_TOKEN = ""
_SSL_CTX = ssl.create_default_context()


def api_call(method: str, params: dict | None = None, timeout: int = 30) -> dict:
    """One Telegram API call. Raises on transport failure; caller decides."""
    url = f"{API_ROOT}/bot{_TOKEN}/{method}"
    data = urllib.parse.urlencode(params or {}).encode()
    req = urllib.request.Request(url, data=data, method="POST")
    with urllib.request.urlopen(req, timeout=timeout, context=_SSL_CTX) as resp:
        return json.loads(resp.read().decode())


def send_message(chat_id: int, text: str) -> None:
    try:
        api_call("sendMessage", {"chat_id": chat_id, "text": text}, timeout=30)
    except Exception as exc:  # noqa: BLE001 - never let a reply failure kill the loop
        log(f"WARNING: sendMessage failed: {redact(str(exc))}")


# --------------------------------------------------------------------------
# Main loop
# --------------------------------------------------------------------------

def poll_forever(allowlist: set[int], router: Router) -> None:
    offset = 0
    backoff = BACKOFF_START
    outage_logged = False

    log(f"started; {len(allowlist)} allowlisted user(s); long polling, no listening socket")

    while True:
        try:
            resp = api_call(
                "getUpdates",
                {"offset": offset, "timeout": POLL_TIMEOUT},
                timeout=HTTP_TIMEOUT,
            )
        except Exception as exc:  # noqa: BLE001 - transport problems are expected
            # Log the FIRST failure of an outage, then stay quiet until it
            # recovers. A bot that logs every retry turns a network blip into a
            # journal full of identical lines, and journald is not free on a
            # volume group with no free extents.
            if not outage_logged:
                log(f"WARNING: Telegram unreachable, backing off: {redact(str(exc))}")
                outage_logged = True
            time.sleep(backoff)
            backoff = min(backoff * 2, BACKOFF_MAX)
            continue

        if outage_logged:
            log("Telegram reachable again")
            outage_logged = False
        backoff = BACKOFF_START

        if not resp.get("ok"):
            log(f"WARNING: getUpdates returned not-ok: {redact(json.dumps(resp))}")
            time.sleep(backoff)
            continue

        for update in resp.get("result", []):
            offset = update["update_id"] + 1
            message = update.get("message") or update.get("edited_message")
            if not message:
                continue

            user_id = (message.get("from") or {}).get("id")
            chat_id = (message.get("chat") or {}).get("id")
            text = message.get("text", "")
            if chat_id is None:
                continue

            # THE ACCESS CONTROL. One place, before anything else happens.
            if user_id not in allowlist:
                log(f"refused: user {user_id} is not allowlisted")
                send_message(chat_id, "Not authorised.")
                continue

            # A failing handler must not kill the bot.
            #
            # Without this, one exception inside a command took the whole
            # process down, systemd restarted it, the same message was still
            # pending, and it crashed again -- a restart loop driven by a
            # single bad message. That is exactly how a read-only status bot
            # turns into a self-inflicted outage on a console-less node.
            #
            # Found in the reference build: ProcSubset=pid in the unit hid
            # /proc/uptime, so every /status raised FileNotFoundError.
            try:
                reply = router.dispatch(user_id, text)
            except Exception as exc:  # noqa: BLE001 - a command must never be fatal
                log(f"ERROR: command failed for user {user_id}: {redact(str(exc))}")
                send_message(chat_id, "That command failed. The error is in the journal.")
                continue

            send_message(chat_id, reply)


def main() -> None:
    global _TOKEN
    _TOKEN = load_token()
    allowlist = load_allowlist()
    privileged = load_privileged()
    restart_units = load_restart_units()

    # THE SUBSET RULE, enforced at startup rather than per request.
    #
    # A user cannot be privileged without first being permitted. Checking it here
    # turns a misconfiguration into a refusal to start, which someone notices,
    # instead of a surprise the first time an unlisted id sends /restart.
    stray = privileged - allowlist
    if stray:
        sys.exit(
            f"ERROR: {len(stray)} id(s) are in the privileged allowlist but not in "
            "the main allowlist.\n"
            "Refusing to start. A user cannot be authorised for privileged commands "
            "without first being permitted to use the bot at all."
        )

    router = Router(privileged_users=privileged, log=log)
    executors.register_all(router, allowed_units=restart_units, log=log)

    log(
        f"{len(allowlist)} allowlisted, {len(privileged)} privileged, "
        f"{len(restart_units)} restartable unit(s), "
        f"{len(router.unique_executors())} executors registered"
    )

    try:
        poll_forever(allowlist, router)
    except KeyboardInterrupt:
        log("stopping")


if __name__ == "__main__":
    main()
