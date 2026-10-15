import importlib.machinery
import importlib.util
import io
import json
import os
import subprocess
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]


def module(name, filename):
    loader = importlib.machinery.SourceFileLoader(name, str(ROOT / "mac" / filename))
    spec = importlib.util.spec_from_loader(name, loader)
    result = importlib.util.module_from_spec(spec)
    loader.exec_module(result)
    return result


profile = module("usage_profile", "homelab-profile")
snapshot = module("usage_snapshot", "usage-snapshot")


# Recorded shape of the gateway's journald audit lines (Compose prefixes each container line).
def audit_line(purpose, status, blocked, real_model="unknown"):
    return (
        f"litellm-1  | model_call purpose={purpose} real_model={real_model} status={status} "
        f"elapsed_seconds=0.188 upstream={str(not blocked).lower()} "
        f"profile_blocked={str(blocked).lower()} profile_paused=false"
    )


AUDIT = "\n".join(
    [
        audit_line("vision", 503, True, "vision"),
        audit_line("vision", 503, True, "vision").replace("paused=false", "paused=true"),
        audit_line("image-gpt-2.5-flare", 503, True),
        audit_line("vision", 200, False, "vision"),
        audit_line("embed-large-bulk", 503, True),
        "litellm-1  | unrelated line mentioning profile_blocked=true purpose=chat",
    ]
)
MODEL_INFO = {
    "data": [
        {"model_name": "vision", "model_info": {"mode": "chat"}},
        {"model_name": "image-gpt-2.5-flare", "model_info": {"seconds_per_image": 10.074}},
        {"model_name": "embed-large-bulk", "model_info": {"seconds_per_image": "fast"}},
    ]
}


def test_waiting_counts_blocked_audit_lines_and_estimates_only_published_timings(monkeypatch):
    commands = []

    def node(command):
        commands.append(command)
        return json.dumps(MODEL_INFO) if "model/info" in command else AUDIT

    monkeypatch.setattr(profile, "node", node)
    assert profile.waiting_report() == {
        "since_hours": 24,
        "purposes": [
            {
                "purpose": "embed-large-bulk",
                "count": 1,
                "seconds_per_request": None,
                "estimated_seconds": None,
            },
            {
                "purpose": "image-gpt-2.5-flare",
                "count": 1,
                "seconds_per_request": 10.074,
                "estimated_seconds": 10.074,
            },
            {
                "purpose": "vision",
                "count": 2,
                "seconds_per_request": None,
                "estimated_seconds": None,
            },
        ],
    }
    audit = next(c for c in commands if "journalctl" in c)
    assert "-u homelab.service" in audit and "--since '24 hours ago'" in audit
    assert all(word not in " ".join(commands) for word in ("rm ", "tee", ">", "restart", "POST"))


def test_waiting_reads_the_node_only_through_the_agent_account(monkeypatch):
    calls = []
    monkeypatch.setattr(
        profile.subprocess,
        "run",
        lambda args, **kwargs: calls.append(args) or subprocess.CompletedProcess(args, 0, "", ""),
    )
    profile.node("true")
    assert calls[0][0] == "/usr/bin/ssh" and calls[0][-2:] == ["homelab-agent", "true"]
    assert "BatchMode=yes" in calls[0]


def test_waiting_with_nothing_blocked():
    assert profile.waiting_purposes("", {}) == []


def write_session(path, entries):
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text("\n".join(json.dumps(entry) for entry in entries) + "\n{not json\n")


def codex_entry(stamp, five, seven, limit_id="codex", windows=(300, 10080)):
    return {
        "timestamp": stamp,
        "type": "event_msg",
        "payload": {
            "type": "token_count",
            "rate_limits": {
                "limit_id": limit_id,
                "primary": {
                    "used_percent": five,
                    "window_minutes": windows[0],
                    "resets_at": 1800000000,
                },
                "secondary": {
                    "used_percent": seven,
                    "window_minutes": windows[1],
                    "resets_at": 1800500000,
                },
            },
        },
    }


def test_codex_usage_reads_the_newest_five_hour_and_weekly_entry(tmp_path):
    root = tmp_path / "sessions"
    old = root / "2026/10/14/rollout-a.jsonl"
    new = root / "2026/10/15/rollout-b.jsonl"
    write_session(old, [codex_entry("2026-10-14T10:00:00.000Z", 90.0, 90.0)])
    write_session(
        new,
        [
            codex_entry("2026-10-15T15:00:00.000Z", 12.0, 40.0),
            codex_entry("2026-10-15T15:05:00.000Z", 14.5, 41.0),
            # Other limit families (no matching windows) are not Codex's 5-hour/7-day usage.
            codex_entry("2026-10-15T15:06:00.000Z", 99.0, 99.0, "premium", (60, 60)),
            {"timestamp": "2026-10-15T15:07:00.000Z", "type": "event_msg", "payload": {}},
        ],
    )
    os.utime(old, (1, 1))
    assert profile.codex_usage(root) == {
        "as_of": "2026-10-15T15:05:00+00:00",
        "five_hour": {"used_percentage": 14.5, "resets_at": "2027-01-15T08:00:00+00:00"},
        "seven_day": {"used_percentage": 41.0, "resets_at": "2027-01-21T02:53:20+00:00"},
    }


def test_codex_usage_falls_back_to_older_files_and_reports_missing(tmp_path):
    root = tmp_path / "sessions"
    assert profile.codex_usage(root) is None
    write_session(root / "a.jsonl", [codex_entry("2026-10-14T10:00:00Z", 5, 6)])
    write_session(root / "b.jsonl", [{"timestamp": "2026-10-15T10:00:00Z", "payload": {}}])
    os.utime(root / "a.jsonl", (1, 1))
    assert profile.codex_usage(root)["five_hour"]["used_percentage"] == 5


def test_claude_usage_is_unknown_until_a_valid_snapshot_exists(tmp_path):
    path = tmp_path / "claude.json"
    assert profile.claude_usage(path) is None
    path.write_text('{"five_hour": {"used_percentage": 3}}')
    assert profile.claude_usage(path) is None
    path.write_text(
        json.dumps(
            {
                "as_of": "2026-10-15T15:00:00+00:00",
                "five_hour": {"used_percentage": 3, "resets_at": 1800000000},
                "seven_day": {"used_percentage": 20.5, "resets_at": None},
            }
        )
    )
    assert profile.claude_usage(path) == {
        "as_of": "2026-10-15T15:00:00+00:00",
        "five_hour": {"used_percentage": 3, "resets_at": "2027-01-15T08:00:00+00:00"},
        "seven_day": {"used_percentage": 20.5, "resets_at": None},
    }


def run_snapshot(monkeypatch, tmp_path, text, capsys):
    monkeypatch.setattr(snapshot.Path, "home", lambda: tmp_path)
    monkeypatch.setattr(snapshot.sys, "stdin", io.StringIO(text))
    assert snapshot.main() == 0
    assert capsys.readouterr() == ("", "")
    return tmp_path / ".local/state/homelab-usage/claude.json"


def test_snapshot_keeps_only_rate_limit_windows(tmp_path, monkeypatch, capsys):
    payload = {
        "session_id": "discard",
        "workspace": {"current_dir": "/private/path"},
        "rate_limits": {
            "five_hour": {"used_percentage": 12.5, "resets_at": 1800000000, "extra": "x"},
            "seven_day": {"used_percentage": 42, "resets_at": 1800500000},
        },
    }
    destination = run_snapshot(monkeypatch, tmp_path, json.dumps(payload), capsys)
    saved = json.loads(destination.read_text())
    assert set(saved) == {"as_of", "five_hour", "seven_day"}
    assert saved["five_hour"] == {"used_percentage": 12.5, "resets_at": 1800000000}
    assert saved["seven_day"] == {"used_percentage": 42, "resets_at": 1800500000}
    assert destination.stat().st_mode & 0o777 == 0o600
    assert destination.parent.stat().st_mode & 0o777 == 0o700
    assert profile.claude_usage(destination)["seven_day"]["used_percentage"] == 42


def test_snapshot_is_silent_and_keeps_the_previous_file_on_bad_input(tmp_path, monkeypatch, capsys):
    good = {"rate_limits": {k: {"used_percentage": 1} for k in ("five_hour", "seven_day")}}
    destination = run_snapshot(monkeypatch, tmp_path, json.dumps(good), capsys)
    before = destination.read_text()
    for text in ("", "not json", "[]", '{"model": {}}', '{"rate_limits": {"five_hour": {}}}'):
        run_snapshot(monkeypatch, tmp_path, text, capsys)
        assert destination.read_text() == before


def test_cli_usage_and_schedule_reports_use_private_home(tmp_path):
    env = {**os.environ, "HOME": str(tmp_path), "PYTHONDONTWRITEBYTECODE": "1"}

    def cli(*args):
        result = subprocess.run(
            [sys.executable, str(profile.SCRIPT), *args], capture_output=True, text=True, env=env
        )
        assert result.returncode == 0, result.stderr
        return json.loads(result.stdout)

    usage = cli("usage")
    assert usage["codex"] is None and usage["claude"] is None
    assert cli("schedule", "show") == {**profile.SCHEDULE_DEFAULT, "last": None}
    # `schedule set` inspects process ancestry; its CLI path is covered in test_mac_profile.
    assert cli("schedule", "off") == {**profile.SCHEDULE_DEFAULT, "last": None}
    saved = tmp_path / ".config/homelab/night-window.json"
    assert saved.stat().st_mode & 0o777 == 0o600
    saved.write_text('{"enabled": true}')
    bad = subprocess.run(
        [sys.executable, str(profile.SCRIPT), "schedule", "show"], capture_output=True, env=env
    )
    assert bad.returncode == 1
