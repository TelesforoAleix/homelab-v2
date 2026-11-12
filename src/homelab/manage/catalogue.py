"""Known route origins. Addresses are internal and never returned by the API."""

from dataclasses import dataclass
from typing import Literal


@dataclass(frozen=True)
class Provider:
    name: str
    kind: Literal["hosted", "local"]
    origin: str
    setting: str | None = None


PROVIDERS = (
    Provider("ai-gateway", "hosted", "https://ai-gateway.vercel.sh/v1"),
    Provider("llama-embed", "local", "http://llama-embed:8080/v1"),
    Provider("llama-embed-large", "local", "http://llama-embed-large:8080/v1"),
    Provider("mac-vision", "local", "os.environ/HOMELAB_MAC_VISION_URL", "mac_vision_url"),
    Provider("mac-embed", "local", "os.environ/HOMELAB_MAC_EMBED_URL", "mac_embed_url"),
)
# Native media routes use the same hosted provider through its v4 endpoint.
MEDIA_ORIGIN = "https://ai-gateway.vercel.sh/v4/ai"
MEDIA_PROVIDER = PROVIDERS[0]
UNKNOWN = Provider("unknown", "local", "")


def identify(origin: str) -> Provider:
    return next((provider for provider in PROVIDERS if provider.origin == origin), UNKNOWN)
