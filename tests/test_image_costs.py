"""Monthly accounting contract; database checks use an isolated test database."""

import asyncio
import os
from concurrent.futures import ThreadPoolExecutor
from datetime import UTC, datetime
from decimal import Decimal
from types import SimpleNamespace

import psycopg
import pytest
from fastapi.testclient import TestClient

from homelab.api.app import app
from tests.test_image_cost_hook import load_hook


def test_cost_route_uses_only_bearer_key(monkeypatch):
    from homelab.api import image_costs

    seen = []

    def total(key):
        seen.append(key)
        return {"month": "2026-10", "total_usd": "0.006", "charged_calls": 1}

    monkeypatch.setattr(image_costs, "current_total", total)
    client = TestClient(app)
    response = client.get("/v1/images/cost", headers={"Authorization": "Bearer client-a"})
    assert response.status_code == 200
    assert response.json() == {"month": "2026-10", "total_usd": "0.006", "charged_calls": 1}
    assert seen == ["client-a"]
    assert client.get("/v1/images/cost").status_code == 401
    assert client.get("/v1/images/cost?key=client-b").status_code == 400
    assert seen == ["client-a"]


@pytest.fixture
def database(monkeypatch):
    url = os.environ.get("HOMELAB_TEST_DATABASE_URL")
    if not url:
        pytest.skip("Set HOMELAB_TEST_DATABASE_URL to an isolated Postgres database")
    monkeypatch.setenv("HOMELAB_DATABASE_URL", url)
    monkeypatch.setenv("HOMELAB_IMAGE_MONTHLY_BUDGET_USD", "20")
    from homelab.api import image_costs

    image_costs.migrate()
    with psycopg.connect(url) as connection:
        connection.execute("TRUNCATE homelab_image_costs")
    gateway = load_hook(monkeypatch)
    return SimpleNamespace(
        record_cost=lambda key, cost, now=None: asyncio.run(
            gateway.persist_image_cost(key, cost, image_costs.month_at(now))
        ),
        total_for=image_costs.total_for,
        send_alerts=image_costs.send_alerts,
    )


def test_monthly_totals_and_threshold_delivery(database):
    october = datetime(2026, 10, 31, 23, 59, tzinfo=UTC)
    november = datetime(2026, 11, 1, tzinfo=UTC)
    database.record_cost("a", Decimal("15"), october)
    database.record_cost("b", Decimal("1"), october)
    assert database.total_for("a", october) == {
        "month": "2026-10",
        "total_usd": "15",
        "charged_calls": 1,
    }
    delivered = []
    database.send_alerts(delivered.append, october)
    database.send_alerts(delivered.append, october)
    assert len(delivered) == 1 and "80%" in delivered[0]
    database.record_cost("a", Decimal("4"), october)
    database.send_alerts(delivered.append, october)
    database.send_alerts(delivered.append, october)
    assert len(delivered) == 2 and "100%" in delivered[1]
    assert database.total_for("a", october)["charged_calls"] == 2
    assert database.total_for("b", october)["total_usd"] == "1"
    assert database.total_for("a", november)["total_usd"] == "0"
    database.record_cost("a", Decimal("20"), november)
    database.send_alerts(delivered.append, november)
    assert len(delivered) == 4


def test_concurrent_calls_and_senders(database):
    now = datetime(2026, 10, 2, tzinfo=UTC)
    with ThreadPoolExecutor(max_workers=6) as pool:
        list(pool.map(lambda _: database.record_cost("a", Decimal("0.25"), now), range(80)))
    assert database.total_for("a", now) == {
        "month": "2026-10",
        "total_usd": "20.00",
        "charged_calls": 80,
    }
    delivered = []
    with ThreadPoolExecutor(max_workers=6) as pool:
        list(pool.map(lambda _: database.send_alerts(delivered.append, now), range(8)))
    assert len(delivered) == 2
    assert sum("80%" in message for message in delivered) == 1
    assert sum("100%" in message for message in delivered) == 1


def test_unsent_alert_is_retried(database):
    now = datetime(2026, 10, 1, tzinfo=UTC)
    database.record_cost("a", Decimal("16"), now)

    def fail(_message):
        raise OSError("notifier unavailable")

    with pytest.raises(OSError):
        database.send_alerts(fail, now)
    delivered = []
    database.send_alerts(delivered.append, now)
    assert len(delivered) == 1
