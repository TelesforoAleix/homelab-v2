"""Runtime configuration. Every value comes from the environment (HOMELAB_*) or a Compose secret."""

from __future__ import annotations

from functools import lru_cache
from pathlib import Path
from typing import Annotated, Literal

from pydantic import Field, SecretStr, field_validator
from pydantic_settings import BaseSettings, NoDecode, SettingsConfigDict


class Settings(BaseSettings):
    model_config = SettingsConfigDict(env_prefix="HOMELAB_", env_file=".env", extra="ignore")

    database_url: str = "postgresql://homelab:homelab@localhost:5432/homelab"
    active_collection: Literal["brain", "about_aleix"] = "about_aleix"
    corpus_file: Path = Path("/data/corpus/about-aleix/corpus.json")
    eval_file: Path = Path("/data/corpus/about-aleix/eval.json")
    brain_dir: Path = Path("/data/brain")
    brain_include: Annotated[list[str], NoDecode] = ["01-knowledge", "02-ideas", "05-logs"]

    models_base_url: str = "http://litellm:4000/v1"
    # Accepted and ignored by the proxy today; kept in the client contract.
    models_api_key: SecretStr = SecretStr("homelab")
    log_level: Literal["DEBUG", "INFO", "WARNING", "ERROR", "CRITICAL"] = "INFO"

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


@lru_cache
def get_settings() -> Settings:
    return Settings()
