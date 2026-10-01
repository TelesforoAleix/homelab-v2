"""
Executors — Phase 08.

Each executor does one thing and declares what it needs. The router decides
whether the caller is entitled to it; nothing here re-decides that.

THE READ EXECUTORS STILL NEVER FORK
-----------------------------------
Every figure comes from /proc, /etc/hostname or os.statvfs(). That was a Phase 07
property and it is kept: a process that cannot execute a program cannot be talked
into executing the wrong one.

THE PRIVILEGED EXECUTOR BREAKS THAT, DELIBERATELY
-------------------------------------------------
`restart` execs /usr/bin/systemctl. That is a real loss of a Phase 07 guarantee
and it is recorded rather than glossed. What replaces it:

  - NoNewPrivileges=yes is RETAINED, so the exec cannot gain privilege via
    setuid. This is why polkit is used instead of sudo -- sudo is setuid and
    is refused outright under no_new_privs:
        "sudo: The "no new privileges" flag is set, which prevents sudo from
         running as root."
  - CapabilityBoundingSet is empty.
  - The authority comes from a polkit rule scoped to ONE user, ONE unit and ONE
    verb -- evaluated by polkit inside PID 1, not by anything in this process.
  - The unit name is checked against an explicit allowlist here as well, so a
    bug in this file cannot reach a unit the rule would have permitted.

Two independent gates, in two different processes, neither trusting the other.
"""

from __future__ import annotations

import os
import socket
import subprocess

import model_client
from router import Capability, Executor

SYSTEMCTL = "/usr/bin/systemctl"

# The exact path matters. /bin/systemctl is the same binary via a symlink, but
# polkit and any audit record see the argv you actually passed. Keep the string
# used here identical to the one named in the polkit rule and the documentation.


# --------------------------------------------------------------------------
# Host state. /proc and statvfs only. No subprocess.
# --------------------------------------------------------------------------

def _fmt_duration(seconds: float) -> str:
    s = int(seconds)
    d, s = divmod(s, 86400)
    h, s = divmod(s, 3600)
    m, _ = divmod(s, 60)
    if d:
        return f"{d}d {h}h {m}m"
    if h:
        return f"{h}h {m}m"
    return f"{m}m"


def _fmt_bytes(n: float) -> str:
    for unit in ("B", "K", "M", "G", "T"):
        if abs(n) < 1024:
            return f"{n:.1f}{unit}"
        n /= 1024
    return f"{n:.1f}P"


def host_uptime() -> str:
    with open("/proc/uptime", "r", encoding="utf-8") as fh:
        return _fmt_duration(float(fh.read().split()[0]))


def host_load() -> str:
    with open("/proc/loadavg", "r", encoding="utf-8") as fh:
        one, five, fifteen = fh.read().split()[:3]
    return f"{one} {five} {fifteen}"


def host_memory() -> str:
    vals = {}
    with open("/proc/meminfo", "r", encoding="utf-8") as fh:
        for line in fh:
            key, _, rest = line.partition(":")
            vals[key] = int(rest.split()[0]) * 1024
    total = vals.get("MemTotal", 0)
    available = vals.get("MemAvailable", 0)
    used = total - available
    pct = (used / total * 100) if total else 0
    return f"{_fmt_bytes(used)} / {_fmt_bytes(total)} used ({pct:.0f}%)"


def host_disk(path: str = "/") -> str:
    """
    Report usage the way `df` does.

    used = total - f_bfree, available = f_bavail. The naive
    `used = total - available` counts the filesystem's root-reserved blocks as
    used and reported 19.3G where df said 8.9G (Phase 07 build log).
    """
    st = os.statvfs(path)
    total = st.f_blocks * st.f_frsize
    used = (st.f_blocks - st.f_bfree) * st.f_frsize
    avail = st.f_bavail * st.f_frsize
    pct = (used / (used + avail) * 100) if (used + avail) else 0
    return f"{_fmt_bytes(used)} used, {_fmt_bytes(avail)} free of {_fmt_bytes(total)} ({pct:.0f}%)"


def host_name() -> str:
    try:
        with open("/etc/hostname", "r", encoding="utf-8") as fh:
            return fh.read().strip()
    except OSError:
        return socket.gethostname()


# --------------------------------------------------------------------------
# READ executors
# --------------------------------------------------------------------------

def _status(_args: list[str]) -> str:
    return (
        f"host:   {host_name()}\n"
        f"uptime: {host_uptime()}\n"
        f"load:   {host_load()}\n"
        f"memory: {host_memory()}\n"
        f"disk /: {host_disk()}"
    )


def _disk(_args: list[str]) -> str:
    return f"disk /: {host_disk()}"


def _uptime(_args: list[str]) -> str:
    return f"uptime: {host_uptime()}\nload:   {host_load()}"


def _spend(_args: list[str], user_id: int) -> str:
    reply = model_client.spend(user_id)
    if not reply.get("ok"):
        return f"Could not read spend: {reply.get('message', 'the model helper failed')}"
    snapshot = reply.get("spend")
    if not isinstance(snapshot, dict):
        return "Could not read spend: malformed helper reply"
    lines = ["Metered spend (USD, rolling windows):"]
    try:
        for kind in ("attended", "unattended"):
            lines.append(f"{kind}:")
            for window in ("hour", "day", "week", "month"):
                item = snapshot[kind][window]
                lines.append(
                    f"  {window:<5} ${item['spent_usd']} / ${item['ceiling_usd']}"
                )
        lines.append(f"metered calls this week: {snapshot['week_ledger_count']}")
    except (KeyError, TypeError):
        return "Could not read spend: malformed helper reply"
    return "\n".join(lines)


# --------------------------------------------------------------------------
# PRIVILEGED executor
# --------------------------------------------------------------------------

def make_restart(allowed_units: set[str], log) -> Executor:
    """
    Restart one service, from an explicit allowlist.

    The allowlist here is the SECOND gate, not the only one. polkit is the first
    and it lives in another process. Either alone would be a single point of
    failure; a bug in this file cannot reach a unit polkit would refuse, and a
    mistake in the polkit rule cannot reach a unit this list does not name.
    """

    def handler(args: list[str]) -> str:
        if not args:
            listing = ", ".join(sorted(allowed_units)) or "(none configured)"
            return f"usage: /restart <service>\nallowed: {listing}"

        unit = args[0]
        if not unit.endswith(".service"):
            unit = f"{unit}.service"

        if unit not in allowed_units:
            log(f"REFUSED restart: {unit} is not in the restart allowlist")
            listing = ", ".join(sorted(allowed_units)) or "(none configured)"
            return f"'{unit}' is not permitted.\nallowed: {listing}"

        # Exact argv. No shell, no string interpolation, nothing the caller
        # supplied reaches a shell at any point.
        try:
            proc = subprocess.run(
                # --no-ask-password is defence in depth. The service has no
                # controlling TTY so polkit cannot find an agent anyway -- but
                # relying on "there is no TTY" is relying on an accident of the
                # environment. Stating it means a denial stays a denial even if
                # this code is ever run from a terminal.
                [SYSTEMCTL, "--no-ask-password", "restart", unit],
                capture_output=True,
                text=True,
                timeout=30,
                check=False,
            )
        except subprocess.TimeoutExpired:
            log(f"restart {unit}: TIMEOUT")
            return f"restart {unit}: timed out after 30s"
        except OSError as exc:
            log(f"restart {unit}: could not execute systemctl: {exc}")
            return f"restart {unit}: could not run systemctl ({exc})"

        if proc.returncode == 0:
            log(f"restart {unit}: ok")
            return f"restarted {unit}"

        # Fails closed and says so. A polkit denial arrives here as a non-zero
        # exit with a message on stderr; it is reported rather than swallowed.
        detail = (proc.stderr or proc.stdout or "").strip().splitlines()
        first = detail[0] if detail else f"exit {proc.returncode}"
        log(f"restart {unit}: FAILED rc={proc.returncode}: {first}")
        return f"restart {unit} failed: {first}"

    return Executor(
        name="/restart",
        capability=Capability.PRIVILEGED,
        handler=handler,
        summary="restart an allowlisted service",
        usage="/restart <service>",
    )


# --------------------------------------------------------------------------
# The model executor — Phase 09 connected it
# --------------------------------------------------------------------------

def _ask(args: list[str], user_id: int) -> str:
    """
    Ask a model a question. Phase 09, ADR-025.

    THIS EXECUTOR CANNOT REACH A PRIVILEGED ONE
    -------------------------------------------
    It returns a string. That string is sent to Telegram and nothing else is
    done with it -- it is never parsed, never matched against the registry, and
    never passed back into dispatch(). The router's capability check is what
    guarantees `/ask` cannot become a route to `/restart`, and the reason it
    holds is that there is no code path from a model's output to a dispatch.

    That is not a theoretical concern. A model with its tools disabled will
    happily EMIT TEXT SHAPED LIKE A TOOL CALL: during Phase 09 testing, Haiku
    with --tools "" replied with a function_calls block and a confabulated
    answer. Anything that parsed model output looking for commands would have
    found one.

    WHAT THIS SENDS OFF THE MACHINE
    -------------------------------
    The question, and the literal output of /status. That is all, and it is the
    same five figures the owner can already see on their phone. The list is
    enforced here by construction: _status() is called with no arguments and
    there is nowhere to add a sixth source without editing this line.
    """
    question = " ".join(args).strip()
    if not question:
        return (
            "Usage: /ask <question>\n\n"
            "Sends your question and the /status figures to a model.\n"
            "Nothing else about this host is sent."
        )

    context = _status([])
    reply = model_client.ask(question, context, user_id)

    if reply.get("ok"):
        text = str(reply.get("text", "")).strip() or "(the model returned nothing)"
        provider = reply.get("provider", "?")
        model = reply.get("model", "?")
        # The provenance line is not decoration. Two providers answer here and
        # they are not interchangeable; the owner should never have to guess
        # which subscription just paid for an answer.
        return f"{text}\n\n-- {provider}/{model}"

    kind = reply.get("kind", "error")
    message = str(reply.get("message", "the model helper failed"))

    if kind == "exhausted":
        lines = ["Both providers are spent."]
        for item in reply.get("detail", []) or []:
            lines.append(f"  {item}")
        lines.append("")
        lines.append("This is a usage limit, not a fault. The read-only")
        lines.append("commands are unaffected -- try /status.")
        return "\n".join(lines)

    return f"Could not ask a model: {message}"


# --------------------------------------------------------------------------
# Registration
# --------------------------------------------------------------------------

def build_help(router) -> Executor:
    """
    /help, generated from the registry.

    Hand-maintained help text is how an undocumented command survives. Adding an
    executor changes this output without anyone editing prose.
    """

    def handler(_args: list[str]) -> str:
        marks = {
            Capability.READ: " ",
            Capability.PRIVILEGED: "*",
            Capability.UNAVAILABLE: "-",
        }
        lines = ["Home Lab bot — commands:", ""]
        seen = set()
        for ex in router.unique_executors():
            seen.add(ex.capability)
            label = ex.usage or ex.name
            lines.append(f" {marks[ex.capability]} {label:<22} {ex.summary}")

        # The legend describes what is actually on the list above.
        #
        # Phase 09 connected the last UNAVAILABLE executor, and /help went on
        # explaining a "-" marker that no longer appeared against anything --
        # help text describing a state the system had left behind. Deriving the
        # legend from the registry is the same principle as deriving the command
        # list from it: prose that is maintained by hand goes stale silently.
        legend = []
        if Capability.PRIVILEGED in seen:
            legend.append(" * privileged — requires authorisation")
        if Capability.UNAVAILABLE in seen:
            legend.append(" - registered but not connected")
        if legend:
            lines += [""] + legend
        return "\n".join(lines)

    return Executor(
        name="/help",
        capability=Capability.READ,
        handler=handler,
        # "list the commands", not "this message". The summary is now read in
        # two places -- /help itself, and the Telegram command menu on the
        # phone, which configure-telegram-bot-profile.sh derives from this
        # registry. "this message" only made sense in one of them.
        summary="list the commands",
    )


def register_all(router, *, allowed_units: set[str], log) -> None:
    router.register(Executor("/status", Capability.READ, _status,
                             "host, uptime, load, memory, disk"))
    router.register(Executor("/disk", Capability.READ, _disk,
                             "root filesystem usage"))
    router.register(Executor("/uptime", Capability.READ, _uptime,
                             "uptime and load average"))
    router.register(Executor("/spend", Capability.READ, _spend,
                             "metered spend and ceilings",
                             wants_user=True))
    router.register(make_restart(allowed_units, log))
    # /ask carries wants_user because the audit record of a call that spends
    # the owner's subscription allowance must name who asked for it, and
    # log_args=False because the argument is the owner's own prose.
    #
    # Capability.READ, not PRIVILEGED. It reads /proc and talks to a socket it
    # is permitted to talk to; it gains no privilege on this host. The thing it
    # spends is a subscription allowance, and that is rationed by the helper's
    # caps rather than by the router's allowlist -- a resource limit is not an
    # authorisation question.
    #
    # /model stays as an alias. It was the documented command from Phase 07
    # onwards and it now does what it always said it would.
    router.register(
        Executor("/ask", Capability.READ, _ask,
                 "ask a model a question, with /status as context",
                 usage="/ask <question>",
                 wants_user=True, log_args=False),
        "/model",
    )
    router.register(build_help(router), "/start")
