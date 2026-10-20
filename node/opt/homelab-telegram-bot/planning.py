"""Read-only planning mirror. Private paths and Git output never enter logs."""

from __future__ import annotations

import asyncio
import json
import math
import os
import re
import shlex
import subprocess
import tempfile
import threading
from dataclasses import dataclass, field
from datetime import UTC, datetime, timedelta
from pathlib import Path

from router import Capability, Executor

CONFIG = Path("/etc/homelab-telegram-bot/planning.json")
DATA = Path("/srv/homelab/frontline")
KEY = "/etc/homelab-telegram-bot/planning_ed25519"
HOSTS = "/etc/homelab-telegram-bot/known_hosts"


@dataclass(frozen=True)
class Config:
    repo_url: str
    branch: str
    state_path: str
    outbox_path: str
    poll_seconds: int = 60
    context_files: list[str] = field(default_factory=list)


def load_config(log, path=CONFIG):
    try:
        values = json.loads(path.read_text())
        config = Config(**values)
        if not re.fullmatch(r"git@github\.com:[\w.-]+/[\w.-]+(?:\.git)?", config.repo_url):
            raise ValueError("SSH repository required")
        if not re.fullmatch(r"[A-Za-z0-9][A-Za-z0-9_./-]*", config.branch):
            raise ValueError("invalid branch")
        if not isinstance(config.context_files, list) or len(config.context_files) > 16:
            raise ValueError("invalid context files")
        for name in (config.state_path, config.outbox_path, *config.context_files):
            if not isinstance(name, str):
                raise ValueError("invalid path")
            path_value = Path(name)
            if path_value.is_absolute() or ".." in path_value.parts or not path_value.parts:
                raise ValueError("relative path required")
        if type(config.poll_seconds) is not int or not 10 <= config.poll_seconds <= 3600:
            raise ValueError("invalid interval")
        return config
    except FileNotFoundError:
        log("planning not configured; feature disabled")
    except (OSError, ValueError, TypeError):
        log("WARNING: planning configuration invalid; feature disabled")
    return None


def data_ready():
    # Query PID 1 rather than a mount retained in the bot's sandbox namespace.
    return (
        subprocess.run(
            ["/usr/bin/systemctl", "is-active", "--quiet", "homelab-data.target"],
            stdout=subprocess.DEVNULL,
            stderr=subprocess.DEVNULL,
            timeout=5,
            check=False,
        ).returncode
        == 0
    )


def group_pending(slices):
    groups = {name: [] for name in ("needs attention", "running", "scheduled", "ready", "planned")}
    for item in slices:
        state = item["state"]
        if state == "accepted":
            continue
        attention = item.get("attention")
        unresolved = False
        if attention:
            kind = attention["kind"]
            unresolved = (
                (kind == "merge" and not attention["approved"])
                or (kind in ("question", "escalation") and attention["answer"] is None)
                or kind in ("node-step", "owner-check")
            )
        if unresolved or state in ("in review", "escalated"):
            group = "needs attention"
        elif state in ("issued", "awaiting handover"):
            group = "running"
        elif item.get("slot"):
            group = "scheduled"
        elif state == "ready":
            group = "ready"
        else:
            group = "planned"
        groups[group].append(item)
    groups["scheduled"].sort(key=lambda item: datetime.fromisoformat(item["slot"]))
    return groups


def pending_summary(slices):
    groups = group_pending(slices)
    sections = []
    for label, items in groups.items():
        if label == "planned":
            sections.append(f"Planned: {len(items)}")
        else:
            sections.append(
                label.capitalize()
                + f" ({len(items)})"
                + "".join(
                    f"\n{item['id']} — {item['title']}"
                    + (f" — {item['slot']}" if label == "scheduled" else "")
                    for item in items
                )
            )
    return "\n\n".join(sections)


def slice_text(item, prefix=""):
    """Keep all fields, with prose paragraphs visible rather than JSON-escaped."""
    sections = []
    for key, value in item.items():
        name = prefix + key
        if isinstance(value, dict) and value:
            sections.append(slice_text(value, name + "."))
        elif isinstance(value, str):
            sections.append(f"{name}: {value}")
        else:
            sections.append(f"{name}: {json.dumps(value, ensure_ascii=False)}")
    return "\n\n".join(sections)


def notice(event):
    text = event["text"]
    if len(text) > 600:
        text = text[:600] + "…"
    return f"{event['id']} — {event['event']}\n{text}\n/slice {event['id']}"


class Planning:
    def __init__(self, config, log, *, directory=DATA, ready=data_ready):
        self.config = config
        self.log = log
        self.directory = directory
        self.clone = directory / "clone"
        self.marker = directory / "outbox-marker.json"
        self.ready = ready
        self.lock = threading.Lock()

    def git_environment(self):
        return {
            "PATH": "/usr/bin:/bin",
            "HOME": str(self.directory),
            "LANG": "C.UTF-8",
            "GIT_CONFIG_NOSYSTEM": "1",
            "GIT_CONFIG_GLOBAL": "/dev/null",
            "GIT_TERMINAL_PROMPT": "0",
            "GIT_ALLOW_PROTOCOL": "ssh",
            "GIT_SSH_COMMAND": "/usr/bin/ssh -F /dev/null -o BatchMode=yes "
            "-o IdentitiesOnly=yes -o IdentityAgent=none -o StrictHostKeyChecking=yes "
            f"-o UserKnownHostsFile={shlex.quote(HOSTS)} "
            "-o GlobalKnownHostsFile=/dev/null -o ConnectTimeout=10 "
            f"-i {shlex.quote(KEY)}",
        }

    def git(self, *args):
        subprocess.run(
            ["/usr/bin/git", "-c", "core.hooksPath=/dev/null", *args],
            env=self.git_environment(),
            cwd=self.directory,
            stdout=subprocess.DEVNULL,
            stderr=subprocess.DEVNULL,
            timeout=45,
            check=True,
        )

    def refresh(self):
        # Never create anything against the bare mountpoint before unlock.
        try:
            if not self.ready():
                return "The data volume is locked; planning is unavailable."
            if self.config is None:
                return "Planning is not configured."
            self.directory.mkdir(mode=0o700, exist_ok=True)
            if not self.clone.exists():
                with tempfile.TemporaryDirectory(prefix=".clone-", dir=self.directory) as temp:
                    staged = Path(temp) / "repo"
                    self.git(
                        "clone",
                        "--single-branch",
                        "--branch",
                        self.config.branch,
                        "--",
                        self.config.repo_url,
                        str(staged),
                    )
                    staged.rename(self.clone)
                self.log("planning clone ready")
            else:
                self.git(
                    "-C",
                    str(self.clone),
                    "pull",
                    "--ff-only",
                    "--no-rebase",
                    "origin",
                    self.config.branch,
                )
                self.log("planning pull succeeded")
        except (OSError, subprocess.SubprocessError):
            self.log("WARNING: planning sync failed")
            return "Planning sync failed; try again shortly."
        return None

    def read_file(self, relative):
        path = (self.clone / relative).resolve()
        if not path.is_relative_to(self.clone.resolve()):
            raise ValueError("path outside clone")
        return path.read_text()

    def events(self):
        raw = self.read_file(self.config.outbox_path)
        # Split only on JSONL newlines, preserving Unicode separators inside text.
        # A final newline is optional; malformed lines fail closed without advancing.
        lines = raw.split("\n")
        if lines[-1] == "":
            lines.pop()
        events = [json.loads(line) for line in lines]
        for event in events:
            if not all(isinstance(event[key], str) for key in ("at", "id", "event", "text")):
                raise ValueError("invalid event")
            if datetime.fromisoformat(event["at"]).tzinfo is None:
                raise ValueError("timestamp needs timezone")
        return events

    def command(self, name, args):
        if name == "recap":
            try:
                hours = float(args[0]) if args else 24
                if len(args) > 1 or not math.isfinite(hours) or hours <= 0:
                    raise ValueError("invalid hours")
                cutoff = datetime.now(UTC) - timedelta(hours=hours)
            except (ValueError, OverflowError):
                return "Usage: /recap [hours] (positive hours)"
        elif (name == "slice" and len(args) != 1) or (name == "pending" and args):
            return "Usage: /slice <id>" if name == "slice" else "Usage: /pending"
        with self.lock:
            failure = self.refresh()
            if failure:
                return failure
            try:
                if name == "recap":
                    grouped = {}
                    for event in self.events():
                        if datetime.fromisoformat(event["at"]) >= cutoff:
                            grouped.setdefault(event["id"], []).append(event)
                    return (
                        "\n\n".join(
                            slice_id
                            + "\n"
                            + "\n".join(
                                f"{event['at']} — {event['event']}\n{event['text']}"
                                for event in events
                            )
                            for slice_id, events in grouped.items()
                        )
                        or "No events in that period."
                    )
                state = json.loads(self.read_file(self.config.state_path))
                if name == "slice":
                    item = next((s for s in state["slices"] if s["id"] == args[0]), None)
                    return slice_text(item) if item else "Slice not found."
                return pending_summary(state["slices"])
            except (OSError, ValueError, KeyError, TypeError):
                self.log("WARNING: planning data invalid or unavailable")
                return "Planning data is unavailable; try again shortly."

    def save_marker(self, marker):
        # Replace atomically, and flush both data and rename before acknowledging.
        # A failed write leaves the prior marker so delivery is retried, never skipped.
        with tempfile.NamedTemporaryFile(mode="w", dir=self.directory, delete=False) as file:
            temporary = Path(file.name)
            try:
                json.dump(marker, file)
                file.flush()
                os.fsync(file.fileno())
                os.replace(temporary, self.marker)
                descriptor = os.open(self.directory, os.O_RDONLY)
                try:
                    os.fsync(descriptor)
                finally:
                    os.close(descriptor)
            finally:
                temporary.unlink(missing_ok=True)

    def notification_batch(self):
        with self.lock:
            if self.refresh():
                return None
            events = self.events()
            try:
                marker = json.loads(self.marker.read_text())
            except FileNotFoundError:
                self.save_marker({"line": len(events), "sent_to": []})
                self.log(f"planning outbox initialized lines={len(events)}; history skipped")
                return None
            line, sent = marker["line"], marker["sent_to"]
            if (
                type(line) is not int
                or not 0 <= line <= len(events)
                or not isinstance(sent, list)
                or any(type(user) is not int for user in sent)
                or (line == len(events) and sent)
            ):
                raise ValueError("invalid marker or truncated outbox")
            return events, marker

    async def notify_once(self, recipients, send):
        batch = await asyncio.to_thread(self.notification_batch)
        if batch is None:
            return
        events, marker = batch
        for index in range(marker["line"], len(events)):
            sent = set(marker["sent_to"])
            for user in sorted(recipients - sent):
                # Success means the Bot API confirmed delivery. Any exception leaves
                # this recipient pending. A crash before the marker may duplicate it.
                await send(user, notice(events[index]))
                sent.add(user)
                marker = {"line": index, "sent_to": sorted(sent)}
                if recipients <= sent:
                    marker = {"line": index + 1, "sent_to": []}
                # Keep this tiny durable write synchronous: cancellation must not
                # leave a background thread racing a final retry or shutdown.
                self.save_marker(marker)
            if marker["line"] == index:
                marker = {"line": index + 1, "sent_to": []}
                self.save_marker(marker)
            self.log(f"planning notice sent line={index + 1} recipients={len(recipients)}")

    async def run(self, recipients, send):
        while True:
            try:
                await self.notify_once(recipients, send)
            except Exception:  # noqa: BLE001 - retry without logging content or stopping the bot
                self.log("WARNING: planning notice failed; retry pending")
            await asyncio.sleep(self.config.poll_seconds)

    def register(self, router):
        for name, summary, usage in (
            ("pending", "planning work needing attention", "/pending"),
            ("recap", "recent planning events", "/recap [hours]"),
            ("slice", "show a planning slice", "/slice <id>"),
        ):
            router.register(
                Executor(
                    "/" + name,
                    Capability.READ,
                    lambda args, command=name: self.command(command, args),
                    summary,
                    usage=usage,
                    log_args=False,
                )
            )
