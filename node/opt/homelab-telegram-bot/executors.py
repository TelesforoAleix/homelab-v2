"""Each executor declares its capability; the router checks entitlement.

Host metrics come from /proc, /etc/hostname and statvfs without subprocesses.
Only /restart executes systemctl, with an exact argv and no shell. It retains
NoNewPrivileges and an empty capability set: polkit authorises one user, unit
and verb outside this process, while the restart allowlist is a second gate.
"""

from __future__ import annotations

import json
import os
import socket
import subprocess
import urllib.error
import urllib.request

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
    with open("/proc/uptime", encoding="utf-8") as fh:
        return _fmt_duration(float(fh.read().split()[0]))


def host_load() -> str:
    with open("/proc/loadavg", encoding="utf-8") as fh:
        one, five, fifteen = fh.read().split()[:3]
    return f"{one} {five} {fifteen}"


def host_memory() -> str:
    vals = {}
    with open("/proc/meminfo", encoding="utf-8") as fh:
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
    used and reported 19.3G where df said 8.9G (root-reserved space).
    """
    st = os.statvfs(path)
    total = st.f_blocks * st.f_frsize
    used = (st.f_blocks - st.f_bfree) * st.f_frsize
    avail = st.f_bavail * st.f_frsize
    pct = (used / (used + avail) * 100) if (used + avail) else 0
    return f"{_fmt_bytes(used)} used, {_fmt_bytes(avail)} free of {_fmt_bytes(total)} ({pct:.0f}%)"


def host_name() -> str:
    try:
        with open("/etc/hostname", encoding="utf-8") as fh:
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
            log(f"/restart {unit}: could not execute systemctl: {type(exc).__name__}")
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
# Knowledge answers
# --------------------------------------------------------------------------

ANSWER_URL = "http://127.0.0.1:8000/v1/knowledge/answer"


def _ask(args: list[str]) -> str:
    """Return text to send, never commands to dispatch or tools to execute.

    The API sends only the question and retrieved chunks to its configured chat
    route. Model output is never parsed as a command or returned to the router.
    """
    question = " ".join(args).strip()
    if not question:
        return "Usage: /ask <question>\nAnswers from indexed knowledge, with numbered sources."

    request = urllib.request.Request(
        ANSWER_URL,
        data=json.dumps({"question": question}).encode("utf-8"),
        headers={"Content-Type": "application/json"},
        method="POST",
    )
    try:
        # Allow retrieval plus the API's 60-second model timeout. No helper,
        # provider credentials or third-party client belongs in this process.
        with urllib.request.urlopen(request, timeout=90) as response:
            reply = json.loads(response.read().decode("utf-8"))
    except (OSError, urllib.error.URLError):
        return "The knowledge service is down. Try again later; /status still works."
    except (ValueError, UnicodeError):
        return "The knowledge service returned an invalid response."

    try:
        answer = reply["answer"]
        refused = reply["refused"]
        if not isinstance(answer, str) or not answer.strip() or not isinstance(refused, bool):
            raise ValueError("invalid answer")
        if refused:
            return answer
        sources = reply["sources"]
        lines = [answer, "", "Sources:"]
        for source in sources:
            number, title = source["number"], source["title"]
            if type(number) is not int or number < 1 or not isinstance(title, str):
                raise ValueError("invalid source")
            lines.append(f"[{number}] {title}")
        return "\n".join(lines)
    except (KeyError, TypeError, ValueError):
        return "The knowledge service returned an invalid response."


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
        # Derive the legend from the registry too, so it cannot describe a
        # capability that no registered command uses.
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
        # Shared by /help and the Telegram command menu.
        summary="list the commands",
    )


def register_all(router, *, allowed_units: set[str], log) -> None:
    router.register(
        Executor("/status", Capability.READ, _status, "host, uptime, load, memory, disk")
    )
    router.register(Executor("/disk", Capability.READ, _disk, "root filesystem usage"))
    router.register(Executor("/uptime", Capability.READ, _uptime, "uptime and load average"))
    router.register(make_restart(allowed_units, log))
    # /ask changes no host state, and its prose must stay out of the journal.
    router.register(
        Executor(
            "/ask",
            Capability.READ,
            _ask,
            "ask indexed knowledge, with cited sources",
            usage="/ask <question>",
            log_args=False,
        )
    )
    router.register(build_help(router), "/start")
