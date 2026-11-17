"""Whole-run execution with fake launcher, GitHub and harness output."""

import json
from datetime import UTC, datetime
from types import SimpleNamespace

import pytest

from homelab.runs import slices, usage
from homelab.tasks.executors import Executor


def executor(harness="codex"):
    return Executor(harness=harness, model="fixture", effort="high")


def test_usage_selects_in_order_and_waits_for_earliest_reset():
    now = 10_000
    readings = {
        "codex": usage.Reading(99, now + 500, now, 5, now + 1000),
        "claude": usage.Reading(89, now + 300, now, 10, now + 2000),
    }
    choices = [executor(), executor("claude")]
    assert usage.choose(choices, readings.get, now) == (choices[1], None)
    readings["claude"] = usage.Reading(90, now + 300, now, 10, now + 2000)
    assert usage.choose(choices, readings.get, now) == (None, now + 300)
    readings["claude"] = usage.Reading(100, now - 1, now - 4000, 100, now + 2000)
    assert usage.choose(choices, readings.get, now) == (choices[1], None)


def test_claude_requires_recent_reading_and_probe_is_lazy_hourly(tmp_path):
    now = [10_000]
    calls = []

    def probe():
        calls.append(now[0])
        return usage.Reading(20, now[0] + 10_000, now[0], 30, now[0] + 20_000)

    cache = usage.ClaudeCache(tmp_path / "usage.json", probe, clock=lambda: now[0])
    choices = [executor(), executor("claude")]
    assert (
        usage.choose(
            choices, lambda h: usage.Reading(1, 20_000, 0) if h == "codex" else cache.read(), now[0]
        )[0]
        == choices[0]
    )
    assert calls == []
    assert cache.read().five == 20
    now[0] += 1801
    assert cache.read().five == 20 and len(calls) == 1
    now[0] += 1800
    cache.read()
    assert len(calls) == 2
    restarted = usage.ClaudeCache(tmp_path / "usage.json", probe, clock=lambda: now[0])
    restarted.read()
    assert len(calls) == 2


def test_usage_parsers_take_last_claude_event_and_codex_windows():
    codex = {
        "primary": {"used_percent": 99, "window_minutes": 300, "resets_at": 1500},
        "secondary": {"used_percent": 10, "window_minutes": 10080, "resets_at": 2000},
    }
    assert usage.codex_reading(json.dumps(codex), 1000).five == 99

    def event(value):
        return json.dumps(
            {
                "type": "rate_limit_event",
                "rate_limit_info": {
                    "unifiedWindows": {
                        "five_hour": {"utilization": value, "resetsAt": 1500},
                        "seven_day": {"utilization": 0.4, "resetsAt": 2000},
                    }
                },
            }
        )

    reading = usage.claude_reading(event(0.8) + "\n{}\n" + event(0.9), 1000)
    assert (reading.five, reading.weekly, reading.reset) == (90, 40, 1500)


@pytest.mark.parametrize(
    "kind,status,resumable,reason",
    [
        ("handover", "done", True, None),
        ("question", "question", True, None),
        ("escalation", "escalated", True, "executor_gave_up"),
        ("waiting", "waiting", True, "client_step"),
    ],
)
def test_final_messages(kind, status, resumable, reason):
    result = slices.final_state({"kind": kind, "body": "Private final"})
    assert result == {
        "status": status,
        "resumable": resumable,
        "status_reason": reason,
        "final_message": "Private final",
        "resume_at": None,
    }


@pytest.mark.parametrize("harness", ["codex", "claude"])
def test_harness_outputs_and_resume_commands(harness):
    selected = executor(harness)
    final = {"kind": "question", "body": "Private question"}
    events = (
        [
            {"type": "thread.started", "thread_id": "session-fixture"},
            {
                "type": "item.completed",
                "item": {"type": "agent_message", "text": json.dumps(final)},
            },
        ]
        if harness == "codex"
        else [{"type": "result", "session_id": "session-fixture", "structured_output": final}]
    )
    answer = slices.parse_output(harness, "\n".join(json.dumps(e) for e in events))
    assert answer.session == "session-fixture" and answer.final == final
    command = slices.command(selected, "Private answer", "session-fixture")
    assert "resume" in " ".join(command) and "session-fixture" in command
    assert command[-1] == "Private answer"


@pytest.mark.parametrize("mode", ["read", "write"])
def test_turn_read_mode_never_bundles_or_pushes(tmp_path, monkeypatch, mode):
    calls = []
    document = {
        "repository": "owner/repo",
        "branch": "slice/test",
        "mode": mode,
        "base_commit": "a" * 40,
        "work": {"allowed_files": ["src"], "pinned_paths": ["src/pinned"], "check_command": "true"},
    }
    record = {
        "id": "fixture",
        "request": document,
        "work_id": "run-" + "a" * 32,
        "executor": executor().selection(),
        "session_id": "existing",
    }

    def capture(request, out, err, seconds, **kwargs):
        calls.append(request)
        assert request["read_only"] is (mode == "read")
        assert request["folders"] == {"work": record["work_id"], "run": record["work_id"]}
        out.write_text(
            json.dumps({"type": "thread.started", "thread_id": "existing"})
            + "\n"
            + json.dumps(
                {
                    "type": "item.completed",
                    "item": {
                        "type": "agent_message",
                        "text": json.dumps({"kind": "handover", "body": "Finished"}),
                    },
                }
            )
        )
        err.write_bytes(b"")
        return 0, "exited"

    monkeypatch.setattr(slices.launcher, "capture", capture)
    monkeypatch.setattr(slices, "deliver", lambda *a, **kw: calls.append("push") or ["b" * 40])
    result, session, commits, metrics = slices.execute_turn(record, "Goal", tmp_path, "turn-1")
    assert result["status"] == "done" and session == "existing"
    assert commits == (["b" * 40] if mode == "write" else [])
    assert ("push" in calls) is (mode == "write")


def test_usage_limit_harness_output_keeps_session_and_waits(tmp_path, monkeypatch):
    def capture(request, out, err, seconds, **kwargs):
        out.write_text(
            json.dumps({"type": "thread.started", "thread_id": "same-session"})
            + '\n{"type":"error","message":"Usage limit reached"}\n'
        )
        err.write_bytes(b"")
        return 1, "exited"

    monkeypatch.setattr(slices.launcher, "capture", capture)
    monkeypatch.setattr(slices, "deliver", lambda *a: pytest.fail("must not deliver"))
    record = {
        "request": {"mode": "write"},
        "work_id": "run-" + "a" * 32,
        "executor": executor().selection(),
        "session_id": None,
    }
    result, session, commits, metrics = slices.execute_turn(record, "Goal", tmp_path, "turn")
    assert result["status_reason"] == "usage_limit" and session == "same-session"
    assert not commits


def test_interruption_retries_only_once():
    assert slices.interrupted_state(0)["status"] == "queued"
    assert slices.interrupted_state(1) == {
        "status": "failed",
        "status_reason": "interrupted",
        "resumable": False,
    }


def test_private_clone_uses_app_and_launcher_owned_copy(tmp_path, monkeypatch):
    calls = []
    monkeypatch.setattr(slices.launcher, "request", lambda r: calls.append(r))
    monkeypatch.setattr(slices, "installation_token", lambda r: "FAKE-CLONE-TOKEN")
    monkeypatch.setattr(slices, "remote_refs", lambda r, t: ("trunk", {"trunk": "a" * 40}))

    def capture(request, out, err, seconds, **kwargs):
        calls.append(request)
        out.write_bytes(b"")
        err.write_bytes(b"")
        return 0, "exited"

    monkeypatch.setattr(slices.launcher, "capture", capture)
    record = {
        "id": SimpleNamespace(hex="a" * 32),
        "request": {
            "repository": "owner/repo",
            "branch": "slice/new",
            "base_commit": "a" * 40,
            "mode": "write",
        },
    }
    assert slices.prepare(record, tmp_path) == "run-" + "a" * 32
    assert calls[0] == {"op": "cli-create", "work_id": "run-" + "a" * 32}
    clone = calls[1]
    assert clone["shape"] == "clone" and clone["workspace"] == "cli"
    assert clone["branch"] == "trunk" and clone["env"]["GIT_CONFIG_VALUE_0"].startswith(
        "Authorization: Basic "
    )
    assert "FAKE-CLONE-TOKEN" not in json.dumps(calls)
    assert "env" not in calls[0]
    assert calls[2]["env"] == {}  # The checkout container never receives the credential.


def test_wait_state_has_utc_resume_time():
    assert slices.waiting_state(1000)["resume_at"] == datetime.fromtimestamp(1000, UTC)


@pytest.mark.parametrize("harness", ["codex", "claude"])
def test_contract_sized_prompts_use_descriptor_without_entering_request(
    tmp_path, monkeypatch, harness
):
    goal = "🌍" * 200_000
    assets = [{"text": "x" * 199_980}]
    record = {
        "request": {"mode": "read", "assets": assets, "work": {"allowed_files": None}},
        "executor": executor(harness).selection(),
        "work_id": "run-" + "a" * 32,
        "session_id": None,
    }

    def capture(request, out, err, seconds, *, stdin=None, **kwargs):
        assert request["stdin"] is True and len(json.dumps(request).encode()) < 65536
        payload = json.load(stdin)
        assert payload["goal"] == goal and payload["assets"] == assets
        assert goal not in json.dumps(request)
        final = {"kind": "handover", "body": "Finished"}
        if harness == "codex":
            assert request["command"][-1] == "-"
            events = [
                {"type": "thread.started", "thread_id": "session"},
                {
                    "type": "item.completed",
                    "item": {"type": "agent_message", "text": json.dumps(final)},
                },
            ]
        else:
            events = [{"type": "result", "session_id": "session", "structured_output": final}]
        out.write_text("\n".join(map(json.dumps, events)))
        err.write_bytes(b"")
        return 0, "exited"

    monkeypatch.setattr(slices.launcher, "capture", capture)
    result, session, commits, metrics = slices.execute_turn(record, goal, tmp_path, "large-turn")
    assert result["status"] == "done"


def test_failed_delivery_keeps_measured_turn_usage(tmp_path, monkeypatch):
    def capture(request, out, err, seconds, **kwargs):
        out.write_text(
            json.dumps(
                {
                    "type": "result",
                    "session_id": "session",
                    "structured_output": {"kind": "handover", "body": "Finished"},
                    "usage": {"input_tokens": 10, "output_tokens": 5},
                    "total_cost_usd": 0.02,
                }
            )
        )
        err.write_bytes(b"")
        return 0, "exited"

    def refuse(*args, **kwargs):
        raise slices.Refused("outside_allowed_files")

    monkeypatch.setattr(slices.launcher, "capture", capture)
    monkeypatch.setattr(slices, "deliver", refuse)
    record = {
        "request": {"mode": "write"},
        "work_id": "run-" + "a" * 32,
        "executor": executor("claude").selection(),
        "session_id": None,
    }
    result, session, commits, metrics = slices.execute_turn(record, "Goal", tmp_path, "turn")
    assert result["status"] == "failed" and result["status_reason"] == "outside_allowed_files"
    assert metrics["fresh"] == 10 and metrics["usd"] == 0.02
    assert session == "session" and not commits


def test_valid_final_overrides_transient_limit_events(tmp_path, monkeypatch):
    def capture(request, out, err, seconds, **kwargs):
        events = [
            {"type": "thread.started", "thread_id": "same-session"},
            {"type": "error", "message": "Reconnecting... 1/5 (rate limit reached)"},
            {
                "type": "item.completed",
                "item": {
                    "type": "agent_message",
                    "text": json.dumps({"kind": "handover", "body": "done"}),
                },
            },
            {"type": "turn.completed"},
        ]
        out.write_text("\n".join(map(json.dumps, events)))
        err.write_bytes(b"")
        return 0, "exited"

    monkeypatch.setattr(slices.launcher, "capture", capture)
    monkeypatch.setattr(slices, "deliver", lambda *a, **kw: ["b" * 40])
    record = {
        "request": {"mode": "write"},
        "work_id": "run-" + "a" * 32,
        "executor": executor().selection(),
        "session_id": None,
    }
    result, session, commits, metrics = slices.execute_turn(record, "Goal", tmp_path, "turn")
    assert result["status"] == "done"
    assert result["status_reason"] is None
    assert result["resumable"] is True
    assert commits == ["b" * 40]


def test_waiting_final_mentioning_rate_limit_is_a_client_step(tmp_path, monkeypatch):
    def capture(request, out, err, seconds, **kwargs):
        events = [
            {"type": "thread.started", "thread_id": "same-session"},
            {
                "type": "item.completed",
                "item": {
                    "type": "agent_message",
                    "text": json.dumps(
                        {"kind": "waiting", "body": "Review the rate-limit middleware"}
                    ),
                },
            },
            {"type": "turn.completed"},
        ]
        out.write_text("\n".join(map(json.dumps, events)))
        err.write_bytes(b"")
        return 0, "exited"

    monkeypatch.setattr(slices.launcher, "capture", capture)
    record = {
        "request": {"mode": "read"},
        "work_id": "run-" + "a" * 32,
        "executor": executor().selection(),
        "session_id": None,
    }
    result, session, commits, metrics = slices.execute_turn(record, "Goal", tmp_path, "turn")
    assert result["status"] == "waiting"
    assert result["status_reason"] == "client_step"
    assert result["resume_at"] is None
