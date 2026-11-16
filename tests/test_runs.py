"""Version-one runs: durable idempotency, ownership and today's task bridge."""

import hashlib
import json
from concurrent.futures import ThreadPoolExecutor
from contextlib import nullcontext
from types import SimpleNamespace

import pytest
from fastapi.testclient import TestClient

from homelab.api import eval_tasks, run_keys, runs
from homelab.api.app import app
from homelab.tasks import claim, finish
from homelab.tasks.executors import load_executors
from homelab.tasks.store import apply_schema
from tests.test_task_store import connection  # noqa: F401

CLIENT = TestClient(app)
MARKER = "planted-private-goal-85fd2"
HEADERS = {"Authorization": "Bearer factory-fixture"}
OTHER = {"Authorization": "Bearer other-fixture"}


def body(**changes):
    return {
        "contract": 1,
        "request_id": "ticket-1",
        "attribution": {"case": "one"},
        "repository": "example/repo",
        "base_commit": "a" * 40,
        "branch": "work/run-api",
        "mode": "write",
        "role": "ticket",
        "level": "standard",
        "work": {
            "goal": MARKER,
            "allowed_files": ["docs/example.md"],
            "pinned_paths": ["tests/check.sh"],
            "check_command": "true",
            "history": "shallow",
        },
        **changes,
    }


@pytest.fixture
def api(connection, monkeypatch, tmp_path):  # noqa: F811
    monkeypatch.setattr(runs, "connect", lambda: nullcontext(connection))
    monkeypatch.setattr(
        eval_tasks, "psycopg", SimpleNamespace(connect=lambda *a, **kw: nullcontext(connection))
    )
    keys = [
        {
            "application": application,
            "sha256": hashlib.sha256(key.encode()).hexdigest(),
            "scopes": scopes,
            "repositories": {"example/repo": "trunk"},
        }
        for application, key, scopes in [
            ("factory", "factory-fixture", ["runs:write", "runs:read", "runs:eval"]),
            ("other", "other-fixture", ["runs:write", "runs:read"]),
            ("reader", "reader-fixture", ["runs:read"]),
        ]
    ]
    path = tmp_path / "keys.json"
    path.write_text(json.dumps(keys))
    monkeypatch.setattr(run_keys, "load_keys", lambda: run_keys.parse_keys(path.read_bytes()))
    monkeypatch.setattr(runs, "executors", load_executors)
    return connection


def post(document=None, headers=HEADERS):
    return CLIENT.post("/v1/runs", headers=headers, json=document or body())


def test_create_retry_conflict_and_content_isolation(api, caplog):
    first = post()
    assert first.status_code == 201
    run_id = first.json()["run_id"]
    again = post()
    assert again.status_code == 200
    assert again.json()["run_id"] == run_id
    conflict = post(body(priority=3))
    assert conflict.status_code == 409
    assert conflict.json()["error"] == "request_conflict"
    assert api.execute("SELECT count(*) FROM homelab_runs").fetchone() == (1,)
    assert api.execute("SELECT count(*) FROM procrastinate_jobs").fetchone() == (1,)
    goal, rest = api.execute(
        "SELECT goal, (to_jsonb(r) - 'goal')::text FROM homelab_runs r"
    ).fetchone()
    assert goal == MARKER and MARKER not in rest
    for table in (
        "homelab_tasks",
        "procrastinate_jobs",
        "homelab_task_attempts",
        "homelab_task_traces",
    ):
        rows = api.execute(f"SELECT to_jsonb(t)::text FROM {table} t").fetchall()
        assert all(MARKER not in row[0] for row in rows)
    assert MARKER not in caplog.text
    record = CLIENT.get(f"/v1/runs/{run_id}", headers=HEADERS).json()
    assert record["executor"] == {"configuration": "pi/base"}
    assert len(record["configuration_hash"]) == 64
    assert record["attribution"] == {"case": "one"}
    assert MARKER not in json.dumps(record)
    apply_schema(api)
    assert api.execute("SELECT count(*) FROM homelab_runs").fetchone() == (1,)


@pytest.mark.parametrize(
    "changes, status, error",
    [
        ({"unknown": MARKER}, 422, "invalid_request"),
        ({"branch": "trunk"}, 409, "branch_is_default"),
        ({"repository": "unknown/repo"}, 403, "repository_not_allowed"),
        (
            {
                "assets": [
                    {
                        "name": "guide",
                        "type": "text/plain",
                        "version": "1",
                        "text": MARKER,
                        "sha256": "0" * 64,
                    }
                ]
            },
            400,
            "asset_hash_mismatch",
        ),
        ({"role": "slice"}, 409, "role_not_available"),
        ({"role": "cut"}, 409, "role_not_available"),
        ({"role": "review"}, 409, "role_not_available"),
    ],
)
def test_rejections_leave_no_record(api, changes, status, error, caplog):
    response = post(body(**changes))
    assert response.status_code == status
    assert response.json()["error"] == error
    assert api.execute("SELECT count(*) FROM homelab_runs").fetchone() == (0,)
    assert api.execute("SELECT count(*) FROM homelab_tasks").fetchone() == (0,)
    assert MARKER not in response.text + caplog.text


def test_scopes_and_application_isolation(api):
    run_id = post().json()["run_id"]
    assert CLIENT.get(f"/v1/runs/{run_id}", headers=OTHER).status_code == 404
    for suffix in ("messages", "cancel"):
        response = CLIENT.post(
            f"/v1/runs/{run_id}/{suffix}",
            headers=OTHER,
            json={"contract": 1, "message_id": "one", "text": "reply"},
        )
        assert response.status_code == 404
    assert post(headers={"Authorization": "Bearer reader-fixture"}).status_code == 403
    assert post(headers={}).status_code == 401
    assert CLIENT.get("/v1/runs", headers=OTHER).json()["runs"] == []
    assert CLIENT.get("/v1/runs?request_id=ticket-1", headers=OTHER).json()["runs"] == []


def test_eval_scope_diff_and_configuration(api, monkeypatch, tmp_path):
    document = body(eval={"set": "T", "case": "one", "configuration": "base"})
    assert post(document, OTHER).status_code == 403
    response = post(document)
    assert response.status_code == 201
    task = claim.claim(api)
    assert task["eval_set"] == "T" and task["eval_configuration"] == "base"
    assert task["goal"] == MARKER
    route = {"kind": "litellm", "purpose": "task-worker"}
    finish.record(
        api, finish.BeginAttempt(event="begin_attempt", task_id=task["id"], number=1, route=route)
    )
    finish.record(
        api,
        finish.EndAttempt(
            event="end_attempt",
            task_id=task["id"],
            number=1,
            route=route,
            status="done",
            reason="eval_kept",
            run_id="eval-one",
            configuration_hash="a" * 64,
            steps=2,
            cost_usd=0.25,
        ),
    )
    finish.record(
        api,
        finish.EndTask(
            event="end_task",
            task_id=task["id"],
            job_id=task["job_id"],
            status="done",
            reason="eval_kept",
        ),
    )
    (tmp_path / "eval-one.patch").write_bytes(b"a diff")
    monkeypatch.setattr(runs, "get_settings", lambda: SimpleNamespace(eval_diffs_dir=tmp_path))
    result = CLIENT.get(f"/v1/runs/{response.json()['run_id']}", headers=HEADERS).json()
    assert result["status"] == "done"
    assert result["diff_base64"] == "YSBkaWZm"
    assert result["commits"] == []
    assert result["metrics"]["requests"] == 2
    assert result["metrics"]["usd"] == 0.25
    assert len(result["attempts"]) == 1


def test_chain_runs_in_order(api):
    ids = []
    for number in range(1, 4):
        response = post(
            body(request_id=f"t-{number}", after=[f"t-{number - 1}"] if number > 1 else [])
        )
        assert response.status_code == 201
        ids.append(response.json()["run_id"])
    for run_id in ids:
        task = claim.claim(api)
        assert task is not None
        assert CLIENT.get(f"/v1/runs/{run_id}", headers=HEADERS).json()["status"] == "running"
        assert claim.claim(api) is None
        finish.record(
            api,
            finish.EndTask(
                event="end_task", task_id=task["id"], job_id=task["job_id"], status="done"
            ),
        )
    assert claim.claim(api) is None


def test_cancel_chain_and_branch_busy(api):
    first = post().json()["run_id"]
    assert post(body(request_id="unrelated")).json()["error"] == "branch_busy"
    second = post(body(request_id="second", after=["ticket-1"])).json()["run_id"]
    third = post(body(request_id="third", after=["second"])).json()["run_id"]
    assert CLIENT.post(f"/v1/runs/{first}/cancel", headers=HEADERS).status_code == 200
    for run_id, reason in [
        (first, "cancelled_by_client"),
        (second, "predecessor"),
        (third, "predecessor"),
    ]:
        result = CLIENT.get(f"/v1/runs/{run_id}", headers=HEADERS).json()
        assert result["status"] == "cancelled" and result["status_reason"] == reason
    assert claim.claim(api) is None


def test_messages_not_resumable(api):
    run_id = post().json()["run_id"]
    response = CLIENT.post(
        f"/v1/runs/{run_id}/messages",
        headers=HEADERS,
        json={"contract": 1, "message_id": "reply", "text": MARKER},
    )
    assert response.status_code == 409 and response.json()["error"] == "not_resumable"


def test_changed_cursor_is_scoped_and_follows_task_changes(api):
    run_id = post().json()["run_id"]
    page = CLIENT.get("/v1/runs", headers=HEADERS).json()
    assert [r["run_id"] for r in page["runs"]] == [run_id]
    cursor = page["cursor"]
    assert CLIENT.get(f"/v1/runs?changed_after={cursor}", headers=HEADERS).json()["runs"] == []
    task = claim.claim(api)
    changed = CLIENT.get(f"/v1/runs?changed_after={cursor}", headers=HEADERS).json()
    assert [r["status"] for r in changed["runs"]] == ["running"]
    assert CLIENT.get(f"/v1/runs?changed_after={cursor}", headers=OTHER).status_code == 400
    assert task is not None


def test_concurrent_retry(api, monkeypatch):
    monkeypatch.setattr(runs, "connect", lambda: runs.psycopg.connect(api.info.dsn))
    with ThreadPoolExecutor(2) as pool:
        responses = list(pool.map(lambda _: post(), range(2)))
    assert sorted(r.status_code for r in responses) == [200, 201]
    assert responses[0].json()["run_id"] == responses[1].json()["run_id"]
    assert api.execute("SELECT count(*) FROM procrastinate_jobs").fetchone() == (1,)


def test_legacy_alias_uses_run_store_and_application_ownership(api):
    from tests.test_eval_tasks import body as eval_body

    document = eval_body(goal=MARKER)
    response = CLIENT.post("/v1/tasks/eval", headers=HEADERS, json=document)
    assert response.status_code == 201
    task_id = response.json()["task"]
    repeat = CLIENT.post("/v1/tasks/eval", headers=HEADERS, json={**document, "goal": "changed"})
    assert repeat.status_code == 409
    assert repeat.json() == {"error": "Attempt already submitted", "task": task_id}
    assert api.execute("SELECT count(*) FROM homelab_runs").fetchone() == (1,)
    assert CLIENT.get(f"/v1/tasks/eval/{task_id}", headers=HEADERS).status_code == 200
    assert CLIENT.get(f"/v1/tasks/eval/{task_id}", headers=OTHER).status_code == 403
    assert api.execute("SELECT goal FROM homelab_tasks").fetchone() == ("",)


@pytest.mark.parametrize(
    "changes",
    [
        {"mode": "read"},
        {"mode": "read", "branch": None},
        {"limits": {}},
        {"limits": None},
        {"limits": []},
        {"limits": {"requests": None, "minutes": None, "usd": None}},
        {"limits": {"requests": 0}},
        {"limits": {"requests": 1}},
        {"limits": {"requests": 101}},
        {"limits": {"minutes": 1}},
        {"limits": {"usd": 0.01}},
    ],
)
def test_execution_service_requests_are_refused_without_records(api, changes, caplog):
    response = post(body(**changes))
    assert response.status_code == 409
    result = response.json()
    assert result["error"] == result["status_reason"] == "role_not_available"
    assert "execution service" in result["message"]
    for table in ("homelab_runs", "homelab_tasks", "procrastinate_jobs"):
        assert api.execute(f"SELECT count(*) FROM {table}").fetchone() == (0,)
    assert MARKER not in response.text + caplog.text


@pytest.mark.parametrize("status", ["failed", "escalated"])
def test_failed_predecessor_cancels_descendants_and_later_submissions(api, status):
    first = post().json()["run_id"]
    second = post(body(request_id="second", after=["ticket-1"])).json()["run_id"]
    third = post(body(request_id="third", after=["second"])).json()["run_id"]
    task = claim.claim(api)
    finish.record(
        api,
        finish.EndTask(
            event="end_task",
            task_id=task["id"],
            job_id=task["job_id"],
            status=status,
            reason="check_failed",
        ),
    )
    assert CLIENT.get(f"/v1/runs/{first}", headers=HEADERS).json()["status"] == status
    fourth = post(body(request_id="fourth", after=["third"])).json()["run_id"]
    for run_id in [second, third, fourth]:
        record = CLIENT.get(f"/v1/runs/{run_id}", headers=HEADERS).json()
        assert record["status"] == "cancelled" and record["status_reason"] == "predecessor"
    assert claim.claim(api) is None


def test_running_cancellation_is_honest(api):
    run_id = post().json()["run_id"]
    assert claim.claim(api) is not None
    response = CLIENT.post(f"/v1/runs/{run_id}/cancel", headers=HEADERS)
    assert response.status_code == 409 and response.json()["error"] == "not_resumable"
    assert CLIENT.get(f"/v1/runs/{run_id}", headers=HEADERS).json()["status"] == "running"


def test_alias_keeps_independent_eval_attempts_on_the_same_branch(api):
    from tests.test_eval_tasks import body as eval_body

    first = CLIENT.post("/v1/tasks/eval", headers=HEADERS, json=eval_body())
    second = CLIENT.post("/v1/tasks/eval", headers=HEADERS, json=eval_body(attempt="case-2"))
    assert first.status_code == second.status_code == 201
    assert first.json()["task"] != second.json()["task"]


def test_old_eval_rows_remain_readable_and_deduplicated(api):
    from homelab.tasks.submit import submit
    from tests.test_eval_tasks import body as eval_body

    plan = eval_tasks.parse_submission(json.dumps(eval_body()).encode())
    (task_id,) = submit(api, plan)
    response = CLIENT.get(f"/v1/tasks/eval/{task_id}", headers=HEADERS)
    assert response.status_code == 200
    response = CLIENT.post("/v1/tasks/eval", headers=HEADERS, json=eval_body())
    assert response.status_code == 409 and response.json()["task"] == task_id
    assert api.execute("SELECT count(*) FROM homelab_tasks").fetchone() == (1,)


def test_eval_accepts_configuration_folder_name(api):
    response = post(body(eval={"set": "T", "case": "one", "configuration": "pi/base"}))
    assert response.status_code == 201
    record = CLIENT.get(f"/v1/runs/{response.json()['run_id']}", headers=HEADERS).json()
    assert record["executor"] == {"configuration": "pi/base"}
    assert claim.claim(api)["eval_configuration"] == "base"


def test_repository_policy_is_per_key_and_not_stored_in_postgres(api, monkeypatch):
    keys = run_keys.load_keys()
    restricted = keys[0].model_copy(
        update={
            "sha256": hashlib.sha256(b"restricted-fixture").hexdigest(),
            "repositories": {"different/repo": "main"},
        }
    )
    monkeypatch.setattr(run_keys, "load_keys", lambda: [*keys, restricted])
    response = post(headers={"Authorization": "Bearer restricted-fixture"})
    assert response.status_code == 403
    assert response.json()["error"] == "repository_not_allowed"
    assert api.execute("SELECT count(*) FROM homelab_runs").fetchone() == (0,)
    assert api.execute("SELECT to_regclass('homelab_run_repositories')").fetchone() == (None,)
    assert post().status_code == 201


def test_nondefault_main_is_also_refused_without_a_record(api):
    response = post(body(branch="main"))
    assert response.status_code == 409
    assert response.json() == {"error": "branch_is_default"}
    for table in ("homelab_runs", "homelab_tasks", "procrastinate_jobs"):
        assert api.execute(f"SELECT count(*) FROM {table}").fetchone() == (0,)


@pytest.mark.parametrize(
    "changes, error",
    [
        ({"repository": "unknown/repo"}, "repository_not_allowed"),
        ({"branch": "trunk"}, "branch_is_default"),
        ({"branch": "main"}, "branch_is_default"),
    ],
)
def test_alias_uses_the_key_repository_policy(api, changes, error):
    from tests.test_eval_tasks import body as eval_body

    response = CLIENT.post("/v1/tasks/eval", headers=HEADERS, json=eval_body(**changes))
    assert response.status_code == (403 if error == "repository_not_allowed" else 409)
    assert response.json()["error"] == error
    for table in ("homelab_runs", "homelab_tasks", "procrastinate_jobs"):
        assert api.execute(f"SELECT count(*) FROM {table}").fetchone() == (0,)


def test_alias_preserves_historical_submission_validation(api):
    from tests.test_eval_tasks import body as eval_body

    response = CLIENT.post("/v1/tasks/eval", headers=HEADERS, json=eval_body(goal="g" * 200_001))
    assert response.status_code == 201
    assert api.execute("SELECT status, branch FROM homelab_tasks").fetchone() == (
        "queued",
        "eval/case-1",
    )


def test_alias_isolates_applications_with_eval_scope(api, monkeypatch):
    from tests.test_eval_tasks import body as eval_body

    keys = run_keys.load_keys()
    keys = [
        key.model_copy(update={"scopes": ["runs:write", "runs:read", "runs:eval"]}) for key in keys
    ]
    monkeypatch.setattr(run_keys, "load_keys", lambda: keys)
    first = CLIENT.post("/v1/tasks/eval", headers=HEADERS, json=eval_body())
    assert first.status_code == 201
    task_id = first.json()["task"]
    assert CLIENT.get(f"/v1/tasks/eval/{task_id}", headers=OTHER).status_code == 404
    second = CLIENT.post("/v1/tasks/eval", headers=OTHER, json=eval_body())
    assert second.status_code == 201 and second.json()["task"] != task_id


def test_valid_assets_reach_only_the_claimed_ticket_and_are_not_logged(api, caplog):
    text = "private-asset-content-472d"
    asset = {
        "name": "guide",
        "type": "text/plain",
        "version": "1",
        "text": text,
        "sha256": hashlib.sha256(text.encode()).hexdigest(),
    }
    assert post(body(assets=[asset])).status_code == 201
    task = claim.claim(api)
    assert text in task["goal"] and MARKER in task["goal"]
    assert api.execute("SELECT goal FROM homelab_tasks").fetchone() == ("",)
    assert text not in caplog.text and MARKER not in caplog.text
