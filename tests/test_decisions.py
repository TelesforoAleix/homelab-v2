"""Decision contract and accounting, using only a fake Jev transport."""

import hashlib
import json
import logging
import os
from concurrent.futures import ThreadPoolExecutor
from contextlib import contextmanager
from datetime import UTC, datetime, timedelta, timezone
from decimal import Decimal
from pathlib import Path
from threading import Lock
from types import SimpleNamespace
from uuid import uuid4

import httpx
import psycopg
import pytest
import yaml
from fastapi.testclient import TestClient
from psycopg import sql
from psycopg.conninfo import make_conninfo

from homelab.api import decisions, run_keys
from homelab.api.app import app

KEY = "typesafe-secret-fixture-9f782c"
CONTENT = "private-decision-text-fixture-4e82"
INSTRUCTIONS = "private-question-fixture-492a"
HEADERS = {"Authorization": "Bearer decision-client"}
DOCUMENT = {
    "text": {"change": CONTENT},
    "questions": {
        "risk": {
            "instructions": INSTRUCTIONS,
            "choices": {"low": "Small change", "high": "Needs review"},
        },
        "ship": {"instructions": "Ready?", "choices": {"yes": "Ready", "no": "Wait"}},
    },
}


def reply(choice="low", probability=0.8, confidence=0.9, tokens=384):
    return {
        "model": "jev-1.14.0",
        "answers": {
            "risk": {
                "type": "choice",
                "choice": choice,
                "confidence": confidence,
                "probabilities": {"low": probability, "high": 1 - probability},
            },
            "ship": {
                "type": "choice",
                "choice": "yes",
                "confidence": 0.8,
                "probabilities": {"yes": 0.9, "no": 0.1},
            },
        },
        "usage": {"input_tokens": tokens, "output_tokens": 40},
    }


class MemoryAccounting:
    def __init__(self):
        self.rows = []
        self.lock = Lock()

    @contextmanager
    def connect(self):
        with self.lock:
            yield self

    def execute(self, query, params=None):
        if "SUM(cost_usd)" in query:
            total = sum(
                (row["cost_usd"] for row in self.rows if params[0] <= row["called_at"] < params[1]),
                Decimal(0),
            )
            return SimpleNamespace(fetchone=lambda: (total,))
        if "INSERT INTO" in query:
            self.rows.append(
                dict(
                    zip(
                        (
                            "called_at",
                            "application",
                            "model",
                            "question_count",
                            "repeats",
                            "input_tokens",
                            "output_tokens",
                            "cost_usd",
                            "status",
                        ),
                        params,
                        strict=True,
                    ),
                    provider="typesafe",
                )
            )
        else:
            assert "pg_advisory_xact_lock" in query


@pytest.fixture
def api(monkeypatch):
    keys = [
        {
            "application": application,
            "sha256": hashlib.sha256(token.encode()).hexdigest(),
            "scopes": scopes,
            "repositories": {},
        }
        for application, token, scopes in [
            ("factory", "decision-client", ["decisions:write"]),
            ("other", "other-client", ["decisions:write"]),
            ("reader", "read-client", ["runs:read"]),
            ("writer", "write-client", ["runs:write", "runs:eval"]),
        ]
    ]
    monkeypatch.setattr(
        run_keys, "load_keys", lambda: run_keys.parse_keys(json.dumps(keys).encode())
    )
    monkeypatch.setenv("HOMELAB_TYPESAFE_API_KEY", KEY)
    monkeypatch.setenv("HOMELAB_DECISIONS_MONTHLY_BUDGET_USD", "20")
    monkeypatch.setenv("HOMELAB_JEV_USD_PER_MILLION_INPUT_TOKENS", "0.042")
    state = SimpleNamespace(
        database=MemoryAccounting(),
        seen=[],
        replies=[reply()],
        now=datetime(2026, 11, 19, tzinfo=UTC),
    )
    monkeypatch.setattr(decisions, "connect", state.database.connect)
    monkeypatch.setattr(decisions, "now_utc", lambda: state.now)

    def handle(request):
        state.seen.append(request)
        assert request.url == "https://api.typesafe.ai/v1/systemone"
        assert request.headers["Authorization"] == f"Bearer {KEY}"
        assert all(value == 30 for value in request.extensions["timeout"].values())
        assert request.method == "POST"
        item = state.replies.pop(0) if len(state.replies) > 1 else state.replies[0]
        if isinstance(item, Exception):
            raise item
        return item if isinstance(item, httpx.Response) else httpx.Response(200, json=item)

    original_client = decisions.provider_client

    def fake_client():
        return original_client(transport=httpx.MockTransport(handle))

    monkeypatch.setattr(decisions, "provider_client", fake_client)
    state.post = lambda document=None, headers=HEADERS: TestClient(app).post(
        "/v1/decisions",
        json=DOCUMENT if document is None else document,
        headers=headers,
    )
    return state


def test_repeats_aggregate_and_record_content_free_cost(api, caplog):
    api.replies = [reply("low", 0.8, 0.9), reply("high", 0.3, 0.6), reply("low", 0.7, 0.9)]
    for item, input_tokens, output_tokens in zip(
        api.replies, (100, 400, 652), (10, 20, 90), strict=True
    ):
        item["usage"] = {"input_tokens": input_tokens, "output_tokens": output_tokens}
    with caplog.at_level(logging.DEBUG):
        response = api.post({**DOCUMENT, "repeats": 3})
    assert response.status_code == 200
    result = response.json()
    assert result["provider"] == "typesafe" and result["model"] == "jev-1.14.0"
    assert result["usage"] == {"input_tokens": 1152, "output_tokens": 120}
    risk = result["questions"]["risk"]
    assert risk["choice"] == "low"
    assert risk["probabilities"] == pytest.approx({"low": 0.6, "high": 0.4})
    assert risk["confidence"] == pytest.approx(0.8)
    assert risk["agreement"] == pytest.approx(2 / 3)
    assert risk["answers"] == [
        {"choice": "low", "confidence": 0.9, "probabilities": {"low": 0.8, "high": 1 - 0.8}},
        {"choice": "high", "confidence": 0.6, "probabilities": {"low": 0.3, "high": 0.7}},
        {"choice": "low", "confidence": 0.9, "probabilities": {"low": 0.7, "high": 1 - 0.7}},
    ]
    assert result["questions"]["ship"]["agreement"] == 1
    assert len(api.seen) == 3
    for request in api.seen:
        assert json.loads(request.content) == {
            "model": "jev-latest",
            "state": DOCUMENT["text"],
            "questions": {
                name: {
                    "type": "choice",
                    "instructions": question["instructions"],
                    "criteria": question["choices"],
                }
                for name, question in DOCUMENT["questions"].items()
            },
        }
    assert api.database.rows == [
        {
            "called_at": api.now,
            "application": "factory",
            "provider": "typesafe",
            "model": "jev-1.14.0",
            "question_count": 2,
            "repeats": 3,
            "input_tokens": 1152,
            "output_tokens": 120,
            "cost_usd": Decimal("0.000048384"),
            "status": "ok",
        }
    ]
    for marker in (KEY, CONTENT, INSTRUCTIONS, "Needs review"):
        assert marker not in str(api.database.rows) + response.text + caplog.text
    assert "probabilities" not in str(api.database.rows)


def test_ties_use_mean_probability_then_choice_order(api):
    api.replies = [reply("low", 0.4), reply("high", 0.2)]
    risk = api.post({**DOCUMENT, "repeats": 2}).json()["questions"]["risk"]
    assert risk["choice"] == "high" and risk["agreement"] == 0.5
    api.replies = [reply("high", 0.5), reply("low", 0.5)]
    assert api.post({**DOCUMENT, "repeats": 2}).json()["questions"]["risk"]["choice"] == "low"


def test_missing_key_is_recorded(api, monkeypatch, caplog):
    monkeypatch.delenv("HOMELAB_TYPESAFE_API_KEY")
    response = api.post()
    assert response.status_code == 503
    assert response.json() == {"error": "decisions_unavailable"}
    assert len(api.database.rows) == 1
    row = api.database.rows[0]
    assert row["status"] == "unavailable" and row["model"] is None
    assert row["input_tokens"] == row["output_tokens"] == row["cost_usd"] == 0
    assert not api.seen
    assert KEY not in response.text + caplog.text


def test_budget_is_global_reached_at_equality_and_resets_in_utc(api, monkeypatch):
    monkeypatch.setenv("HOMELAB_DECISIONS_MONTHLY_BUDGET_USD", "0.000016128")
    assert api.post().status_code == 200
    response = api.post(headers={"Authorization": "Bearer other-client"})
    assert response.status_code == 429
    assert response.json() == {"error": "decisions_budget_reached"}
    assert len(api.seen) == 1
    assert api.database.rows[-1]["status"] == "refused_budget"
    assert api.database.rows[-1]["application"] == "other"
    assert api.database.rows[-1]["cost_usd"] == 0
    api.now = datetime(2026, 11, 30, 23, 30, tzinfo=timezone(timedelta(hours=-1)))
    assert api.post().status_code == 200
    assert len(api.seen) == 2
    assert len(api.database.rows) == 3


def test_default_repeat_and_reported_model(api):
    response = api.post()
    assert response.status_code == 200 and len(api.seen) == 1
    risk = response.json()["questions"]["risk"]
    assert risk["agreement"] == 1 and len(risk["answers"]) == 1
    second = reply()
    second["model"] = "jev-1.15.0"
    api.replies = [reply(), second]
    response = api.post({**DOCUMENT, "repeats": 2})
    assert response.json()["model"] == "jev-1.15.0"
    assert api.database.rows[-1]["model"] == "jev-1.15.0"


def test_crossing_call_completes_and_parallel_next_call_refuses(api, monkeypatch):
    monkeypatch.setenv("HOMELAB_DECISIONS_MONTHLY_BUDGET_USD", "0.000001")
    with ThreadPoolExecutor(max_workers=2) as pool:
        responses = list(pool.map(lambda _: api.post(), range(2)))
    assert sorted(response.status_code for response in responses) == [200, 429]
    assert len(api.seen) == 1 and len(api.database.rows) == 2


def test_local_character_limit_is_total_text_and_inclusive(api):
    document = {**DOCUMENT, "text": {"a": "é" * 40_000, "b": "x" * 40_000}}
    assert api.post(document).status_code == 200
    document["text"]["b"] += "x"
    response = api.post(document)
    assert response.status_code == 413
    assert response.json() == {"error": "decisions_input_too_large"}
    assert len(api.seen) == 1
    assert len(api.database.rows) == 2
    assert api.database.rows[-1]["status"] == "max_tokens_exceeded"


@pytest.mark.parametrize(
    "provider_response, status, error, recorded",
    [
        (
            httpx.Response(400, json={"detail": {"error_type": "api_usage_error", "message": KEY}}),
            422,
            "api_usage_error",
            "api_usage_error",
        ),
        (
            httpx.Response(
                400, json={"detail": {"error_type": "max_tokens_exceeded", "message": KEY}}
            ),
            413,
            "decisions_input_too_large",
            "max_tokens_exceeded",
        ),
        (
            httpx.Response(400, json={"detail": {"error_type": "future_error", "message": KEY}}),
            422,
            "future_error",
            "future_error",
        ),
        (
            httpx.Response(400, json={"detail": {"error_type": KEY, "message": KEY}}),
            502,
            "decisions_provider_error",
            "provider_error",
        ),
        (httpx.Response(500, text=KEY), 502, "decisions_provider_error", "provider_error"),
        (httpx.Response(503, text=KEY), 502, "decisions_provider_error", "provider_error"),
        (httpx.Response(401, text=KEY), 502, "decisions_provider_error", "provider_error"),
        (
            httpx.Response(302, headers={"Location": "https://example.invalid/"}),
            502,
            "decisions_provider_error",
            "provider_error",
        ),
        (httpx.Response(200, text=KEY), 502, "decisions_provider_error", "provider_error"),
        (httpx.ReadTimeout(KEY), 502, "decisions_provider_error", "provider_error"),
        (httpx.ConnectError(KEY), 502, "decisions_provider_error", "provider_error"),
    ],
)
def test_provider_errors_are_mapped_recorded_and_sanitized(
    api, caplog, provider_response, status, error, recorded
):
    api.replies = [provider_response]
    with caplog.at_level(logging.DEBUG):
        response = api.post({**DOCUMENT, "repeats": 3})
    assert response.status_code == status and response.json() == {"error": error}
    assert len(api.seen) == 1 and len(api.database.rows) == 1
    assert api.database.rows[0]["status"] == recorded
    assert KEY not in response.text + caplog.text + str(api.database.rows)


def test_partial_repeat_failure_keeps_known_usage_and_cost(api):
    api.replies = [reply(), httpx.ReadTimeout(KEY)]
    assert api.post({**DOCUMENT, "repeats": 3}).status_code == 502
    assert len(api.seen) == 2 and len(api.database.rows) == 1
    row = api.database.rows[0]
    assert row["input_tokens"] == 384 and row["output_tokens"] == 40
    assert row["cost_usd"] == Decimal("0.000016128")
    assert row["status"] == "provider_error"


@pytest.mark.parametrize(
    "change",
    [
        {"model": KEY},
        {"answers": {}},
        {"usage": {"input_tokens": -1, "output_tokens": 40}},
        {"usage": {"input_tokens": True, "output_tokens": 40}},
    ],
)
def test_invalid_provider_reply_fails_closed(api, change):
    api.replies = [{**reply(), **change}]
    response = api.post()
    assert response.status_code == 502
    assert response.json() == {"error": "decisions_provider_error"}
    assert KEY not in response.text + str(api.database.rows)


@pytest.mark.parametrize(
    "field, value",
    [
        ("choice", CONTENT),
        ("confidence", 1.1),
        ("probabilities", {"low": 1}),
        ("probabilities", {"low": 0.5, "high": 0.5, "extra": 0}),
    ],
)
def test_invalid_provider_answer_fails_closed(api, field, value):
    document = reply()
    document["answers"]["risk"][field] = value
    api.replies = [document]
    assert api.post().status_code == 502


@pytest.mark.parametrize(
    "changes",
    [
        {"text": {}},
        {"text": {"x": 1}},
        {"text": {"": CONTENT}},
        {"questions": {}},
        {"questions": {"risk": {"instructions": "x", "choices": {}}}},
        {"repeats": 0},
        {"repeats": 6},
        {"repeats": True},
        {"repeats": "3"},
        {"provider": KEY},
        {"model": KEY},
    ],
)
def test_invalid_requests_exclude_validation_content(api, changes):
    response = api.post({**DOCUMENT, **changes})
    assert response.status_code == 422 and response.json() == {"error": "invalid_request"}
    assert not api.database.rows and not api.seen
    assert KEY not in response.text and CONTENT not in response.text


@pytest.mark.parametrize(
    "token, status", [("read-client", 403), ("write-client", 403), ("unknown", 401), (None, 401)]
)
def test_requires_decision_scope(api, token, status):
    headers = {"Authorization": f"Bearer {token}"} if token else {}
    assert api.post(headers=headers).status_code == status
    assert not api.database.rows and not api.seen


@pytest.mark.parametrize(
    "variable, value",
    [
        ("HOMELAB_DECISIONS_MONTHLY_BUDGET_USD", "0"),
        ("HOMELAB_DECISIONS_MONTHLY_BUDGET_USD", "NaN"),
        ("HOMELAB_DECISIONS_MONTHLY_BUDGET_USD", "bad"),
        ("HOMELAB_JEV_USD_PER_MILLION_INPUT_TOKENS", "-1"),
        ("HOMELAB_JEV_USD_PER_MILLION_INPUT_TOKENS", "Infinity"),
    ],
)
def test_invalid_money_configuration_fails_closed(api, monkeypatch, variable, value):
    monkeypatch.setenv(variable, value)
    assert api.post().status_code == 503
    assert api.database.rows[-1]["status"] == "unavailable"
    assert not api.seen


def test_zero_price_is_valid_and_five_repeats_supported(api, monkeypatch):
    monkeypatch.setenv("HOMELAB_JEV_USD_PER_MILLION_INPUT_TOKENS", "0")
    response = api.post({**DOCUMENT, "repeats": 5})
    assert response.status_code == 200 and len(api.seen) == 5
    assert api.database.rows[-1]["cost_usd"] == 0


def test_database_failure_is_sanitized(api, monkeypatch, caplog):
    def fail():
        raise psycopg.OperationalError(KEY)

    monkeypatch.setattr(decisions, "connect", fail)
    response = api.post()
    assert response.status_code == 503
    assert response.json() == {"error": "decisions_unavailable"}
    assert KEY not in response.text + caplog.text


def test_compose_limits_configuration_to_api():
    compose = yaml.safe_load(Path("compose.yaml").read_text())
    variables = {
        "HOMELAB_TYPESAFE_API_KEY": "${HOMELAB_TYPESAFE_API_KEY:-}",
        "HOMELAB_DECISIONS_MONTHLY_BUDGET_USD": "${HOMELAB_DECISIONS_MONTHLY_BUDGET_USD:-20}",
        "HOMELAB_JEV_USD_PER_MILLION_INPUT_TOKENS": (
            "${HOMELAB_JEV_USD_PER_MILLION_INPUT_TOKENS:-0.042}"
        ),
    }
    for service, configuration in compose["services"].items():
        environment = configuration.get("environment", {})
        for variable, value in variables.items():
            if service == "api":
                assert environment[variable] == value
            else:
                assert variable not in environment


@pytest.fixture
def postgres(api, monkeypatch):
    database_url = os.environ.get("HOMELAB_TEST_DATABASE_URL")
    if not database_url:
        pytest.skip("Set HOMELAB_TEST_DATABASE_URL to an isolated Postgres database")
    name = "decision_test_" + uuid4().hex
    with psycopg.connect(database_url) as admin:
        admin.execute(sql.SQL("CREATE SCHEMA {}").format(sql.Identifier(name)))
    url = make_conninfo(database_url, options="-c search_path=" + name)
    monkeypatch.setattr(decisions, "connect", lambda: psycopg.connect(url))
    try:
        decisions.migrate()
        decisions.migrate()
        yield api, url
    finally:
        with psycopg.connect(database_url) as admin:
            admin.execute(sql.SQL("DROP SCHEMA {} CASCADE").format(sql.Identifier(name)))


def test_postgres_accounting_cap_and_month_rollover(postgres, monkeypatch):
    api, url = postgres
    monkeypatch.setenv("HOMELAB_DECISIONS_MONTHLY_BUDGET_USD", "0.000048384")
    assert api.post({**DOCUMENT, "repeats": 3}).status_code == 200
    assert api.post().status_code == 429
    api.now = datetime(2026, 12, 1, tzinfo=UTC)
    assert api.post().status_code == 200
    monkeypatch.delenv("HOMELAB_TYPESAFE_API_KEY")
    assert api.post().status_code == 503
    with psycopg.connect(url) as connection:
        rows = connection.execute(
            "SELECT input_tokens, output_tokens, cost_usd, status FROM homelab_decision_calls "
            "ORDER BY id"
        ).fetchall()
        serialized = connection.execute(
            "SELECT to_jsonb(t)::text FROM homelab_decision_calls t"
        ).fetchall()
    assert rows == [
        (1152, 120, Decimal("0.000048384"), "ok"),
        (0, 0, 0, "refused_budget"),
        (384, 40, Decimal("0.000016128"), "ok"),
        (0, 0, 0, "unavailable"),
    ]
    assert all(marker not in str(serialized) for marker in (KEY, CONTENT, INSTRUCTIONS, "answers"))


def test_postgres_concurrent_admission(postgres, monkeypatch):
    api, url = postgres
    monkeypatch.setenv("HOMELAB_DECISIONS_MONTHLY_BUDGET_USD", "0.000001")
    with ThreadPoolExecutor(max_workers=3) as pool:
        responses = list(pool.map(lambda _: api.post(), range(3)))
    assert sorted(response.status_code for response in responses) == [200, 429, 429]
    assert len(api.seen) == 1
    with psycopg.connect(url) as connection:
        assert connection.execute("SELECT count(*) FROM homelab_decision_calls").fetchone() == (3,)
