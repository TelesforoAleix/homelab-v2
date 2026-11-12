import json
import re

import httpx
import pytest
import yaml
from fastapi.testclient import TestClient

from homelab.api.app import app
from homelab.manage import TABLE, ProviderStates, documents
from homelab.manage.catalogue import PROVIDERS
from homelab.settings import Settings


def test_documents_match_table_and_disclose_no_addresses_or_keys(monkeypatch):
    monkeypatch.setattr(
        "homelab.manage.states.state",
        lambda provider, settings: "configured" if provider.kind == "hosted" else "up",
    )
    client = TestClient(app)
    routes = client.get("/v1/manage/routes").json()
    providers = client.get("/v1/manage/providers").json()
    table = yaml.safe_load(TABLE.read_bytes())
    names = [row["model_name"] for row in table["model_list"] + table["media_model_list"]]
    assert [row["purpose"] for row in routes["routes"]] == list(dict.fromkeys(names))
    assert routes["version"] == providers["version"]
    assert len(routes["version"]) == 64
    assert {"vision-*", "embed-large-bulk-*"} <= set(names)
    assert all(row["live"] in row["routes"] for row in routes["routes"])
    assert all(row["health"] in {"up", "configured"} for row in routes["routes"])
    assert all(set(row) == {"purpose", "routes", "live", "health"} for row in routes["routes"])
    assert all(
        set(route) == {"provider", "model", "enabled"}
        for row in routes["routes"]
        for route in row["routes"]
    )
    assert all(set(row) == {"name", "kind", "state", "purposes"} for row in providers["providers"])
    assert "unknown" not in {row["name"] for row in providers["providers"]}
    serialised = json.dumps([routes, providers])
    assert not re.search(
        r"api_base|https?://|\b(?:\d{1,3}\.){3}\d{1,3}\b|api_key|HOMELAB_", serialised, re.I
    )


def test_version_changes_and_duplicate_purposes_keep_order(tmp_path, monkeypatch):
    monkeypatch.setattr("homelab.manage.states.state", lambda *args: "up")
    path = tmp_path / "routes.yaml"
    table = yaml.safe_load(TABLE.read_bytes())
    path.write_text(yaml.safe_dump(table))
    original, _ = documents(path)
    duplicate = dict(table["model_list"][0])
    duplicate["litellm_params"] = dict(duplicate["litellm_params"], model="second")
    table["model_list"].append(duplicate)
    path.write_text(yaml.safe_dump(table))
    changed, _ = documents(path)
    assert changed["version"] != original["version"]
    assert changed["routes"][0]["routes"][1]["model"] == "second"
    assert changed["routes"][0]["live"] == changed["routes"][0]["routes"][0]


@pytest.mark.parametrize("outcome,expected", [(200, "up"), (503, "down"), ("timeout", "down")])
def test_local_probe_and_cache(monkeypatch, outcome, expected):
    calls = []
    now = [10.0]
    monkeypatch.setattr("homelab.manage.time.monotonic", lambda: now[0])

    def get(url, timeout):
        calls.append((url, timeout))
        if outcome == "timeout":
            raise httpx.TimeoutException("timeout")
        return httpx.Response(outcome, request=httpx.Request("GET", url))

    monkeypatch.setattr("homelab.manage.httpx.get", get)
    cache = ProviderStates()
    provider = PROVIDERS[1]
    assert cache.state(provider, Settings()) == expected
    assert cache.state(provider, Settings()) == expected
    assert len(calls) == 1 and calls[0][1] == 2.0
    assert calls[0][0].endswith("/v1/models")
    now[0] += 30
    assert cache.state(provider, Settings()) == expected
    assert len(calls) == 2


def test_hosted_never_probed(monkeypatch):
    def fail(*args, **kwargs):
        pytest.fail("hosted provider was probed")

    monkeypatch.setattr("homelab.manage.httpx.get", fail)
    assert ProviderStates().state(PROVIDERS[0], Settings()) == "configured"
