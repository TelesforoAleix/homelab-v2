"""Version-one runs: durable idempotency, ownership and today's task bridge."""

import hashlib
import json
from concurrent.futures import ThreadPoolExecutor
from contextlib import nullcontext

import pytest
from fastapi.testclient import TestClient

from homelab.api import run_keys, runs
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
    assert record["executor"] == load_executors()["ticket"]["standard"][0].selection()
    assert len(record["configuration_hash"]) == 64
    assert record["attribution"] == {"case": "one"}
    assert MARKER not in json.dumps(record)
    apply_schema(api)
    assert api.execute("SELECT count(*) FROM homelab_runs").fetchone() == (1,)


@pytest.mark.parametrize(
    "changes, status, error",
    [
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


def test_unknown_field_is_ignored_and_recorded(api, caplog):
    response = post(body(unknown=MARKER))
    assert response.status_code == 201
    stored = api.execute("SELECT request::text FROM homelab_runs").fetchone()[0]
    assert "unknown" not in stored and MARKER not in stored + response.text + caplog.text
    assert api.execute("SELECT field FROM homelab_contract_unknown_fields").fetchall() == [
        ("$.unknown",)
    ]


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


def test_eval_scope_executor_and_commits(api):
    document = body(
        branch="eval/one",
        eval={"set": "T", "case": "one", "executor": {"configuration": "pi/base"}},
    )
    assert post(document, OTHER).status_code == 403
    response = post(document)
    assert response.status_code == 201
    task = claim.claim(api, legacy_roles=False)
    assert task["eval_set"] == "T" and task["eval_configuration"] == "base"
    assert task["goal"] == MARKER
    finish.record(
        api,
        finish.EndTask(
            event="end_task",
            task_id=task["id"],
            job_id=task["job_id"],
            status="done",
            commits=["b" * 40],
        ),
    )
    result = CLIENT.get(f"/v1/runs/{response.json()['run_id']}", headers=HEADERS).json()
    assert result["status"] == "done" and result["commits"] == ["b" * 40]
    assert result["executor"] == {"configuration": "pi/base"}
    assert "diff_base64" not in result


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


def test_real_retry_accepts_a_pre_overlay_request_fingerprint(api):
    from homelab.contracts.run_api import RunRequest

    run_id = post().json()["run_id"]
    historical = RunRequest.model_validate(body()).model_dump()
    del historical["work"]["overlay"]  # This field did not exist before the upgrade.
    digest = hashlib.sha256(
        json.dumps(historical, sort_keys=True, separators=(",", ":")).encode()
    ).hexdigest()
    api.execute("UPDATE homelab_runs SET request_hash = %s WHERE id = %s", (digest, run_id))
    assert post().status_code == 200
    assert api.execute("SELECT count(*) FROM homelab_tasks").fetchone() == (1,)


def assert_matches_models(document, model):
    """The document is valid for the model, and the model names every key it carries."""
    parsed = model.model_validate(document)
    assert set(document) == {name for name, _ in parsed if name != "check" or "check" in document}
    return parsed


def test_responses_validate_against_the_contract_models(api):
    from homelab.contracts.run_api import RunCreated, RunPage, RunRecord

    created = post()
    assert RunCreated.model_validate(created.json())
    run_id = created.json()["run_id"]
    task = claim.claim(api)
    finish.record(
        api,
        finish.EndTask(
            event="end_task",
            task_id=task["id"],
            job_id=task["job_id"],
            status="done",
            commits=["b" * 40],
        ),
    )
    record = CLIENT.get(f"/v1/runs/{run_id}", headers=HEADERS).json()
    assert record["attempts"]
    assert_matches_models(record, RunRecord)
    # A check record carries its result; no other record has the key.
    api.execute(
        """UPDATE homelab_runs SET request = request || %s::jsonb WHERE id = %s""",
        (json.dumps({"role": "check", "check": {"exit_code": 1, "tail": "x"}}), run_id),
    )
    checked = CLIENT.get(f"/v1/runs/{run_id}", headers=HEADERS).json()
    assert checked["check"] == {"exit_code": 1, "tail": "x"} and checked["executor"] is None
    assert_matches_models(checked, RunRecord)
    page = CLIENT.get("/v1/runs", headers=HEADERS).json()
    assert page["runs"] and set(page) == set(RunPage.model_fields)
    RunPage.model_validate(page)
    queued = post(body(request_id="second", branch="work/other")).json()["run_id"]
    cancelled = CLIENT.post(f"/v1/runs/{queued}/cancel", headers=HEADERS).json()
    assert cancelled["status"] == "cancelled"
    assert_matches_models(cancelled, RunRecord)


def test_record_builder_output_validates_without_a_database():
    from datetime import UTC, datetime
    from uuid import uuid4

    from fastapi.encoders import jsonable_encoder

    from homelab.contracts.run_api import RunRecord

    now = datetime.now(UTC)
    attempt = (1, "done", None, now, now, "h" * 64, "run", 0, 3, 10, 5, 7, 0.5, 90.0)

    class Connection:
        def __init__(self, rows):
            self.rows = rows

        def execute(self, *_):
            return self

        def fetchall(self):
            return self.rows

    saved = {
        "id": uuid4(),
        "task_id": 1,
        "request_id": "ticket-1",
        "application": "factory",
        "request": {**body(), "role": "ticket"},
        "status": "done",
        "status_reason": None,
        "executor": {"harness": "claude", "model": "sonnet"},
        "configuration_hash": "h" * 64,
        "commits": ["b" * 40],
        "resumable": False,
        "final_message": None,
        "resume_at": None,
        "created_at": now,
        "updated_at": now,
        "started_at": now,
        "finished_at": now,
    }
    for rows in ([attempt], []):
        document = jsonable_encoder(runs.record(Connection(rows), saved))
        assert_matches_models(document, RunRecord)
    saved["request"] = {**saved["request"], "role": "check", "check": {"exit_code": 0, "tail": ""}}
    document = jsonable_encoder(runs.record(Connection([attempt]), saved))
    assert document["check"] == {"exit_code": 0, "tail": ""} and document["executor"] is None
    assert_matches_models(document, RunRecord)
