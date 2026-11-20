"""Copy retention uses a fake clock and launcher; sessions outlive their work copy."""

from contextlib import nullcontext
from datetime import UTC, datetime, timedelta

import pytest

from homelab.runs import launcher
from homelab.tasks import slices
from homelab.tasks.executors import load_executors

NOW = datetime(2026, 11, 17, tzinfo=UTC)


class Database:
    def __init__(self, rows):
        self.rows = rows
        self.updated = []

    def transaction(self):
        return nullcontext()

    def execute(self, sql, args=()):
        if sql.startswith("UPDATE"):
            self.updated.append(args)
        return self

    def fetchall(self):
        return self.rows


@pytest.mark.parametrize(
    "status,age,expected",
    [
        ("failed", 0, True),
        ("cancelled", 0, True),
        ("done", 72, True),
        ("question", 73, True),
        ("waiting", 71, False),
        ("escalated", 72, True),
        ("running", 100, False),
        ("queued", 100, False),
    ],
)
def test_retention(monkeypatch, status, age, expected):
    db = Database([("id", "run-work", status, NOW - timedelta(hours=age))])
    requests = []
    monkeypatch.setattr(launcher, "request", requests.append)
    assert slices.sweep(db, now=NOW) == int(expected)
    assert requests == ([{"op": "cli-remove", "work_id": "run-work"}] if expected else [])
    assert bool(db.updated) == expected


def test_busy_copy_is_retried_without_changing_record(monkeypatch):
    db = Database([("id", "run-work", "failed", NOW)])

    def busy(request):
        raise launcher.Refused("launcher_refused")

    monkeypatch.setattr(launcher, "request", busy)
    assert slices.sweep(db, now=NOW) == 0
    assert not db.updated
    monkeypatch.setattr(launcher, "request", lambda request: None)
    assert slices.sweep(db, now=NOW) == 1


def test_review_catalogue():
    table = load_executors()
    for level, model in [("light", "sonnet"), ("standard", "sonnet"), ("strong", "sonnet")]:
        assert [executor.selection() for executor in table["review"][level]] == [
            {"harness": "claude", "model": model},
            {"harness": "codex", "model": "gpt-6.1-sol", "effort": "high"},
        ]


def test_queue_sweeps_other_states_hourly_and_failures_each_poll(monkeypatch, tmp_path):
    from homelab.runs import slice_loop

    now = [0]
    calls = []
    monkeypatch.setattr(slice_loop.time, "monotonic", lambda: now[0])
    monkeypatch.setattr(slices, "claim", lambda connection: None)
    monkeypatch.setattr(slices, "sweep", lambda connection, **kw: calls.append(kw["failed_only"]))
    service = slice_loop.Service(lambda: nullcontext(object()), tmp_path, lambda: None)

    class Stop:
        def is_set(self):
            return now[0] > 3600

        def wait(self, seconds):
            now[0] += 1800

    service.stop = Stop()
    try:
        service.loop()
        assert calls == [False, True, False]
    finally:
        service.pool.shutdown()
