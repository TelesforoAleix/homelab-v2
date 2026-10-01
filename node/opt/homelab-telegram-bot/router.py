"""Registry plus dispatch, with one authorisation check before handlers.

The main allowlist gates use of the bot; the privileged allowlist gates changes.
The privileged set must be a subset of the main set, checked at startup so a
misconfiguration fails visibly. New handlers are registry entries and cannot
accidentally bypass the capability check.
"""

from __future__ import annotations

import enum
from collections.abc import Callable
from dataclasses import dataclass


class Capability(enum.Enum):
    """
    What an executor needs in order to run.

    READ        answers from the host's own state; changes nothing.
    PRIVILEGED  changes something. Requires the privileged allowlist.
    UNAVAILABLE registered so it appears in /help and in the architecture, but
                deliberately not wired.
    """

    READ = "read"
    PRIVILEGED = "privileged"
    UNAVAILABLE = "unavailable"


@dataclass(frozen=True)
class Executor:
    """One capability. Does one thing, and declares what it needs to do it."""

    name: str
    capability: Capability
    handler: Callable[..., str]
    summary: str
    usage: str = ""

    # Pass (args, user_id) instead of (args) when a handler needs caller metadata.
    wants_user: bool = False

    # /restart needs the unit name for auditing. /ask arguments are private
    # prose: log only the question length, never its content.
    log_args: bool = True


class Router:
    """Registry plus dispatch. The only place a command becomes an action."""

    def __init__(self, privileged_users: set[int], log: Callable[[str], None]):
        self._registry: dict[str, Executor] = {}
        self._privileged_users = privileged_users
        self._log = log

    # -- registration ------------------------------------------------------

    def register(self, executor: Executor, *aliases: str) -> None:
        for name in (executor.name, *aliases):
            key = name.lower()
            if key in self._registry:
                raise ValueError(f"duplicate executor registration: {key}")
            self._registry[key] = executor

    @property
    def executors(self) -> dict[str, Executor]:
        return dict(self._registry)

    def unique_executors(self) -> list[Executor]:
        """Registered executors, de-duplicated across aliases, in registration order."""
        seen: list[Executor] = []
        for ex in self._registry.values():
            if ex not in seen:
                seen.append(ex)
        return seen

    # -- dispatch ----------------------------------------------------------

    def parse(self, text: str) -> tuple[str, list[str]]:
        """
        Split an incoming message into a command and its arguments.

        Telegram appends "@BotName" to commands in group chats, so that is
        stripped. Matching is case-insensitive on the command only -- arguments
        are passed through untouched, because a service name is case-sensitive.
        """
        parts = text.strip().split()
        if not parts:
            return "", []
        command = parts[0].split("@", 1)[0].lower()
        return command, parts[1:]

    def dispatch(self, user_id: int, text: str) -> str:
        """
        Route one message. Returns the reply text.

        This function is the authorisation boundary. Every privileged action in
        this project passes through the single `if` below, and nothing else in
        the codebase is permitted to decide entitlement.
        """
        command, args = self.parse(text)
        executor = self._registry.get(command)

        if executor is None:
            self._log(f"user {user_id}: unknown command")
            return "Unknown command. Try /help"

        # --- THE AUTHORISATION CHECK ---
        #
        # Note it happens BEFORE the handler is called, and that the refusal is
        # distinguishable from "unknown command". Telling a permitted user that a
        # command does not exist would send them looking for a typo instead of
        # asking for access.
        if executor.capability is Capability.PRIVILEGED and user_id not in self._privileged_users:
            self._log(f"REFUSED privileged: user {user_id} -> {executor.name}")
            return (
                f"'{command}' is a privileged command and you are not authorised for it.\n"
                "This refusal has been logged."
            )

        if executor.log_args:
            self._log(f"user {user_id}: {executor.name} {' '.join(args)}".rstrip())
        else:
            self._log(f"user {user_id}: {executor.name} ({len(' '.join(args))} chars)")

        if executor.wants_user:
            return executor.handler(args, user_id)
        return executor.handler(args)


def load_ids(path: str, *, required: bool) -> set[int]:
    """
    Read numeric ids, one per line. '#' comments and blank lines ignored.

    `required` distinguishes the two allowlists. The main allowlist being empty
    is fatal -- an empty file must never mean "allow everyone". The
    privileged allowlist being empty is FINE and means "nobody may escalate",
    which is a perfectly reasonable posture and must not stop the bot starting.
    """
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
                    pass
    except FileNotFoundError:
        if required:
            raise
        return set()
    return ids
