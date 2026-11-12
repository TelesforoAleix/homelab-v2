"""Read-only management projections of the deployed route table."""

from __future__ import annotations

import hashlib
import threading
import time
from pathlib import Path

import httpx
import yaml

from homelab.manage.catalogue import MEDIA_ORIGIN, MEDIA_PROVIDER, UNKNOWN, Provider, identify
from homelab.settings import Settings, get_settings

TABLE = Path(__file__).resolve().parents[3] / "config" / "litellm.yaml"


class ProviderStates:
    def __init__(self) -> None:
        self._cache: dict[str, tuple[float, str]] = {}
        self._lock = threading.Lock()

    def state(self, provider: Provider, settings: Settings) -> str:
        if provider == UNKNOWN:
            return "down"
        if provider.kind == "hosted":
            return "configured"
        base = getattr(settings, provider.setting) if provider.setting else provider.origin
        with self._lock:
            now = time.monotonic()
            cached = self._cache.get(base)
            if cached and now < cached[0]:
                return cached[1]
            try:
                response = httpx.get(f"{base.rstrip('/')}/models", timeout=2.0)
                response.raise_for_status()
                state = "up"
            except httpx.HTTPError:
                state = "down"
            self._cache[base] = (time.monotonic() + 30, state)
            return state


states = ProviderStates()


def documents(path: Path = TABLE, settings: Settings | None = None) -> tuple[dict, dict]:
    settings = settings or get_settings()
    raw = path.read_bytes()
    table = yaml.safe_load(raw)
    version = hashlib.sha256(raw).hexdigest()
    purposes: dict[str, list[dict]] = {}
    providers: dict[str, dict] = {}

    def add(purpose: str, model: str, provider: Provider, enabled: bool) -> None:
        if provider.name not in providers:
            providers[provider.name] = {
                "name": provider.name,
                "kind": provider.kind,
                "state": states.state(provider, settings),
                "purposes": [],
            }
        record = providers[provider.name]
        if purpose not in record["purposes"]:
            record["purposes"].append(purpose)
        purposes.setdefault(purpose, []).append(
            {"provider": provider.name, "model": model, "enabled": enabled}
        )

    for row in table["model_list"]:
        params = row["litellm_params"]
        add(row["model_name"], params["model"], identify(params.get("api_base", "")), True)
    for row in table.get("media_model_list", []):
        # Native media routes use the configured media gateway, not litellm_params.
        provider = MEDIA_PROVIDER if table.get("media_gateway_url") == MEDIA_ORIGIN else UNKNOWN
        add(row["model_name"], row["gateway_model"], provider, True)

    routes = []
    for purpose, entries in purposes.items():
        live = next((entry for entry in entries if entry["enabled"]), None)
        routes.append(
            {
                "purpose": purpose,
                "routes": entries,
                "live": live,
                "health": providers[live["provider"]]["state"] if live else "down",
            }
        )
    return {"version": version, "routes": routes}, {
        "version": version,
        "providers": list(providers.values()),
    }
