import importlib.machinery
import importlib.util
import json
import os
import subprocess
import sys
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[1]


def module(name, filename):
    loader = importlib.machinery.SourceFileLoader(name, str(ROOT / "mac" / filename))
    spec = importlib.util.spec_from_loader(name, loader)
    result = importlib.util.module_from_spec(spec)
    loader.exec_module(result)
    return result


profile = module("usage_profile", "homelab-profile")


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

    def node(command, what, **kwargs):
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
    assert "CONTAINER_NAME=homelab-litellm-1" in audit
    assert "-u homelab.service" not in audit and "--since '24 hours ago'" in audit
    assert all(word not in " ".join(commands) for word in ("rm ", "tee", ">", "restart", "POST"))


def test_waiting_reads_the_node_only_through_the_agent_account(monkeypatch):
    calls = []
    monkeypatch.setattr(
        profile.subprocess,
        "run",
        lambda args, **kwargs: calls.append(args) or subprocess.CompletedProcess(args, 0, "", ""),
    )
    profile.node("true", "test read")
    assert calls[0][0] == "/usr/bin/ssh" and calls[0][-2:] == ["homelab-agent", "true"]
    assert "BatchMode=yes" in calls[0]


def test_waiting_with_nothing_blocked():
    assert profile.waiting_purposes("", {}) == []


def test_waiting_empty_journal_exit_one_means_no_refusals(monkeypatch):
    calls = []

    def run(args, **kwargs):
        calls.append(args[-1])
        if args[-1] == profile.MODEL_INFO_COMMAND:
            return subprocess.CompletedProcess(args, 0, json.dumps(MODEL_INFO), "")
        return subprocess.CompletedProcess(args, 1, "", "")

    monkeypatch.setattr(profile.subprocess, "run", run)
    assert profile.waiting_report() == {"since_hours": 24, "purposes": []}
    assert calls == [profile.MODEL_INFO_COMMAND, profile.AUDIT_COMMAND]


@pytest.mark.parametrize(
    "command,stage",
    [
        (profile.AUDIT_COMMAND, "reading gateway audit lines"),
        (profile.MODEL_INFO_COMMAND, "reading /v1/model/info"),
    ],
)
@pytest.mark.parametrize(
    "exit_code,stdout,stderr", [(1, "", "denied"), (1, "unexpected", ""), (255, "", "ssh error")]
)
def test_waiting_node_failures_name_the_call_without_output(
    monkeypatch, command, stage, exit_code, stdout, stderr
):
    def run(args, **kwargs):
        if args[-1] == command:
            return subprocess.CompletedProcess(args, exit_code, stdout, stderr)
        return subprocess.CompletedProcess(args, 0, json.dumps(MODEL_INFO), "")

    monkeypatch.setattr(profile.subprocess, "run", run)
    with pytest.raises(profile.ProfileRefusal, match=stage) as failure:
        profile.waiting_report()
    assert "denied" not in str(failure.value)
    assert "unexpected" not in str(failure.value)
    assert "ssh error" not in str(failure.value)


def test_waiting_bad_model_metadata_names_the_call(monkeypatch):
    monkeypatch.setattr(profile, "node", lambda *args, **kwargs: "not json")
    with pytest.raises(profile.ProfileRefusal, match="/v1/model/info returned invalid JSON"):
        profile.waiting_report()


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


@pytest.mark.parametrize("window", ["primary", "secondary"])
def test_codex_usage_skips_newest_null_window(tmp_path, window):
    complete = codex_entry("2026-10-15T15:00:00Z", 5, 6)
    newest = codex_entry("2026-10-15T16:00:00Z", 100, 100)
    newest["payload"]["rate_limits"][window] = None
    write_session(tmp_path / "session.jsonl", [complete, newest])
    result = profile.codex_usage(tmp_path)
    assert result["as_of"] == "2026-10-15T15:00:00+00:00"
    assert result["five_hour"]["used_percentage"] == 5
    assert result["seven_day"]["used_percentage"] == 6


@pytest.mark.parametrize("window", ["primary", "secondary"])
def test_codex_usage_only_null_windows_are_unknown(tmp_path, window):
    entry = codex_entry("2026-10-15T16:00:00Z", 100, 100)
    entry["payload"]["rate_limits"][window] = None
    write_session(tmp_path / "session.jsonl", [entry])
    assert profile.codex_usage(tmp_path) is None


def test_codex_usage_null_entry_falls_back_to_complete_older_file(tmp_path):
    old = tmp_path / "old.jsonl"
    new = tmp_path / "new.jsonl"
    write_session(old, [codex_entry("2026-10-15T15:00:00Z", 5, 6)])
    entry = codex_entry("2026-10-15T16:00:00Z", 100, 100)
    entry["payload"]["rate_limits"] = None
    write_session(new, [entry])
    os.utime(old, (1, 1))
    assert profile.codex_usage(tmp_path)["five_hour"]["used_percentage"] == 5


CLAUDE_NOW = {
    "at": 1792076400,  # 2026-10-15T15:00:00Z
    "rate_limits": {
        "five_hour": {"used_percentage": 3, "resets_at": 1800000000},
        "seven_day": {"used_percentage": 20.5, "resets_at": 1800500000},
    },
}


def test_claude_usage_reads_the_status_line_file_and_its_time(tmp_path):
    path = tmp_path / "usage-now.json"
    path.write_text(json.dumps(CLAUDE_NOW))
    before = path.read_bytes()
    assert profile.claude_usage(path) == {
        "as_of": "2026-10-15T15:00:00+00:00",
        "five_hour": {"used_percentage": 3, "resets_at": "2027-01-15T08:00:00+00:00"},
        "seven_day": {"used_percentage": 20.5, "resets_at": "2027-01-21T02:53:20+00:00"},
    }
    assert path.read_bytes() == before


def test_claude_usage_is_unknown_when_missing_or_invalid(tmp_path):
    path = tmp_path / "usage-now.json"
    assert profile.claude_usage(path) is None
    limits = CLAUDE_NOW["rate_limits"]
    for invalid in (
        "not json",
        "[]",
        json.dumps({"rate_limits": limits}),
        json.dumps({**CLAUDE_NOW, "at": "yesterday"}),
        json.dumps({**CLAUDE_NOW, "at": True}),
        json.dumps({"at": 1, "rate_limits": {"five_hour": limits["five_hour"]}}),
        json.dumps({"at": 1, "rate_limits": {**limits, "seven_day": {"used_percentage": "x"}}}),
        json.dumps({"at": 1, "rate_limits": None}),
    ):
        path.write_text(invalid)
        assert profile.claude_usage(path) is None, invalid


def test_usage_report_gives_each_source_its_age(tmp_path, monkeypatch):
    path = tmp_path / "usage-now.json"
    path.write_text(json.dumps(CLAUDE_NOW))
    monkeypatch.setattr(profile, "CLAUDE_USAGE", path)
    monkeypatch.setattr(profile, "CODEX_SESSIONS", tmp_path / "sessions")
    write_session(tmp_path / "sessions/a.jsonl", [codex_entry("2026-10-15T14:55:00Z", 5, 6)])
    report = profile.usage_report(now=CLAUDE_NOW["at"] + 90)
    assert report["read_at"] == "2026-10-15T15:01:30+00:00"
    assert report["claude"]["age_seconds"] == 90
    assert report["codex"]["age_seconds"] == 390
    path.unlink()
    assert profile.usage_report(now=CLAUDE_NOW["at"])["claude"] is None


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
