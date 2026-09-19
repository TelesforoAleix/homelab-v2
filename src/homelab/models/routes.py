"""The route table: purpose -> provider + model.

`config/routes.yaml` names providers (an OpenAI-compatible base URL each) and routes
(a purpose such as `chat` or `embed`, bound to one provider and one model). Nothing
above this module names a provider or a model; changing either is an edit to the
YAML file. The `embed` route is special only by convention: the index is built with
it, so changing it means reindexing.
"""

from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path

import yaml

from homelab.settings import Settings, get_settings


class UnknownRoute(KeyError):
    """A purpose that the route table does not define. Never defaulted."""


@dataclass(frozen=True)
class Route:
    purpose: str
    provider: str
    model: str
    base_url: str
    api_key: str | None

    @property
    def is_local(self) -> bool:
        return self.provider == "local"


class RouteTable:
    def __init__(self, routes: dict[str, Route]):
        self._routes = routes

    def resolve(self, purpose: str) -> Route:
        try:
            return self._routes[purpose]
        except KeyError:
            raise UnknownRoute(purpose) from None

    def purposes(self) -> list[str]:
        return sorted(self._routes)

    # --- framework clients -------------------------------------------------
    # Built here so that no other module needs to know which provider a purpose
    # maps to. Both are OpenAI-compatible clients pointed at the route's base URL.

    def embedding_model(self, purpose: str = "embed"):
        from llama_index.embeddings.openai_like import OpenAILikeEmbedding

        route = self.resolve(purpose)
        return OpenAILikeEmbedding(
            model_name=route.model,
            api_base=route.base_url,
            api_key=route.api_key or "none",
        )

    def llm(self, purpose: str = "chat", **kwargs):
        from llama_index.llms.openai_like import OpenAILike

        route = self.resolve(purpose)
        return OpenAILike(
            model=route.model,
            api_base=route.base_url,
            api_key=route.api_key or "none",
            is_chat_model=True,
            **kwargs,
        )


def load_routes(path: Path | None = None, settings: Settings | None = None) -> RouteTable:
    settings = settings or get_settings()
    path = path or settings.routes_file
    data = yaml.safe_load(Path(path).read_text()) or {}

    providers: dict[str, tuple[str, str | None]] = {}
    for name, spec in (data.get("providers") or {}).items():
        if name == "gateway":
            providers[name] = (
                spec.get("base_url") or settings.gateway_base_url,
                settings.resolve_gateway_api_key(),
            )
        elif name == "local":
            providers[name] = (spec.get("base_url") or settings.local_base_url, None)
        else:
            # A future provider (a GPU node, a rented box): base_url in the YAML,
            # key from HOMELAB_<NAME>_API_KEY if the provider needs one.
            import os

            providers[name] = (spec["base_url"], os.environ.get(f"HOMELAB_{name.upper()}_API_KEY"))

    routes: dict[str, Route] = {}
    for purpose, spec in (data.get("routes") or {}).items():
        provider = spec["provider"]
        if provider not in providers:
            raise ValueError(f"route {purpose!r} names unknown provider {provider!r}")
        base_url, api_key = providers[provider]
        routes[purpose] = Route(
            purpose=purpose,
            provider=provider,
            model=spec["model"],
            base_url=base_url,
            api_key=api_key,
        )
    return RouteTable(routes)
