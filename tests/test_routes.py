from pathlib import Path

import pytest

from homelab.models.routes import UnknownRoute, load_routes
from homelab.settings import Settings

ROUTES = """
providers:
  gateway: {}
  local:
    base_url: http://llama:8080/v1
routes:
  chat: {provider: gateway, model: some/chat-model}
  embed: {provider: local, model: some-embedding}
"""


@pytest.fixture
def routes_file(tmp_path: Path) -> Path:
    path = tmp_path / "routes.yaml"
    path.write_text(ROUTES)
    return path


def test_routes_resolve_provider_base_url_and_key(routes_file: Path, tmp_path: Path):
    key_file = tmp_path / "key"
    key_file.write_text("sk-test\n")
    settings = Settings(gateway_api_key_file=key_file, _env_file=None)
    table = load_routes(routes_file, settings)

    chat = table.resolve("chat")
    assert chat.provider == "gateway"
    assert chat.model == "some/chat-model"
    assert chat.base_url == settings.gateway_base_url
    assert chat.api_key == "sk-test"

    embed = table.resolve("embed")
    assert embed.is_local
    assert embed.base_url == "http://llama:8080/v1"
    assert embed.api_key is None


def test_unknown_purpose_is_refused_not_defaulted(routes_file: Path):
    table = load_routes(routes_file, Settings(_env_file=None))
    with pytest.raises(UnknownRoute):
        table.resolve("summarise")


def test_route_naming_unknown_provider_fails_at_load(tmp_path: Path):
    path = tmp_path / "routes.yaml"
    path.write_text("providers: {local: {}}\nroutes: {chat: {provider: gpu, model: x}}\n")
    with pytest.raises(ValueError, match="unknown provider"):
        load_routes(path, Settings(_env_file=None))


def test_framework_clients_point_at_the_route(routes_file: Path):
    table = load_routes(routes_file, Settings(_env_file=None))
    emb = table.embedding_model("embed")
    assert emb.model_name == "some-embedding"
    assert emb.api_base == "http://llama:8080/v1"
    llm = table.llm("chat")
    assert llm.model == "some/chat-model"
