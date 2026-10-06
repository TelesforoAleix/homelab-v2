"""Purpose clients for LiteLLM; its /models endpoint owns the route table."""

from __future__ import annotations

from dataclasses import dataclass, field

import httpx

from homelab.settings import Settings, get_settings


class UnknownRoute(KeyError):
    """A purpose that LiteLLM does not offer. Never defaulted."""


@dataclass(frozen=True)
class Route:
    purpose: str
    base_url: str
    api_key: str = field(repr=False)


class RouteTable:
    def __init__(self, base_url: str, api_key: str):
        self.base_url = base_url.rstrip("/")
        self.api_key = api_key

    def purposes(self) -> list[str]:
        response = httpx.get(
            f"{self.base_url}/models",
            headers={"Authorization": f"Bearer {self.api_key}"},
            timeout=5,
        )
        response.raise_for_status()
        return sorted(item["id"] for item in response.json()["data"])

    def resolve(self, purpose: str) -> Route:
        if purpose not in self.purposes():
            raise UnknownRoute(purpose)
        return Route(purpose=purpose, base_url=self.base_url, api_key=self.api_key)

    def embedding_model(self, purpose: str = "embed"):
        from llama_index.embeddings.openai_like import OpenAILikeEmbedding

        route = self.resolve(purpose)
        return OpenAILikeEmbedding(
            model_name=route.purpose, api_base=route.base_url, api_key=route.api_key
        )

    def llm(self, purpose: str = "chat", **kwargs):
        from llama_index.llms.openai_like import OpenAILike

        route = self.resolve(purpose)
        return OpenAILike(
            model=route.purpose,
            api_base=route.base_url,
            api_key=route.api_key,
            is_chat_model=True,
            **kwargs,
        )


def load_routes(settings: Settings | None = None) -> RouteTable:
    settings = settings or get_settings()
    return RouteTable(settings.models_base_url, settings.models_api_key.get_secret_value())
