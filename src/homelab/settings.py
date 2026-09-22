"""Runtime configuration. Every value comes from the environment (HOMELAB_*) or a Compose secret."""

from __future__ import annotations

from functools import lru_cache
from pathlib import Path
from typing import Annotated

from pydantic import Field, field_validator
from pydantic_settings import BaseSettings, NoDecode, SettingsConfigDict


class Settings(BaseSettings):
    model_config = SettingsConfigDict(env_prefix="HOMELAB_", env_file=".env", extra="ignore")

    database_url: str = "postgresql://homelab:homelab@localhost:5432/homelab"
    routes_file: Path = Path("config/routes.yaml")
    brain_dir: Path = Path("/data/brain")
    brain_include: Annotated[list[str], NoDecode] = ["01-knowledge", "02-ideas", "05-logs"]

    # The gateway key is read from a file (a Compose secret) when one is given,
    # otherwise from HOMELAB_GATEWAY_API_KEY. It never appears in config files.
    gateway_api_key: str | None = None
    gateway_api_key_file: Path | None = None
    gateway_base_url: str = "https://ai-gateway.vercel.sh/v1"

    # llama-server on the node (OpenAI-compatible). No key.
    local_base_url: str = "http://localhost:8080/v1"

    log_content: bool = Field(
        default=False,
        description="Log prompts and retrieved text. Off by default; "
        "ids, sizes and timings are always logged.",
    )

    @field_validator("brain_include", mode="before")
    @classmethod
    def split_brain_include(cls, value):
        if isinstance(value, str):
            return [item.strip() for item in value.split(",") if item.strip()]
        return value

    def resolve_gateway_api_key(self) -> str | None:
        if self.gateway_api_key_file and self.gateway_api_key_file.exists():
            return self.gateway_api_key_file.read_text().strip()
        return self.gateway_api_key


@lru_cache
def get_settings() -> Settings:
    return Settings()
