"""Read the Mac's fixed public offer. This module has no Mac control operations."""

from __future__ import annotations

import asyncio
import json
import logging
import os
from datetime import datetime
from typing import Annotated, Literal

import httpx
import psycopg
from psycopg.types.json import Jsonb
from pydantic import BaseModel, ConfigDict, Field, StringConstraints

from homelab.settings import get_settings

logger = logging.getLogger(__name__)
# HTTPX otherwise emits complete URLs at INFO in the worker's default logs.
logging.getLogger("httpx").setLevel(logging.WARNING)
Identifier = Annotated[str, StringConstraints(pattern=r"^[A-Za-z0-9][A-Za-z0-9_.-]{0,191}$")]
Count = Annotated[int, Field(strict=True, ge=0)]


class PublicFields(BaseModel):
    model_config = ConfigDict(extra="forbid", strict=True, allow_inf_nan=False)


class Model(PublicFields):
    id: Identifier
    kind: Literal["chat", "embedding", "vision"]
    state: Literal["loaded", "on request", "off"]
    context: Annotated[int, Field(strict=True, gt=0)] | None


class Queue(PublicFields):
    playing: Annotated[int, Field(strict=True, ge=0, le=1)]
    running: Count
    waiting: Count


class NightWindow(PublicFields):
    start: Annotated[str, StringConstraints(pattern=r"^(?:[01]\d|2[0-3]):[0-5]\d$")]
    end: Annotated[str, StringConstraints(pattern=r"^(?:[01]\d|2[0-3]):[0-5]\d$")]


class Offer(PublicFields):
    at: datetime
    profile: Literal["working", "inference"]
    power: Literal["ac", "battery"]
    watts: Annotated[float, Field(ge=0)] | None = None
    paused: bool
    models: list[Model]
    memory_free_mb: Annotated[float, Field(ge=0)] | None
    queue: Queue
    night_window: NightWindow | None


async def read_offer(base: str) -> dict:
    # This is a total deadline, including a slow trickle of response bytes. HTTPX's
    # per-I/O timeout alone would not bound the whole read. Never follow redirects.
    async with asyncio.timeout(3):
        async with httpx.AsyncClient(timeout=3, trust_env=False, follow_redirects=False) as client:
            response = await client.get(
                base.removesuffix("/").removesuffix("/v1") + "/homelab-offer.json"
            )
            response.raise_for_status()
            Offer.model_validate_json(response.content)
            return json.loads(response.content)


# Both polling and CLI use the database clock, independently of the offer's Mac clock.
FRESHNESS = """CASE WHEN seen_at > clock_timestamp() - interval '90 seconds'
               THEN 'fresh' ELSE 'away' END"""


def save_observation(connection, offer: dict | None) -> None:
    if offer is not None:
        connection.execute(
            """INSERT INTO homelab_providers (provider, offer, seen_at, state)
               VALUES ('mac', %s, clock_timestamp(), 'fresh')
               ON CONFLICT (provider) DO UPDATE
               SET offer = EXCLUDED.offer, seen_at = EXCLUDED.seen_at, state = 'fresh'""",
            (Jsonb(offer),),
        )
    else:
        connection.execute(
            """INSERT INTO homelab_providers (provider, offer, seen_at, state)
               VALUES ('mac', NULL, NULL, 'away') ON CONFLICT (provider) DO NOTHING"""
        )
    connection.execute(f"UPDATE homelab_providers SET state = {FRESHNESS}")


def provider_rows(connection) -> list[dict]:
    # Recompute when displaying too: an unavailable worker must not leave 'fresh' forever.
    rows = connection.execute(
        f"SELECT provider, offer, seen_at, {FRESHNESS} FROM homelab_providers ORDER BY provider"
    ).fetchall()
    return [dict(zip(("provider", "offer", "seen_at", "state"), row, strict=True)) for row in rows]


async def poll() -> None:
    base = os.environ.get("HOMELAB_MAC_VISION_URL", "")
    offer = None
    reason = "unconfigured"
    if base:
        try:
            offer = await read_offer(base.rstrip("/"))
            reason = "read"
        except (httpx.HTTPError, TimeoutError, ValueError):
            reason = "unavailable"
    with psycopg.connect(get_settings().database_url) as connection:
        save_observation(connection, offer)
    logger.info("provider=mac reason=%s", reason)
