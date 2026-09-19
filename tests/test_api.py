from fastapi.testclient import TestClient

from homelab.api.app import app
from homelab.settings import Settings, get_settings


def test_health_reports_routes_and_no_key(tmp_path, monkeypatch):
    routes = tmp_path / "routes.yaml"
    routes.write_text(
        "providers: {gateway: {}, local: {}}\nroutes: {chat: {provider: gateway, model: m}}\n"
    )
    get_settings.cache_clear()
    app.dependency_overrides.clear()
    monkeypatch.setenv("HOMELAB_ROUTES_FILE", str(routes))
    monkeypatch.delenv("HOMELAB_GATEWAY_API_KEY", raising=False)
    monkeypatch.setattr("homelab.api.app.get_settings", lambda: Settings(_env_file=None))

    body = TestClient(app).get("/health").json()
    assert body["status"] == "ok"
    assert body["routes"] == ["chat"]
    assert body["gateway_key_present"] is False
