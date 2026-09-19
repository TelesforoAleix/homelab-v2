"""The Home Lab API. Loopback-only in slice 1; clients on this node call it at 127.0.0.1:8000."""

from __future__ import annotations

from fastapi import FastAPI

from homelab import __version__
from homelab.models import load_routes
from homelab.settings import get_settings

app = FastAPI(title="Home Lab", version=__version__)


@app.get("/health")
def health() -> dict:
    settings = get_settings()
    routes = load_routes(settings=settings)
    return {
        "status": "ok",
        "version": __version__,
        "routes": routes.purposes(),
        "gateway_key_present": settings.resolve_gateway_api_key() is not None,
    }
