#!/usr/bin/env python3
"""Home Lab Telegram client, using python-telegram-bot long polling.

Long polling needs outbound HTTPS and no inbound port. The main allowlist is
checked once before dispatch, so new handlers cannot accidentally be public.
The router separately gates /restart; /ask only returns text for Telegram.
The token comes from systemd's credential directory, never the environment.
Handler failures must log only metadata, since exception text can contain prose.
"""

import asyncio
import logging
import os
import re
import sys
from contextlib import suppress

import executors
from conversation import Conversation
from planning import Planning, load_config
from router import Router, load_ids
from telegram import Bot, BotCommand, Update
from telegram.ext import Application, ContextTypes, ExtBot, MessageHandler, filters
from telegram.request import HTTPXRequest

POLL_TIMEOUT = 50


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


class AuditedBot(ExtBot):
    """Add content-free outage metadata; the library owns polling and retries."""

    __slots__ = ("_outage_logged",)

    def __init__(self, token: str):
        super().__init__(
            token,
            request=HTTPXRequest(read_timeout=30),
            # PTB adds the long-poll timeout to this read timeout (65s total).
            get_updates_request=HTTPXRequest(read_timeout=15),
        )
        self._outage_logged = False

    async def get_updates(self, *args, **kwargs):
        try:
            updates = await super().get_updates(*args, **kwargs)
        except Exception as exc:
            if not self._outage_logged:
                log(f"WARNING: Telegram unreachable, backing off: {type(exc).__name__}")
                self._outage_logged = True
            raise
        if self._outage_logged:
            log("Telegram reachable again")
            self._outage_logged = False
        return updates


async def send_message(bot: Bot, chat_id: int, text: str) -> list[int]:
    message_ids = []
    for part in split_message(text):
        try:
            sent = await bot.send_message(chat_id=chat_id, text=part, read_timeout=30)
            message_ids.append(sent.message_id)
        except Exception as exc:  # noqa: BLE001 - reply failure must not stop polling
            log(f"WARNING: sendMessage failed: {type(exc).__name__}")
            return message_ids
    return message_ids


def split_message(text: str) -> list[str]:
    """Split plain text below Telegram's 4096 limit, counting UTF-16 units.

    Counting supplementary characters twice also works for clients that measure
    the limit in Unicode characters. No markup parsing or escape rules apply.
    """
    parts = []
    while text:
        units = 0
        end = 0
        for char in text:
            size = 2 if ord(char) > 0xFFFF else 1
            if units + size > 4096:
                break
            units += size
            end += 1
        if end < len(text):
            paragraph = text.rfind("\n\n", 0, end)
            if paragraph > 0:
                end = paragraph + 2
            else:
                # A single oversize paragraph falls back to sentence, line, word,
                # then character boundaries, in that order.
                boundaries = list(re.finditer(r"[.!?]\s+|\n", text[:end]))
                if boundaries:
                    end = boundaries[-1].end()
                elif (space := text.rfind(" ", 0, end)) > 0:
                    end = space + 1
        parts.append(text[:end])
        text = text[end:]
    return parts


async def register_profile(bot: Bot, router: Router) -> None:
    commands = [
        BotCommand(name.removeprefix("/"), executor.summary)
        for name, executor in router.executors.items()
    ]
    description = "Home Lab commands: " + "; ".join(
        f"/{command.command}: {command.description}" for command in commands
    )
    for method, call, params in (
        ("setMyCommands", bot.set_my_commands, {"commands": commands}),
        ("setMyDescription", bot.set_my_description, {"description": description[:512]}),
    ):
        try:
            if await call(**params):
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


def build_application(
    token: str, allowlist: set[int], router: Router, planning: Planning | None = None
) -> Application:
    notice_task = None
    conversation = Conversation(planning, log)

    async def startup(application: Application) -> None:
        nonlocal notice_task
        await register_profile(application.bot, router)
        log(f"started; {len(allowlist)} allowlisted user(s); long polling, no listening socket")
        if planning is not None and planning.config is not None:

            async def send_notice(user_id, text):
                # Unlike command replies, failures must propagate to retain the marker.
                for part in split_message(text):
                    await application.bot.send_message(chat_id=user_id, text=part, read_timeout=30)

            notice_task = asyncio.create_task(planning.run(allowlist, send_notice))

    async def shutdown(application: Application) -> None:
        if notice_task is not None:
            notice_task.cancel()
            with suppress(asyncio.CancelledError):
                await notice_task

    async def receive(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
        message = update.effective_message
        if message is None or update.effective_chat is None:
            return
        user_id = update.effective_user.id if update.effective_user else None
        chat_id = update.effective_chat.id
        # One main access check before dispatch, including non-text messages.
        if user_id not in allowlist:
            log(f"refused: user {user_id} is not allowlisted")
            reply = "Not authorised."
        elif message.text and not message.text.lstrip().startswith("/"):
            owner = (chat_id, user_id)
            reply_to = message.reply_to_message
            reply, episode = await conversation.answer(
                owner,
                message.text,
                reply_to.message_id
                if reply_to and reply_to.from_user and reply_to.from_user.id == context.bot.id
                else None,
            )
            message_ids = await send_message(context.bot, chat_id, reply)
            if episode is not None:
                conversation.remember(owner, episode, message_ids)
            return
        else:
            conversation.close((chat_id, user_id))
            # Executors remain synchronous; don't block Telegram's network loop.
            reply = await asyncio.to_thread(dispatch_reply, router, user_id, message.text or "")
        await send_message(context.bot, chat_id, reply)

    async def error(update: object, context: ContextTypes.DEFAULT_TYPE) -> None:
        # Polling failures are already audited by AuditedBot. Never log exception text.
        if update is not None:
            log(f"ERROR: Telegram update failed: {type(context.error).__name__}")

    application = (
        Application.builder()
        .bot(AuditedBot(token))
        .post_init(startup)
        .post_stop(shutdown)
        .post_shutdown(shutdown)
        .build()
    )
    application.add_handler(MessageHandler(filters.ALL, receive))
    application.add_error_handler(error)
    return application


def main() -> None:
    # Vendor logs can include Bot API URLs (tokens), payloads and exception text.
    # Only our explicit metadata audit reaches journald, including fatal failures.
    logging.disable(logging.CRITICAL)
    token = load_token()
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
    planning = Planning(load_config(log), log)
    planning.register(router)

    log(
        f"{len(allowlist)} allowlisted, {len(privileged)} privileged, "
        f"{len(restart_units)} restartable unit(s), "
        f"{len(router.unique_executors())} executors registered"
    )

    try:
        application = build_application(token, allowlist, router, planning)
        application.run_polling(
            timeout=POLL_TIMEOUT,
            bootstrap_retries=-1,
            allowed_updates=["message", "edited_message"],
        )
    except Exception as exc:
        sys.exit(f"ERROR: Telegram client failed: {type(exc).__name__}")


if __name__ == "__main__":
    main()
