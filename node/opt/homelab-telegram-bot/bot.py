#!/usr/bin/env python3
"""Home Lab Telegram client, using long polling and the standard library.

Long polling needs outbound HTTPS and no inbound port. The main allowlist is
checked once before dispatch, so new handlers cannot accidentally be public.
The router separately gates /restart; /ask only returns text for Telegram.
The token comes from systemd's credential directory, never the environment.
Handler failures must log only metadata, since exception text can contain prose.
"""

import json
import os
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
        with open(path, encoding="utf-8") as fh:
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

    The file contains private deployment data and stays on the node.

    An EMPTY allowlist is a hard error, not "allow everyone". A misconfiguration
    must fail closed: the failure mode of an accidentally-empty file must never
    be a bot that answers strangers.
    """
    path = os.environ.get("HOMELAB_BOT_ALLOWLIST", "/etc/homelab-telegram-bot/allowlist")
    ids: set[int] = set()
    try:
        with open(path, encoding="utf-8") as fh:
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
        with open(path, encoding="utf-8") as fh:
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
    for part in split_message(text):
        try:
            response = api_call("sendMessage", {"chat_id": chat_id, "text": part}, timeout=30)
            if not response.get("ok"):
                log("WARNING: sendMessage returned not-ok")
                return
        except Exception as exc:  # noqa: BLE001 - never let a reply failure kill the loop
            log(f"WARNING: sendMessage failed: {type(exc).__name__}")
            return


def split_message(text: str) -> list[str]:
    """Split plain text below Telegram's 4096 limit, counting UTF-16 units.

    Counting supplementary characters twice also works for clients that measure
    the limit in Unicode characters. No markup parsing or escape rules apply.
    """
    parts = []
    chars = []
    units = 0
    for char in text:
        size = 2 if ord(char) > 0xFFFF else 1
        if units + size > 4096:
            parts.append("".join(chars))
            chars, units = [], 0
        chars.append(char)
        units += size
    if chars:
        parts.append("".join(chars))
    return parts


def register_profile(router: Router) -> None:
    commands = [
        {"command": name.removeprefix("/"), "description": executor.summary}
        for name, executor in router.executors.items()
    ]
    description = "Home Lab commands: " + "; ".join(
        f"/{command['command']}: {command['description']}" for command in commands
    )
    for method, params in (
        ("setMyCommands", {"commands": json.dumps(commands)}),
        ("setMyDescription", {"description": description}),
    ):
        try:
            response = api_call(method, params)
            if response.get("ok"):
                log(f"Telegram command registration {method} succeeded")
            else:
                log(f"WARNING: Telegram command registration {method} returned not-ok")
        except Exception as exc:  # noqa: BLE001 - registration must not stop polling
            log(f"WARNING: Telegram command registration {method} failed: {type(exc).__name__}")


def dispatch_reply(router: Router, user_id: int, text: str) -> str:
    # A handler failure must not cause a restart loop driven by one pending
    # message. Log the registered command, never arguments or exception text.
    try:
        return router.dispatch(user_id, text)
    except Exception as exc:  # noqa: BLE001 - a command must never be fatal
        command, _ = router.parse(text)
        executor = router.executors.get(command)
        name = executor.name if executor else "unknown"
        log(f"ERROR: command {name} failed for user {user_id}: {type(exc).__name__}")
        return "That command failed. The error is in the journal."


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
                log(f"WARNING: Telegram unreachable, backing off: {type(exc).__name__}")
                outage_logged = True
            time.sleep(backoff)
            backoff = min(backoff * 2, BACKOFF_MAX)
            continue

        if outage_logged:
            log("Telegram reachable again")
            outage_logged = False
        backoff = BACKOFF_START

        if not resp.get("ok"):
            log("WARNING: getUpdates returned not-ok")
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

            send_message(chat_id, dispatch_reply(router, user_id, text))


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

    register_profile(router)

    try:
        poll_forever(allowlist, router)
    except KeyboardInterrupt:
        log("stopping")


if __name__ == "__main__":
    main()
