"""The eval write key gates both routes, without exposing task input on refusal."""

import base64
from concurrent.futures import ThreadPoolExecutor
from contextlib import nullcontext
from datetime import UTC, datetime
from decimal import Decimal
from pathlib import Path
from types import SimpleNamespace

import pytest
import yaml
from fastapi.testclient import TestClient

from homelab.api import eval_tasks
from homelab.api.app import app
from homelab.settings import Settings
from homelab.tasks import finish
from homelab.tasks.store import Refused
from tests.test_task_store import connection  # noqa: F401

# Deliberately fake; no test uses an installed key.
KEY = "fixture-eval-key"
HEADERS = {"Authorization": f"Bearer {KEY}"}
CLIENT = TestClient(app)


def body(**changes):
    return {
        "attempt": "case-1",
        "repository": "example/repo",
        "branch": "eval/case-1",
        "base_commit": "a" * 40,
        "title": "Docs case",
        "goal": "Update the sample docs",
        "allowed_files": ["docs/example.md"],
        "check_command": "test -f docs/example.md",
        "pinned_paths": ["tests/check.sh"],
        "eval": {"set": "T", "case": "docs", "configuration": "base"},
        **changes,
    }


@pytest.fixture(autouse=True)
def settings(monkeypatch, tmp_path):
    settings = Settings(_env_file=None, eval_task_key=KEY, task_runs_dir=tmp_path)
    monkeypatch.setattr(eval_tasks, "get_settings", lambda: settings)
    return settings


@pytest.mark.parametrize("key", [None, ""])
@pytest.mark.parametrize("method, path", [("post", "/v1/tasks/eval"), ("get", "/v1/tasks/eval/1")])
def test_disabled_without_key(monkeypatch, method, path, key):
    monkeypatch.setenv("HOMELAB_EVAL_TASK_KEY", key or "")
    settings = Settings(_env_file=None)
    assert settings.eval_task_key.get_secret_value() == ""
    monkeypatch.setattr(eval_tasks, "get_settings", lambda: settings)
    response = getattr(CLIENT, method)(path, headers=HEADERS)
    assert response.status_code == 503
    assert response.json() == {"error": "Eval tasks are not configured"}


@pytest.mark.parametrize("authorization", [None, "Bearer wrong-fixture-key", "Basic fixture"])
@pytest.mark.parametrize("method, path", [("post", "/v1/tasks/eval"), ("get", "/v1/tasks/eval/1")])
def test_key_required(method, path, authorization, caplog):
    response = getattr(CLIENT, method)(
        path, headers={} if authorization is None else {"Authorization": authorization}
    )
    assert response.status_code == 401
    assert response.json() == {"error": "Eval task key required"}
    assert KEY not in caplog.text
    assert (authorization or "not-present") not in caplog.text


@pytest.mark.parametrize(
    "changes",
    [
        {"attempt": "../private-input"},
        {"attempt": "a" * 65},
        {"attempt": "private-input\n"},
        {"allowed_files": ["../private-input"]},
        {"pinned_paths": ["/private-input"]},
        {"base_commit": "private-input"},
        {"branch": "-private-input"},
        {"goal": 123},
        {"eval": {"set": "T", "case": "x", "configuration": "private-input"}},
        {"private-input": "private-input"},
        {"eval": {"set": "T", "case": "x", "configuration": "base", "private-input": 1}},
        {"position": 1},
        {"depends_on": []},
        {"eval": None},
    ],
)
def test_refused_body_is_redacted(monkeypatch, changes, caplog):
    def no_database(*args, **kwargs):
        pytest.fail("invalid submission reached Postgres")

    monkeypatch.setattr(eval_tasks.psycopg, "connect", no_database)
    response = CLIENT.post("/v1/tasks/eval", headers=HEADERS, json=body(**changes))
    assert response.status_code == 422
    assert set(response.json()) == {"error"}
    assert "private-input" not in response.text
    assert "private-input" not in caplog.text
    assert KEY not in caplog.text


@pytest.mark.parametrize("raw", [b"not json", b"[]", b"{}", b"\xff"])
def test_malformed_body(raw):
    response = CLIENT.post("/v1/tasks/eval", headers=HEADERS, content=raw)
    assert response.status_code == 422
    assert set(response.json()) == {"error"}


class Rows:
    def __init__(self, rows):
        self.rows = rows

    def fetchone(self):
        return self.rows[0] if self.rows else None

    def fetchall(self):
        return self.rows


def stub_database(monkeypatch, task=(1, "done", "eval_kept"), attempts=()):
    calls = []

    def execute(query, params):
        calls.append((query, params))
        return Rows(
            attempts if "homelab_task_attempts" in query else ([] if task is None else [task])
        )

    database = SimpleNamespace(execute=execute)
    monkeypatch.setattr(eval_tasks.psycopg, "connect", lambda *a, **kw: nullcontext(database))
    return calls


def test_submit_uses_shared_models_and_submit(monkeypatch):
    stub_database(monkeypatch, task=None)
    plans = []
    monkeypatch.setattr(eval_tasks, "submit", lambda db, plan: plans.append(plan) or [42])
    response = CLIENT.post("/v1/tasks/eval", headers=HEADERS, json=body())
    assert response.status_code == 201
    assert response.json() == {"task": 42}
    (plan,) = plans
    assert plan.slice == "eval:case-1"
    assert plan.branch == "eval/case-1"
    (task,) = plan.tasks
    assert task.position == 1
    assert task.eval.model_dump() == body()["eval"]


def test_repeat_returns_original_even_if_other_fields_change(monkeypatch):
    stub_database(monkeypatch, task=(42,))
    response = CLIENT.post("/v1/tasks/eval", headers=HEADERS, json=body(repository="other/repo"))
    assert response.status_code == 409
    assert response.json() == {"error": "Attempt already submitted", "task": 42}


def test_concurrent_duplicate_is_409(monkeypatch):
    rows = iter([Rows([]), Rows([(42,)])])
    database = SimpleNamespace(execute=lambda *args: next(rows))
    monkeypatch.setattr(eval_tasks.psycopg, "connect", lambda *a, **kw: nullcontext(database))

    def concurrent_submit(*args):
        raise Refused("this slice already has a task at one of these positions")

    monkeypatch.setattr(eval_tasks, "submit", concurrent_submit)
    response = CLIENT.post("/v1/tasks/eval", headers=HEADERS, json=body())
    assert response.status_code == 409
    assert response.json()["task"] == 42


def test_non_eval_or_unknown_task_is_404(monkeypatch):
    calls = stub_database(monkeypatch, task=None)
    response = CLIENT.get("/v1/tasks/eval/1", headers=HEADERS)
    assert response.status_code == 404
    assert "slice LIKE 'eval:%%'" in calls[0][0]


def attempt(number, *, ended=True, run_id="run-1", usage=(3, 10, 4, 6, Decimal("0.25"))):
    now = datetime(2026, 11, 13, tzinfo=UTC)
    return (
        number,
        "done" if ended else "running",
        "eval_kept" if ended else None,
        now,
        now if ended else None,
        "a" * 64,
        run_id,
        *usage,
    )


def test_read_attempts_and_last_round_diff(monkeypatch, settings):
    folder = settings.task_runs_dir / "run-1"
    folder.mkdir()
    data = b"final diff\x00\xff"
    for name, content in [
        ("diff.patch", b"first"),
        ("diff-2.patch", b"second"),
        ("diff-10.patch", data),
        ("diff-bogus.patch", b"ignore"),
    ]:
        (folder / name).write_bytes(content)
    stub_database(monkeypatch, attempts=[attempt(1), attempt(2, ended=False, run_id="other")])
    response = CLIENT.get("/v1/tasks/eval/1", headers=HEADERS)
    assert response.status_code == 200
    result = response.json()
    assert result["task"] == 1
    assert result["status"] == "done"
    assert result["reason"] == "eval_kept"
    assert base64.b64decode(result["diff_base64"]) == data
    assert result["attempts"][0] == {
        "number": 1,
        "status": "done",
        "reason": "eval_kept",
        "started_at": "2026-11-13T00:00:00+00:00",
        "ended_at": "2026-11-13T00:00:00+00:00",
        "configuration_hash": "a" * 64,
        "usage": {
            "steps": 3,
            "fresh_tokens": 10,
            "cached_tokens": 4,
            "output_tokens": 6,
            "cost_usd": 0.25,
        },
    }
    assert result["attempts"][1]["ended_at"] is None


@pytest.mark.parametrize(
    "size, too_large",
    [(0, False), (eval_tasks.DIFF_LIMIT, False), (eval_tasks.DIFF_LIMIT + 1, True)],
)
def test_diff_size(monkeypatch, settings, size, too_large):
    folder = settings.task_runs_dir / "run-1"
    folder.mkdir()
    (folder / "diff.patch").write_bytes(b"x" * size)
    stub_database(monkeypatch, attempts=[attempt(1)])
    result = CLIENT.get("/v1/tasks/eval/1", headers=HEADERS).json()
    if too_large:
        assert result["diff_base64"] is None
        assert result["diff_too_large"] is True
    else:
        assert len(base64.b64decode(result["diff_base64"])) == size
        assert "diff_too_large" not in result


@pytest.mark.parametrize("run_id", ["../outside", "a/b", "/absolute", ".", "..", None])
def test_invalid_run_id_never_reads(monkeypatch, settings, run_id):
    def no_read(*args):
        pytest.fail("unsafe run_id reached the filesystem")

    monkeypatch.setattr(Path, "iterdir", no_read)
    stub_database(monkeypatch, attempts=[attempt(1, run_id=run_id)])
    assert CLIENT.get("/v1/tasks/eval/1", headers=HEADERS).json()["diff_base64"] is None


@pytest.mark.parametrize("link_folder", [False, True])
def test_diff_symlinks_are_not_followed(settings, link_folder):
    outside = settings.task_runs_dir.parent / (settings.task_runs_dir.name + "-outside")
    outside.mkdir()
    (outside / "diff.patch").write_bytes(b"private bytes")
    folder = settings.task_runs_dir / "run-1"
    if link_folder:
        folder.symlink_to(outside, target_is_directory=True)
    else:
        folder.mkdir()
        (folder / "diff.patch").symlink_to(outside / "diff.patch")
    assert eval_tasks.read_diff(settings.task_runs_dir, "run-1") == {"diff_base64": None}


def test_no_attempts_or_missing_diff(monkeypatch):
    stub_database(monkeypatch)
    result = CLIENT.get("/v1/tasks/eval/1", headers=HEADERS).json()
    assert result["attempts"] == []
    assert result["diff_base64"] is None
    stub_database(monkeypatch, attempts=[attempt(1, usage=(None,) * 5)])
    result = CLIENT.get("/v1/tasks/eval/1", headers=HEADERS).json()
    assert all(v is None for v in result["attempts"][0]["usage"].values())
    assert result["diff_base64"] is None


def test_compose_only_api_gets_key_and_run_mount():
    services = yaml.safe_load(Path("compose.yaml").read_text())["services"]
    api = services["api"]
    assert api["environment"]["HOMELAB_EVAL_TASK_KEY"] == "${HOMELAB_EVAL_TASK_KEY:-}"
    assert any(
        "HOMELAB_TASK_RUNS_PATH" in volume and volume.endswith(":ro") for volume in api["volumes"]
    )
    for name, service in services.items():
        if name != "api":
            assert "HOMELAB_EVAL_TASK_KEY" not in service.get("environment", {})
            assert not any("HOMELAB_TASK_RUNS_PATH" in v for v in service.get("volumes", []))


def test_submit_and_read_postgres(connection, monkeypatch, settings):  # noqa: F811
    monkeypatch.setattr(
        eval_tasks, "psycopg", SimpleNamespace(connect=lambda *a, **kw: nullcontext(connection))
    )
    response = CLIENT.post("/v1/tasks/eval", headers=HEADERS, json=body())
    assert response.status_code == 201
    task_id = response.json()["task"]
    row = connection.execute(
        "SELECT slice, eval_set, eval_case, eval_configuration FROM homelab_tasks WHERE id = %s",
        (task_id,),
    ).fetchone()
    assert row == ("eval:case-1", "T", "docs", "base")
    assert connection.execute("SELECT count(*) FROM homelab_tasks").fetchone() == (1,)
    assert connection.execute("SELECT count(*) FROM procrastinate_jobs").fetchone() == (1,)
    response = CLIENT.post("/v1/tasks/eval", headers=HEADERS, json=body())
    assert response.status_code == 409
    route = {"kind": "litellm", "purpose": "task-worker"}
    finish.record(
        connection,
        finish.BeginAttempt(
            event="begin_attempt",
            task_id=task_id,
            number=1,
            route=route,
        ),
    )
    finish.record(
        connection,
        finish.EndAttempt(
            event="end_attempt",
            task_id=task_id,
            number=1,
            route=route,
            status="done",
            reason="eval_kept",
            run_id="run-1",
            configuration_hash="a" * 64,
            steps=2,
            fresh_tokens=10,
            cached_tokens=4,
            output_tokens=6,
            cost_usd=0.25,
            review_route=route,
            reviews=[finish.Review(result="pass", steps=1, cost_usd=0.5)],
        ),
    )
    result = CLIENT.get(f"/v1/tasks/eval/{task_id}", headers=HEADERS).json()
    assert result["attempts"][0]["usage"] == {
        "steps": 3,
        "fresh_tokens": 10,
        "cached_tokens": 4,
        "output_tokens": 6,
        "cost_usd": 0.75,
    }
    connection.execute("UPDATE homelab_task_traces SET cost_usd = NULL WHERE kind = 'review'")
    result = CLIENT.get(f"/v1/tasks/eval/{task_id}", headers=HEADERS).json()
    assert result["attempts"][0]["usage"]["cost_usd"] is None
    connection.execute("UPDATE homelab_tasks SET slice = 'ordinary'")
    assert CLIENT.get(f"/v1/tasks/eval/{task_id}", headers=HEADERS).status_code == 404


@pytest.mark.parametrize("task_id", ["not-a-task", "0", "-1", str(2**63), "9" * 100])
def test_invalid_task_id_is_authenticated_before_rejection(monkeypatch, task_id):
    def no_database(*args, **kwargs):
        pytest.fail("invalid id reached Postgres")

    monkeypatch.setattr(eval_tasks.psycopg, "connect", no_database)
    assert CLIENT.get(f"/v1/tasks/eval/{task_id}").status_code == 401
    response = CLIENT.get(f"/v1/tasks/eval/{task_id}", headers=HEADERS)
    assert response.status_code == 404
    assert response.json() == {"error": "Task not found"}


def test_concurrent_submissions_postgres(connection, settings):  # noqa: F811
    settings.database_url = connection.info.dsn

    def start(_):
        return CLIENT.post("/v1/tasks/eval", headers=HEADERS, json=body())

    with ThreadPoolExecutor(max_workers=2) as pool:
        responses = list(pool.map(start, range(2)))
    assert sorted(r.status_code for r in responses) == [201, 409]
    assert responses[0].json()["task"] == responses[1].json()["task"]
    assert connection.execute("SELECT count(*) FROM homelab_tasks").fetchone() == (1,)
    assert connection.execute("SELECT count(*) FROM procrastinate_jobs").fetchone() == (1,)
