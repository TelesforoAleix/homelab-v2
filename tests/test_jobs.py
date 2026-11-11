from contextlib import contextmanager

from homelab.jobs.schema import ensure_schema


class FakeSchemaManager:
    def __init__(self):
        self.applied = False

    def apply_schema(self):
        self.applied = True


class FakeApp:
    def __init__(self, schema_exists):
        self.schema_exists = schema_exists
        self.schema_manager = FakeSchemaManager()
        self.is_open = False

    @contextmanager
    def open(self):
        self.is_open = True
        try:
            yield
        finally:
            self.is_open = False

    def check_connection(self):
        assert self.is_open
        return self.schema_exists


def test_ensure_schema_applies_when_absent():
    app = FakeApp(schema_exists=False)

    assert ensure_schema(app) is True
    assert app.schema_manager.applied is True
    assert app.is_open is False


def test_ensure_schema_skips_when_present():
    app = FakeApp(schema_exists=True)

    assert ensure_schema(app) is False
    assert app.schema_manager.applied is False
    assert app.is_open is False


# Reuse the repository's isolated-Postgres fixture; never point this at the live node.
from tests.test_task_store import connection  # noqa: E402, F401


def public_offer():
    return {
        "at": "2026-11-11T12:00:00Z",
        "profile": "working",
        "power": "battery",
        "paused": False,
        "models": [{"id": "sample", "kind": "chat", "state": "loaded", "context": 4096}],
        "memory_free_mb": 1234,
        "queue": {"playing": 1, "running": 1, "waiting": 2},
        "night_window": None,
    }


async def test_offer_read_fixed_path_timeout_and_shape(monkeypatch):
    import httpx

    from homelab.jobs import providers

    original = httpx.AsyncClient
    seen = []

    def handle(request):
        seen.append(request)
        return httpx.Response(200, json=public_offer())

    def client(**kwargs):
        assert kwargs == {"timeout": 3, "trust_env": False, "follow_redirects": False}
        return original(transport=httpx.MockTransport(handle), **kwargs)

    monkeypatch.setattr(providers.httpx, "AsyncClient", client)
    assert await providers.read_offer("http://mac.invalid/v1") == public_offer()
    assert str(seen[0].url) == "http://mac.invalid/homelab-offer.json"
    assert seen[0].method == "GET" and not seen[0].content


async def test_offer_total_three_second_limit(monkeypatch):
    import asyncio
    from time import monotonic

    import httpx
    import pytest

    from homelab.jobs import providers

    original = httpx.AsyncClient

    class Trickle(httpx.AsyncByteStream):
        async def __aiter__(self):
            for _ in range(120):
                await asyncio.sleep(0.25)
                yield b" "

    async def slow(request):
        return httpx.Response(200, stream=Trickle())

    monkeypatch.setattr(
        providers.httpx,
        "AsyncClient",
        lambda **kwargs: original(transport=httpx.MockTransport(slow), **kwargs),
    )
    started = monotonic()
    with pytest.raises(TimeoutError):
        await providers.read_offer("http://mac.invalid/v1")
    assert 2.9 <= monotonic() - started < 3.5


def test_offer_validation_rejects_extra_fields_at_every_level():
    import copy
    import json

    import pytest
    from pydantic import ValidationError

    from homelab.jobs.providers import Offer

    value = public_offer()
    value["watts"] = 20.5
    value["night_window"] = {"start": "22:00", "end": "06:00"}
    assert Offer.model_validate_json(json.dumps(value)).model_dump(mode="json") == value
    for scope in ("root", "models", "queue", "night_window"):
        bad = copy.deepcopy(value)
        target = bad if scope == "root" else bad[scope][0] if scope == "models" else bad[scope]
        target["private"] = "/private/path"
        with pytest.raises(ValidationError):
            Offer.model_validate_json(json.dumps(bad))
    for field, invalid in (("profile", "custom"), ("power", "unknown"), ("watts", -1)):
        with pytest.raises(ValidationError):
            Offer.model_validate_json(json.dumps({**value, field: invalid}))


async def test_poll_failure_is_content_free_and_records_failed_read(monkeypatch, caplog):
    from contextlib import nullcontext

    import httpx

    from homelab.jobs import providers

    values = []
    monkeypatch.setenv("HOMELAB_MAC_VISION_URL", "http://mac.invalid/v1")

    async def unavailable(base):
        raise httpx.ConnectError("private content")

    monkeypatch.setattr(providers, "read_offer", unavailable)
    monkeypatch.setattr(providers.psycopg, "connect", lambda *args: nullcontext(object()))
    monkeypatch.setattr(providers, "save_observation", lambda conn, offer: values.append(offer))
    with caplog.at_level("INFO", logger=providers.__name__):
        await providers.poll()
    assert values == [None]
    assert "provider=mac reason=unavailable" in caplog.text
    assert "private content" not in caplog.text


def test_provider_freshness_and_retention_in_postgres(connection):  # noqa: F811
    from homelab.jobs.providers import provider_rows, save_observation
    from homelab.tasks.store import apply_schema

    apply_schema(connection)
    save_observation(connection, None)
    assert provider_rows(connection) == [
        {"provider": "mac", "offer": None, "seen_at": None, "state": "away"}
    ]
    save_observation(connection, public_offer())
    row = provider_rows(connection)[0]
    assert row["offer"] == public_offer() and row["state"] == "fresh"
    for age, expected in ((89, "fresh"), (91, "away")):
        connection.execute(
            "UPDATE homelab_providers SET seen_at = clock_timestamp() - %s",
            (__import__("datetime").timedelta(seconds=age),),
        )
        # CLI must age even without the polling task running.
        assert provider_rows(connection)[0]["state"] == expected
        save_observation(connection, None)
        row = provider_rows(connection)[0]
        assert row["state"] == expected and row["offer"] == public_offer()
        assert connection.execute("SELECT state FROM homelab_providers").fetchone()[0] == expected
    before = provider_rows(connection)
    apply_schema(connection)
    assert provider_rows(connection) == before
    save_observation(connection, {**public_offer(), "profile": "inference"})
    assert provider_rows(connection)[0]["state"] == "fresh"
    assert provider_rows(connection)[0]["offer"]["profile"] == "inference"


def test_offer_task_uses_procrastinate_minute_schedule():
    from homelab.jobs.app import app
    from homelab.jobs.tasks import read_provider_offer

    periodic = app.periodic_registry.periodic_tasks[(read_provider_offer.name, "")]
    assert periodic.cron == "* * * * *"
    assert read_provider_offer.queue == "default"


def test_providers_cli_reads_table_without_contacting_mac(monkeypatch, capsys):
    import json
    from contextlib import nullcontext

    import psycopg

    from homelab import cli
    from homelab.jobs import providers

    rows = [{"provider": "mac", "offer": public_offer(), "seen_at": None, "state": "away"}]
    monkeypatch.setattr(psycopg, "connect", lambda *args: nullcontext(object()))
    monkeypatch.setattr(providers, "provider_rows", lambda conn: rows)
    assert cli.main(["providers"]) == 0
    assert json.loads(capsys.readouterr().out) == rows


async def test_offer_redirects_and_invalid_payloads_are_not_followed_or_saved(monkeypatch):
    import httpx
    import pytest

    from homelab.jobs import providers

    original = httpx.AsyncClient
    for response in (
        httpx.Response(302, headers={"location": "http://elsewhere.invalid/command"}),
        httpx.Response(200, json={**public_offer(), "path": "/private"}),
    ):
        seen = []

        def handle(request, response=response, seen=seen):
            seen.append(request)
            return response

        monkeypatch.setattr(
            providers.httpx,
            "AsyncClient",
            lambda handle=handle, **kwargs: original(
                transport=httpx.MockTransport(handle), **kwargs
            ),
        )
        with pytest.raises((httpx.HTTPError, ValueError)):
            await providers.read_offer("http://mac.invalid")
        assert len(seen) == 1
