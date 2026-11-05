"""Idempotently ensure that Procrastinate's schema and the task tables exist."""

from __future__ import annotations

import logging
from typing import Any

import psycopg

from homelab.jobs.app import app
from homelab.settings import get_settings
from homelab.tasks.store import apply_schema

logger = logging.getLogger(__name__)


def ensure_schema(jobs_app: Any = app) -> bool:
    """Apply the schema only when Procrastinate reports that it is absent."""
    with jobs_app.open():
        if jobs_app.check_connection():
            return False
        jobs_app.schema_manager.apply_schema()
        return True


def ensure_task_tables() -> None:
    with psycopg.connect(get_settings().database_url) as connection:
        apply_schema(connection)


def main() -> None:
    logging.basicConfig(level=logging.INFO)
    applied = ensure_schema()
    logger.info("Procrastinate schema ready applied=%s", applied)
    ensure_task_tables()
    logger.info("Task tables ready")


if __name__ == "__main__":
    main()
