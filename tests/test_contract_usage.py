"""Run-api readers accept and record unknown fields, count shapes and name refusals."""

# ruff: noqa: F811

import hashlib
import json
import logging
from datetime import timedelta
from pathlib import Path

import pytest

from homelab.api import contract_usage, run_keys
from homelab.contracts.run_api import ContractUsage, Message, RunRequest
from tests.test_runs import CLIENT, HEADERS, MARKER, OTHER, api, body, post  # noqa: F401
from tests.test_task_store import connection  # noqa: F401

ROOT = Path(__file__).resolve().parents[1]
WRITER = {"Authorization": "Bearer writer-fixture"}
OPERATIONS = (
    "POST /v1/runs",
    "POST /v1/runs/{run_id}/messages",
    "POST /v1/runs/{run_id}/cancel",
)


def key(application, secret, scopes):
    return {
        "application": application,
        "sha256": hashlib.sha256(secret.encode()).hexdigest(),
        "scopes": scopes,
        "repositories": {"example/repo": "trunk"},
    }


@pytest.fixture
def keys(monkeypatch):
    """Keys alone: enough for refusals that happen before any database work."""
    document = json.dumps(
        [
            key("factory", "factory-fixture", ["runs:write", "runs:read", "runs:eval"]),
            key("writer", "writer-fixture", ["runs:write"]),
        ]
    ).encode()
    monkeypatch.setattr(run_keys, "load_keys", lambda: run_keys.parse_keys(document))


@pytest.fixture
def writer(api, monkeypatch):
    loaded = run_keys.load_keys()
    extra = run_keys.parse_keys(json.dumps([key("writer", "writer-fixture", ["runs:write"])]))
    monkeypatch.setattr(run_keys, "load_keys", lambda: [*loaded, *extra])
    return api


def walked(document, model=RunRequest):
    return contract_usage.unknown_fields(model, json.dumps(document).encode())


def test_nested_and_list_unknown_fields():
    document = body(
        reviewer="x",
        attribution={"anything": "free-form"},
        assets=[{"name": "a", "note": MARKER}, {"name": "b", "note": "y", "other": 1}],
        eval={"set": "T", "case": "one", "executor": {"harness": "claude", "extra": 1}},
    )
    document["work"] = {**document["work"], "reviewer": "z", "overlay": {"commit": "c", "x": 1}}
    assert walked(document) == [
        "$.work.reviewer",
        "$.work.overlay.x",
        "$.reviewer",
        "$.assets[].note",
        "$.assets[].other",
        "$.eval.executor.extra",
    ]
    assert walked(body()) == []
    assert walked({"contract": 1, "message_id": "m", "text": "t", "note": 1}, Message) == ["$.note"]
    assert walked([1, 2]) == [] and contract_usage.unknown_fields(RunRequest, b"{") == []


def test_names_are_sanitised_and_capped():
    assert walked(body(**{"bad name": 1, "x" * 65: 1, "ok-name_1": 1})) == [
        "$.?",
        "$.ok-name_1",
    ]
    many = walked(body(**{f"field{number}": number for number in range(40)}))
    assert len(many) == contract_usage.MAX_FIELDS
    assert many[0] == "$.field0" and many[-1] == "$.field31"


def test_error_field_uses_schema_names_only():
    field = contract_usage.error_field
    assert field(RunRequest, ("assets", 0, "sha256")) == "$.assets.*.sha256"
    assert field(RunRequest, ("attribution", MARKER)) == "$.attribution.*"
    assert field(RunRequest, ("eval", "executor", "harness")) == "$.eval.executor.harness"
    assert field(RunRequest, (MARKER, "work")) == "$.*.*"
    assert field(RunRequest, ()) == "$"


def test_schema_hash_matches_committed_document():
    committed = (ROOT / "contracts/run-api/openapi.json").read_bytes()
    assert contract_usage.schema_hash() == hashlib.sha256(committed).hexdigest()


@pytest.mark.parametrize(
    "document, field",
    [
        (body(priority=MARKER), "$.priority"),
        (body(work={"goal": MARKER, "history": MARKER}), "$.work.history"),
        (body(attribution={"case": 7}), "$.attribution.*"),
        (body(base_commit=MARKER), "$.base_commit"),
    ],
)
def test_refusal_names_contract_field_and_schema(keys, document, field, caplog):
    response = post(document)
    assert response.status_code == 422
    assert response.json() == {
        "error": "invalid_request",
        "contract": "run-api",
        "field": field,
        "schema": contract_usage.schema_hash(),
    }
    assert MARKER not in response.text + caplog.text


def counts(database):
    return dict(
        database.execute(
            """SELECT operation, count FROM homelab_contract_usage
               WHERE contract = 'run-api' AND application = 'factory' AND shape = 'base'
                 AND day = current_date"""
        ).fetchall()
    )


def test_counters_for_each_operation(api):
    run_id = post().json()["run_id"]
    assert post().status_code == 200
    reply = CLIENT.post(
        f"/v1/runs/{run_id}/messages",
        headers=HEADERS,
        json={"contract": 1, "message_id": "one", "text": MARKER, "note": MARKER},
    )
    assert reply.status_code == 409 and reply.json()["error"] == "not_resumable"
    assert CLIENT.post(f"/v1/runs/{run_id}/cancel", headers=HEADERS).status_code == 200
    # Refusals before acceptance are not counted, nor are reads.
    assert post(body(priority="x")).status_code == 422
    assert CLIENT.post(f"/v1/runs/{run_id}/cancel", headers=OTHER).status_code == 404
    assert CLIENT.get(f"/v1/runs/{run_id}", headers=HEADERS).status_code == 200
    assert counts(api) == dict(zip(OPERATIONS, (2, 1, 1), strict=True))
    fields = api.execute(
        "SELECT operation, field, count FROM homelab_contract_unknown_fields"
    ).fetchall()
    assert fields == [(OPERATIONS[1], "$.note", 1)]
    stored = api.execute("SELECT to_jsonb(r)::text FROM homelab_contract_unknown_fields r")
    assert all(MARKER not in row[0] for row in stored.fetchall())


def test_recording_failure_leaves_request_working(api, caplog):
    api.execute("DROP TABLE homelab_contract_usage")
    with caplog.at_level(logging.WARNING, logger=contract_usage.__name__):
        response = post(body(unknown=MARKER))
    assert response.status_code == 201
    assert api.execute("SELECT count(*) FROM homelab_runs").fetchone() == (1,)
    warnings = [r for r in caplog.records if r.name == contract_usage.__name__]
    assert len(warnings) == 1 and MARKER not in caplog.text
    assert api.execute("SELECT count(*) FROM homelab_contract_unknown_fields").fetchone() == (0,)


def test_usage_endpoint(writer):
    post(body(unknown=MARKER))
    post(body(request_id="ticket-2"), OTHER)
    writer.execute(
        """INSERT INTO homelab_contract_usage VALUES
               ('run-api', 'factory', 'POST /v1/runs', 'base', current_date - 40, 9)"""
    )
    response = CLIENT.get("/v1/contracts/usage", headers=HEADERS)
    assert response.status_code == 200
    document = response.json()
    ContractUsage.model_validate(document)
    assert set(document) == {"contract", "schema", "since", "shapes", "unknown_fields"}
    assert document["contract"] == "run-api"
    assert document["schema"] == contract_usage.schema_hash()
    today = writer.execute("SELECT current_date").fetchone()[0]
    assert document["since"] == (today - timedelta(days=29)).isoformat()
    assert [
        (row["application"], row["operation"], row["shape"], row["count"])
        for row in document["shapes"]
    ] == [("factory", "POST /v1/runs", "base", 1), ("other", "POST /v1/runs", "base", 1)]
    assert [(row["application"], row["field"]) for row in document["unknown_fields"]] == [
        ("factory", "$.unknown")
    ]
    assert MARKER not in response.text
    assert CLIENT.get("/v1/contracts/usage", headers=OTHER).status_code == 200
    refused = CLIENT.get("/v1/contracts/usage", headers=WRITER)
    assert refused.status_code == 403 and refused.json() == {"error": "scope_required"}
    assert CLIENT.get("/v1/contracts/usage").status_code == 401
