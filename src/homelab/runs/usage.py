"""Subscription windows, read only when an ordered executor choice needs them."""

import json
import math
import os
import threading
import time
from dataclasses import asdict, dataclass
from pathlib import Path

_cache_mutexes = {}


@dataclass(frozen=True)
class Reading:
    five: float
    reset: float
    seen: float
    weekly: float | None = None
    weekly_reset: float | None = None


def number(value):
    if type(value) not in (int, float) or not math.isfinite(value) or value < 0:
        raise ValueError("invalid_usage")
    return value


def codex_reading(text, now):
    try:
        value = json.loads(text)
        windows = {
            number(w["window_minutes"]): w
            for w in value.values()
            if isinstance(w, dict) and "window_minutes" in w
        }
        five, week = windows[300], windows.get(10080)
        return Reading(
            number(five["used_percent"]),
            number(five["resets_at"]),
            now,
            number(week["used_percent"]) if week else None,
            number(week["resets_at"]) if week else None,
        )
    except (ValueError, KeyError, TypeError, AttributeError):
        return None


def claude_reading(text, now):
    result = None
    for line in text.splitlines():
        try:
            event = json.loads(line)
            if event.get("type") != "rate_limit_event":
                continue
            windows = event["rate_limit_info"]["unifiedWindows"]
            five, week = windows["five_hour"], windows.get("seven_day")
            utilization = number(five["utilization"])
            if utilization > 1:
                raise ValueError
            result = Reading(
                utilization * 100,
                number(five["resetsAt"]),
                now,
                number(week["utilization"]) * 100 if week else None,
                number(week["resetsAt"]) if week else None,
            )
        except (ValueError, KeyError, TypeError, AttributeError):
            continue
    return result


def choose(executors, read, now):
    resets = []
    for executor in executors:
        if executor.configuration:
            return executor, None
        reading = read(executor.harness)
        if reading is None:
            resets.append(now + 3600)
            continue
        reset = reading.reset
        allows = reset <= now or (
            reading.five < (99 if executor.harness == "codex" else 90)
            and (executor.harness == "codex" or now - reading.seen < 3600)
        )
        if allows:
            return executor, None
        resets.append(reset if reset > now else now + 3600)
    return None, min(resets) if resets else now + 3600


class ClaudeCache:
    """A failed probe also consumes its hourly opportunity, including across restarts."""

    def __init__(self, path: Path, probe, *, clock=time.time):
        self.path, self.probe, self.clock = path, probe, clock
        self.mutex = _cache_mutexes.setdefault(str(path.resolve()), threading.Lock())

    def read(self):
        with self.mutex:
            now = self.clock()
            try:
                data = json.loads(self.path.read_text())
                previous = Reading(**data["reading"]) if data["reading"] else None
                probed = number(data["probed"])
            except (OSError, ValueError, KeyError, TypeError):
                previous, probed = None, None
            if (previous is None or now - previous.seen > 1800) and (
                probed is None or now - probed >= 3600
            ):
                self.save(previous, now)
                fresh = self.probe()
                if fresh is not None:
                    previous = fresh
                self.save(previous, now)
            return previous

    def save(self, reading, probed):
        self.path.parent.mkdir(mode=0o700, parents=True, exist_ok=True)
        temporary = self.path.with_suffix(".tmp")
        with temporary.open("w") as stream:
            os.fchmod(stream.fileno(), 0o600)
            json.dump({"probed": probed, "reading": asdict(reading) if reading else None}, stream)
            stream.flush()
            os.fsync(stream.fileno())
        temporary.replace(self.path)
