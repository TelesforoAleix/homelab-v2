"""
Router — Phase 08.

Turns a parsed command into a named capability, and decides whether this user is
entitled to it.

WHY THIS IS A TABLE AND NOT A CHAIN OF IFs
------------------------------------------
Phase 09 adds transcription, Phase 10 adds knowledge retrieval, Phase 12 adds
automation. If dispatch were a chain of `if command == ...` branches, each of
those phases would edit the same function and the authorisation check would
drift into the branches. A registry means a new executor is a new entry, and the
check stays in exactly one place.

TWO CHECKS, NOT ONE
-------------------
Phase 07 had authentication: *may this user talk to the bot at all?* That was
sufficient while everything was read-only.

This phase adds authorisation: *may this user invoke THIS executor?* They are
different questions with different answers, and conflating them is how a
read-only bot quietly acquires a privileged command.

    allowlist          -> may use the bot          (authentication)
    privileged-allow   -> may invoke PRIVILEGED    (authorisation)

The second is enforced as a SUBSET of the first: a user cannot be privileged
without first being permitted. That is checked at load time, not per request, so
a misconfiguration is a startup failure rather than a surprise at 3am.
"""

from __future__ import annotations

import enum
from dataclasses import dataclass
from typing import Callable


class Capability(enum.Enum):
    """
    What an executor needs in order to run.

    READ        answers from the host's own state; changes nothing.
    PRIVILEGED  changes something. Requires the privileged allowlist.
    UNAVAILABLE registered so it appears in /help and in the architecture, but
                deliberately not wired. See executors.model_executor.
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

    # Phase 09 added these two, and they are the first change to this class
    # since it was written. The Phase 08 handover hoped a new executor would
    # never require touching dispatch(); /ask does, and the reason is recorded
    # rather than worked around with a module-level "current user" variable,
    # which is how this kind of thing usually gets smuggled in.

    # wants_user: the handler is called with (args, user_id) instead of (args).
    #
    # /ask is the first executor whose action costs a shared resource -- the
    # owner's own subscription allowance. The audit record of who spent it has
    # to be in the record written by the process that spent it, not stitched
    # together from two journals by timestamp.
    wants_user: bool = False

    # log_args: whether the router writes the arguments to the journal.
    #
    # True for everything before Phase 09, and deliberately so: `/restart
    # chrony.service` without the unit name is a useless audit line. False for
    # /ask, because its argument is the owner's own prose. The journal should
    # record that a question was asked and what it cost, not what was asked.
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
    is fatal -- an empty file must never mean "allow everyone" (Phase 07). The
    privileged allowlist being empty is FINE and means "nobody may escalate",
    which is a perfectly reasonable posture and must not stop the bot starting.
    """
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
                    pass
    except FileNotFoundError:
        if required:
            raise
        return set()
    return ids
