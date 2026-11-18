"""Durable messages, usage waits and interruption recovery use Procrastinate transactions."""

import json
import threading
import time
from contextlib import contextmanager
from datetime import UTC, datetime, timedelta
from decimal import Decimal

import pytest

from homelab.api import runs as api_runs
from homelab.tasks import slices
from tests.test_runs import CLIENT, HEADERS, api, body, post  # noqa: F401
from tests.test_task_store import connection  # noqa: F401


@pytest.fixture
def enabled(api, monkeypatch):  # noqa: F811
    monkeypatch.setenv("HOMELAB_RUN_ROLES", "slice,cut,review")
    return api


def submitted(database, **changes):
    response = post(body(**{"role": "slice", **changes}))
    assert response.status_code == 201, response.text
    return database.execute(
        "SELECT id FROM homelab_runs WHERE request_id = %s",
        (changes.get("request_id", "ticket-1"),),
    ).fetchone()[0]


def test_whole_runs_have_separate_durable_queue(enabled):
    submitted(enabled)
    assert enabled.execute("SELECT queue_name, args FROM procrastinate_jobs").fetchall() == [
        ("runs", {"task_id": 1})
    ]
    assert slices.claim(enabled)["request"]["role"] == "slice"
    assert slices.claim(enabled) is None


@pytest.mark.parametrize("status", ["question", "waiting", "done", "escalated"])
def test_messages_deduplicate_and_resume_same_copy_and_executor(enabled, status):
    run_id = submitted(enabled)
    claimed = slices.claim(enabled)
    slices.begin(enabled, claimed, claimed["executor"])
    slices.prepared(enabled, run_id, "run-" + run_id.hex)
    result = {
        "status": status,
        "status_reason": "client_step" if status == "waiting" else None,
        "resumable": True,
        "final_message": "Question",
        "resume_at": None,
    }
    slices.complete(enabled, claimed, result, "same-session", [], {})
    endpoint = f"/v1/runs/{run_id}/messages"
    message = {"contract": 1, "message_id": "one", "text": "Private answer"}
    assert CLIENT.post(endpoint, headers=HEADERS, json=message).status_code == 200
    assert CLIENT.post(endpoint, headers=HEADERS, json=message).status_code == 200
    resumed = slices.claim(enabled)
    assert resumed["session_id"] == "same-session"
    assert resumed["work_id"] == "run-" + run_id.hex and resumed["copy_ready"]
    assert resumed["executor"] == claimed["executor"]
    assert slices.prompt(enabled, resumed) == "Private answer"
    slices.begin(enabled, resumed, resumed["executor"])
    slices.complete(enabled, resumed, {**result, "status": "done"}, "same-session", [], {})
    assert slices.claim(enabled) is None
    assert enabled.execute("SELECT count(*) FROM homelab_task_attempts").fetchone()[0] == 2
    assert enabled.execute("SELECT count(*) FROM homelab_run_messages").fetchone()[0] == 1
    assert "Private answer" not in str(
        enabled.execute("SELECT args FROM procrastinate_jobs").fetchall()
    )


def test_usage_wait_requeues_itself_at_reset_without_consuming_an_attempt(enabled):
    run_id = submitted(enabled)
    claimed = slices.claim(enabled)
    reset = datetime.now(UTC) + timedelta(hours=1)
    slices.defer_usage(enabled, claimed, reset)
    saved = api_runs.read("factory", run_id)
    assert saved["status"] == "waiting" and saved["status_reason"] == "usage_limit"
    assert saved["resume_at"] == reset and saved["attempts"] == []
    assert slices.claim(enabled) is None
    enabled.execute("UPDATE procrastinate_jobs SET scheduled_at = now() WHERE status = 'todo'")
    assert slices.claim(enabled)["id"] == run_id


def test_limit_during_turn_keeps_session_and_same_executor(enabled):
    submitted(enabled)
    claimed = slices.claim(enabled)
    slices.begin(enabled, claimed, claimed["executor"])
    reset = datetime.now(UTC) + timedelta(hours=1)
    slices.complete(
        enabled,
        claimed,
        {
            "status": "waiting",
            "status_reason": "usage_limit",
            "resumable": True,
            "resume_at": reset,
        },
        "limited-session",
        [],
        {},
    )
    enabled.execute("UPDATE procrastinate_jobs SET scheduled_at = now() WHERE status = 'todo'")
    resumed = slices.claim(enabled)
    assert resumed["session_id"] == "limited-session"
    assert resumed["executor"] == claimed["executor"]
    assert resumed["harness_started"]


def test_recover_aborts_job_for_already_failed_run(enabled):
    run_id = submitted(enabled)
    claimed = slices.claim(enabled)
    slices.begin(enabled, claimed, claimed["executor"])
    enabled.execute(
        """UPDATE homelab_runs SET status = 'failed', status_reason = 'sandbox_error',
        resumable = false WHERE id = %s""",
        (run_id,),
    )
    before = enabled.execute("SELECT * FROM homelab_runs WHERE id = %s", (run_id,)).fetchone()
    assert slices.recover(enabled) == 0
    assert (
        enabled.execute("SELECT * FROM homelab_runs WHERE id = %s", (run_id,)).fetchone() == before
    )
    assert enabled.execute("SELECT status FROM procrastinate_jobs").fetchall() == [("aborted",)]
    assert slices.claim(enabled) is None


def test_two_interruptions_fail_the_run_and_both_attempts(enabled):
    run_id = submitted(enabled)
    for index in (0, 1):
        claimed = slices.claim(enabled)
        slices.begin(enabled, claimed, claimed["executor"])
        assert slices.recover(enabled) == 1
        saved = api_runs.read("factory", run_id)
        assert saved["status"] == ("queued" if index == 0 else "failed")
        assert saved["status_reason"] == "interrupted"
    assert slices.claim(enabled) is None
    assert enabled.execute(
        "SELECT status, reason FROM homelab_task_attempts ORDER BY number"
    ).fetchall() == [("failed", "interrupted"), ("failed", "interrupted")]


def test_repository_lock_prevents_overlapping_roles(enabled):
    submitted(enabled)
    submitted(enabled, request_id="another", branch="slice/other", role="review", mode="read")
    first = slices.claim(enabled)
    assert first is not None
    assert slices.claim(enabled) is None
    slices.defer_usage(enabled, first, datetime.now(UTC) + timedelta(hours=1))
    assert slices.claim(enabled)["request_id"] == "another"


def test_review_requires_read_mode(enabled):
    response = post(body(role="review"))
    assert response.status_code == 422
    assert enabled.execute("SELECT count(*) FROM homelab_runs").fetchone()[0] == 0


def test_failed_harness_run_cancels_all_descendants(enabled):
    submitted(enabled)
    submitted(enabled, request_id="child", after=["ticket-1"])
    claimed = slices.claim(enabled)
    slices.begin(enabled, claimed, claimed["executor"])
    slices.complete(
        enabled,
        claimed,
        {"status": "failed", "status_reason": "outside_allowed_files", "resumable": False},
        None,
        [],
        {},
    )
    rows = enabled.execute(
        "SELECT status, status_reason FROM homelab_runs ORDER BY created_at"
    ).fetchall()
    assert rows == [("failed", "outside_allowed_files"), ("cancelled", "predecessor")]
    assert slices.claim(enabled) is None


def test_service_fallback_and_message_resume_with_fake_launcher_and_github(
    enabled, monkeypatch, tmp_path, caplog
):
    from homelab.runs import slices as execute
    from homelab.runs.slice_loop import Service

    run_id = submitted(enabled)
    calls = []
    turns = []
    prompts = []
    operations = []
    monkeypatch.setattr(execute, "installation_token", lambda _: "FAKE-PRIVATE-TOKEN")
    monkeypatch.setattr(execute, "remote_refs", lambda *a: ("trunk", {"trunk": "a" * 40}))
    monkeypatch.setattr(execute.launcher, "request", lambda r: operations.append(r))
    monkeypatch.setattr(execute, "deliver", lambda *a, **kw: ["b" * 40])

    def capture(request, out, err, seconds, **kwargs):
        calls.append(request)
        err.write_bytes(b"")
        output = ""
        if request["profile"] == "usage":
            output = json.dumps(
                {
                    "primary": {
                        "used_percent": 99,
                        "window_minutes": 300,
                        "resets_at": time.time() + 1000,
                    }
                }
            )
        elif request["profile"] == "cli-run":
            if "Reply with: ok" in request["command"]:
                output = json.dumps(
                    {
                        "type": "rate_limit_event",
                        "rate_limit_info": {
                            "unifiedWindows": {
                                "five_hour": {"utilization": 0.1, "resetsAt": time.time() + 1000}
                            }
                        },
                    }
                )
            else:
                turns.append(request)
                prompts.append(json.load(kwargs["stdin"]))
                assert request["tool"] == "claude"
                output = json.dumps(
                    {
                        "type": "result",
                        "session_id": "same-session",
                        "structured_output": {
                            "kind": "question" if len(turns) == 1 else "handover",
                            "body": "Private final message",
                        },
                        "usage": {"input_tokens": 10, "output_tokens": 5},
                        "total_cost_usd": 0.01,
                    }
                )
        out.write_text(output)
        return 0, "exited"

    monkeypatch.setattr(execute.launcher, "capture", capture)

    @contextmanager
    def database():
        yield enabled

    service = Service(database, tmp_path, lambda: None)
    try:
        service.run(slices.claim(enabled))
        first = api_runs.read("factory", run_id)
        assert first["status"] == "question" and first["resumable"]
        assert first["executor"]["harness"] == "claude"
        assert first["commits"] == ["b" * 40]
        assert len(first["attempts"]) == 1
        endpoint = f"/v1/runs/{run_id}/messages"
        message = {"contract": 1, "message_id": "answer", "text": "Private answer"}
        assert CLIENT.post(endpoint, headers=HEADERS, json=message).status_code == 200
        assert CLIENT.post(endpoint, headers=HEADERS, json=message).status_code == 200
        service.run(slices.claim(enabled))
        second = api_runs.read("factory", run_id)
        assert second["status"] == "done" and len(second["attempts"]) == 2
        assert second["metrics"]["tokens"]["fresh"] == 20
        assert second["metrics"]["usd"] == Decimal("0.02")
        assert "--resume" in turns[1]["command"] and "same-session" in turns[1]["command"]
        assert prompts[1] == {"goal": "Private answer", "assets": []}
        assert "Private answer" not in json.dumps(turns[1])
        assert prompts[0]["work"]["allowed_files"] == ["docs/example.md"]
        assert turns[0]["folders"] == turns[1]["folders"]
        assert len([r for r in calls if r.get("shape") == "clone"]) == 1
        assert "FAKE-PRIVATE-TOKEN" not in repr(calls)
        assert not list(tmp_path.glob("turn-*"))
        assert "Private" not in caplog.text
    finally:
        service.close()


def test_dispatcher_never_starts_more_than_two_cli_runs(monkeypatch, tmp_path):
    from homelab.runs.slice_loop import Service

    release = threading.Event()
    started = []

    @contextmanager
    def database():
        yield object()

    monkeypatch.setattr(slices, "claim", lambda _: {"id": len(started)})
    monkeypatch.setattr(slices, "sweep", lambda _, **kw: 0)
    service = Service(database, tmp_path, lambda: None)

    def run(saved):
        started.append(saved)
        assert release.wait(10)

    monkeypatch.setattr(service, "run", run)
    monkeypatch.setattr(service, "legacy_inactive", lambda: None)
    service.start()
    try:
        deadline = time.monotonic() + 5
        while len(started) < 2 and time.monotonic() < deadline:
            time.sleep(0.01)
        assert len(started) == 2
        time.sleep(1.1)
        assert len(started) == 2
    finally:
        service.stop.set()
        release.set()
        service.close()


@pytest.mark.parametrize("role", ["slice", "cut", "review"])
def test_enabled_read_roles_and_default_branch_are_accepted(enabled, role):
    run_id = submitted(enabled, role=role, mode="read", branch=None)
    record = api_runs.read("factory", run_id)
    assert record["mode"] == "read" and record["status"] == "queued"


def test_setting_only_enables_named_roles_and_preserves_other_rejections(enabled, monkeypatch):
    monkeypatch.setenv("HOMELAB_RUN_ROLES", "slice")
    assert post(body(role="cut", mode="read")).status_code == 409
    assert post(body(role="review", mode="read")).status_code == 409
    assert post(body(mode="read")).status_code == 409
    assert post(body(role="slice", mode="read", limits={})).status_code == 409
    assert (
        post(
            body(
                role="slice",
                mode="read",
                eval={"set": "T", "case": "C", "executor": {"configuration": "pi/base"}},
            )
        ).status_code
        == 409
    )
    assert enabled.execute("SELECT count(*) FROM homelab_runs").fetchone()[0] == 0


def test_goal_and_asset_bounds_remain_enforced_when_read_is_enabled(enabled):
    assert post(body(role="slice", mode="read", work={"goal": "x" * 200_001})).status_code == 422
    assert enabled.execute("SELECT count(*) FROM homelab_runs").fetchone()[0] == 0


def test_message_during_usage_wait_keeps_reset_and_one_scheduled_turn(enabled):
    run_id = submitted(enabled)
    claimed = slices.claim(enabled)
    slices.begin(enabled, claimed, claimed["executor"])
    reset = datetime.now(UTC) + timedelta(hours=1)
    slices.complete(
        enabled,
        claimed,
        {
            "status": "waiting",
            "status_reason": "usage_limit",
            "resumable": True,
            "resume_at": reset,
        },
        "same-session",
        [],
        {},
    )
    endpoint = f"/v1/runs/{run_id}/messages"
    message = {"contract": 1, "message_id": "one", "text": "Answer after reset"}
    assert CLIENT.post(endpoint, headers=HEADERS, json=message).status_code == 200
    assert CLIENT.post(endpoint, headers=HEADERS, json=message).status_code == 200
    saved = api_runs.read("factory", run_id)
    assert saved["status"] == "waiting" and saved["resume_at"] == reset
    assert slices.claim(enabled) is None
    assert (
        enabled.execute("SELECT count(*) FROM procrastinate_jobs WHERE status = 'todo'").fetchone()[
            0
        ]
        == 1
    )
    enabled.execute("UPDATE procrastinate_jobs SET scheduled_at = now() WHERE status = 'todo'")
    resumed = slices.claim(enabled)
    assert slices.prompt(enabled, resumed) == "Answer after reset"


def test_unavailable_usage_readings_wait_without_spending_a_turn(enabled, monkeypatch, tmp_path):
    from homelab.runs import slices as execute
    from homelab.runs.slice_loop import Service

    run_id = submitted(enabled)

    @contextmanager
    def database():
        yield enabled

    def unavailable(*args, **kwargs):
        raise execute.launcher.Refused("launcher_unavailable")

    service = Service(database, tmp_path, lambda: None)
    monkeypatch.setattr(execute, "codex_usage", unavailable)
    monkeypatch.setattr(service.cache, "read", unavailable)
    try:
        service.run(slices.claim(enabled))
        saved = api_runs.read("factory", run_id)
        assert saved["status"] == "waiting" and saved["status_reason"] == "usage_limit"
        assert saved["attempts"] == [] and not saved["resumable"]
    finally:
        service.close()


def test_write_cut_is_accepted(enabled):
    submitted(enabled, role="cut")
    assert slices.claim(enabled)["request"]["mode"] == "write"


def test_removed_copy_can_resume_with_same_session(enabled, monkeypatch):
    from homelab.runs import launcher

    run_id = submitted(enabled)
    saved = slices.claim(enabled)
    slices.begin(enabled, saved, saved["executor"])
    slices.prepared(enabled, run_id, "run-" + run_id.hex)
    slices.complete(enabled, saved, {"status": "done", "resumable": True}, "session", [], {})
    monkeypatch.setattr(launcher, "request", lambda request: None)
    assert slices.sweep(enabled, now=datetime.now(UTC) + timedelta(days=4)) == 1
    assert not api_runs.read("factory", run_id)["resumable"]
    message = {"contract": 1, "message_id": "fresh", "text": "Continue"}
    assert (
        CLIENT.post(f"/v1/runs/{run_id}/messages", headers=HEADERS, json=message).status_code == 200
    )
    resumed = slices.claim(enabled)
    assert not resumed["copy_ready"] and resumed["session_id"] == "session"


@pytest.mark.parametrize("state", ["done", "question", "waiting", "escalated"])
def test_cancel_after_copy_removed_disables_fresh_resume(enabled, monkeypatch, state):
    from homelab.runs import launcher

    run_id = submitted(enabled)
    saved = slices.claim(enabled)
    slices.begin(enabled, saved, saved["executor"])
    slices.prepared(enabled, run_id, "run-" + run_id.hex)
    slices.complete(enabled, saved, {"status": state, "resumable": True}, "session", [], {})
    monkeypatch.setattr(launcher, "request", lambda request: None)
    assert slices.sweep(enabled, now=datetime.now(UTC) + timedelta(days=4)) == 1
    response = CLIENT.post(f"/v1/runs/{run_id}/cancel", headers=HEADERS)
    assert response.status_code == 200 and response.json()["status"] == "cancelled"
    message = {"contract": 1, "message_id": "after-cancel", "text": "Continue"}
    assert (
        CLIENT.post(f"/v1/runs/{run_id}/messages", headers=HEADERS, json=message).status_code == 409
    )


def test_sweep_keeps_busy_record_and_uses_last_turn_end(enabled, monkeypatch):
    from homelab.runs import launcher

    run_id = submitted(enabled)
    saved = slices.claim(enabled)
    slices.begin(enabled, saved, saved["executor"])
    slices.prepared(enabled, run_id, "run-" + run_id.hex)
    slices.complete(enabled, saved, {"status": "question", "resumable": True}, "session", [], {})
    monkeypatch.setattr(launcher, "request", lambda request: None)
    # A newer administrative finished timestamp does not extend the last turn's life.
    enabled.execute("UPDATE homelab_task_attempts SET ended_at = now() - interval '4 days'")
    assert slices.sweep(enabled) == 1
    message = {"contract": 1, "message_id": "answer", "text": "Continue"}
    assert (
        CLIENT.post(f"/v1/runs/{run_id}/messages", headers=HEADERS, json=message).status_code == 200
    )
    assert slices.sweep(enabled, now=datetime.now(UTC) + timedelta(days=4)) == 0


def test_service_reprepares_removed_copy_and_resumes_session(enabled, monkeypatch, tmp_path):
    from homelab.runs import slices as execute
    from homelab.runs.slice_loop import Service
    from homelab.runs.usage import Reading

    run_id = submitted(enabled)
    saved = slices.claim(enabled)
    slices.begin(enabled, saved, saved["executor"])
    work_id = "run-" + run_id.hex
    slices.prepared(enabled, run_id, work_id)
    slices.complete(enabled, saved, {"status": "done", "resumable": True}, "session", [], {})
    monkeypatch.setattr(execute.launcher, "request", lambda request: None)
    assert slices.sweep(enabled, now=datetime.now(UTC) + timedelta(days=4)) == 1
    message = {"contract": 1, "message_id": "answer", "text": "Continue"}
    assert (
        CLIENT.post(f"/v1/runs/{run_id}/messages", headers=HEADERS, json=message).status_code == 200
    )
    prepares, turns = [], []

    def prepare(record, folder, **kwargs):
        prepares.append(record["id"])
        return work_id

    def turn(record, *args, **kwargs):
        turns.append((record["session_id"], record["executor"]))
        return {"status": "done", "resumable": True}, "session", [], {}

    monkeypatch.setattr(execute, "prepare", prepare)
    monkeypatch.setattr(execute, "execute_turn", turn)
    monkeypatch.setattr(
        execute, "codex_usage", lambda *a, **k: Reading(0, time.time() + 3600, time.time())
    )

    @contextmanager
    def database():
        yield enabled

    service = Service(database, tmp_path, lambda: None)
    try:
        service.run(slices.claim(enabled))
        assert prepares == [run_id]
        assert turns == [("session", saved["executor"])]
    finally:
        service.close()


def test_removed_escalated_copy_keeps_branch_exclusion_and_dependency_resume(enabled, monkeypatch):
    from homelab.runs import launcher

    run_id = submitted(enabled)
    saved = slices.claim(enabled)
    slices.begin(enabled, saved, saved["executor"])
    slices.prepared(enabled, run_id, "run-" + run_id.hex)
    slices.complete(enabled, saved, {"status": "escalated", "resumable": True}, "session", [], {})
    monkeypatch.setattr(launcher, "request", lambda request: None)
    assert slices.sweep(enabled, now=datetime.now(UTC) + timedelta(days=4)) == 1
    assert post(body(role="slice", request_id="other")).status_code == 409
    submitted(enabled, request_id="dependent", after=["ticket-1"])
    message = {"contract": 1, "message_id": "answer", "text": "Continue"}
    assert (
        CLIENT.post(f"/v1/runs/{run_id}/messages", headers=HEADERS, json=message).status_code == 200
    )
    assert slices.claim(enabled)["id"] == run_id


def test_removal_does_not_admit_two_pending_messages_at_usage_reset(enabled, monkeypatch):
    from homelab.runs import launcher

    run_id = submitted(enabled)
    saved = slices.claim(enabled)
    slices.begin(enabled, saved, saved["executor"])
    slices.prepared(enabled, run_id, "run-" + run_id.hex)
    slices.complete(
        enabled,
        saved,
        {
            "status": "waiting",
            "status_reason": "usage_limit",
            "resumable": True,
            "resume_at": datetime.now(UTC) + timedelta(days=5),
        },
        "session",
        [],
        {},
    )
    monkeypatch.setattr(launcher, "request", lambda request: None)
    assert slices.sweep(enabled, now=datetime.now(UTC) + timedelta(days=4)) == 1
    endpoint = f"/v1/runs/{run_id}/messages"
    message = {"contract": 1, "message_id": "one", "text": "Continue"}
    assert CLIENT.post(endpoint, headers=HEADERS, json=message).status_code == 200
    assert CLIENT.post(endpoint, headers=HEADERS, json=message).status_code == 200
    assert (
        CLIENT.post(endpoint, headers=HEADERS, json={**message, "message_id": "two"}).status_code
        == 409
    )
    assert slices.claim(enabled) is None


def test_failed_prepare_removes_partial_copy_immediately(enabled, monkeypatch, tmp_path):
    from homelab.runs import slices as execute
    from homelab.runs.slice_loop import Service
    from homelab.runs.usage import Reading

    run_id = submitted(enabled)
    operations = []
    monkeypatch.setattr(execute.launcher, "request", operations.append)
    monkeypatch.setattr(
        execute, "codex_usage", lambda *a, **k: Reading(0, time.time() + 3600, time.time())
    )

    def prepare(*args, **kwargs):
        raise execute.Refused("sandbox_error")

    monkeypatch.setattr(execute, "prepare", prepare)

    @contextmanager
    def database():
        yield enabled

    service = Service(database, tmp_path, lambda: None)
    try:
        service.run(slices.claim(enabled))
        assert api_runs.read("factory", run_id)["status"] == "failed"
        assert operations == [{"op": "cli-remove", "work_id": "run-" + run_id.hex}] * 2
        record = enabled.execute("SELECT work_id, copy_ready FROM homelab_runs").fetchone()
        assert record == (None, False)
    finally:
        service.close()


def test_push_refused_completes_failed_run_with_contract_reason(enabled):
    run_id = submitted(enabled)
    saved = slices.claim(enabled)
    slices.begin(enabled, saved, saved["executor"])
    slices.complete(
        enabled,
        saved,
        {"status": "failed", "status_reason": "push_refused", "resumable": False},
        "session",
        [],
        {},
    )
    result = api_runs.read("factory", run_id)
    assert result["status"] == "failed" and result["status_reason"] == "push_refused"
    assert result["commits"] == [] and not result["resumable"]
    assert result["attempts"][0]["status"] == "failed"
    assert result["attempts"][0]["outcome"] == "push_refused"
    assert enabled.execute("SELECT status FROM procrastinate_jobs").fetchone()[0] == "failed"
    submitted(enabled, request_id="next", branch="slice/next")
    assert slices.claim(enabled)["request_id"] == "next"
