import httpx
import pytest

from homelab.models.routes import UnknownRoute, load_routes
from homelab.settings import Settings


@pytest.fixture
def model_endpoint(monkeypatch):
    purposes = ["chat", "chat:high", "chat:xhigh", "embed"]
    requests = []

    def models(url, **kwargs):
        requests.append((url, kwargs))
        return httpx.Response(
            200,
            json={"data": [{"id": purpose} for purpose in purposes]},
            request=httpx.Request("GET", url),
        )

    monkeypatch.setattr("homelab.models.routes.httpx.get", models)
    return purposes, requests


def test_routes_discover_purposes_from_the_endpoint(model_endpoint):
    purposes, requests = model_endpoint
    settings = Settings(
        models_base_url="http://proxy:4000/v1/", models_api_key="client-key", _env_file=None
    )
    table = load_routes(settings)
    assert table.purposes() == purposes
    chat = table.resolve("chat")
    assert chat.purpose == "chat"
    assert chat.base_url == "http://proxy:4000/v1"
    assert chat.api_key == "client-key"
    assert all(url == "http://proxy:4000/v1/models" for url, _ in requests)
    assert requests[0][1]["headers"] == {"Authorization": "Bearer client-key"}
    assert "client-key" not in repr(settings)


@pytest.mark.parametrize("purpose", ["summarise", "vision", "embed:high", "openai/gpt-5.6-luna"])
def test_unknown_purpose_is_refused_not_defaulted(model_endpoint, purpose):
    table = load_routes(Settings(_env_file=None))
    with pytest.raises(UnknownRoute):
        table.resolve(purpose)


def test_new_purposes_are_discovered_without_an_application_route_table(model_endpoint):
    purposes, _ = model_endpoint
    table = load_routes(Settings(_env_file=None))
    purposes.append("summarise")
    assert table.resolve("summarise").purpose == "summarise"


def test_endpoint_failure_never_defaults(monkeypatch):
    def unavailable(url, **kwargs):
        return httpx.Response(503, request=httpx.Request("GET", url))

    monkeypatch.setattr("homelab.models.routes.httpx.get", unavailable)
    with pytest.raises(httpx.HTTPStatusError):
        load_routes(Settings(_env_file=None)).resolve("chat")


def test_framework_clients_send_purposes_to_litellm(model_endpoint):
    table = load_routes(Settings(_env_file=None))
    emb = table.embedding_model("embed")
    assert emb.model_name == "embed"
    assert emb.api_base == "http://litellm:4000/v1"
    for purpose in ["chat", "chat:high", "chat:xhigh"]:
        llm = table.llm(purpose, timeout=60, max_retries=0)
        assert llm.model == purpose
        assert llm.api_base == emb.api_base
        assert llm.timeout == 60
        assert llm.max_retries == 0
