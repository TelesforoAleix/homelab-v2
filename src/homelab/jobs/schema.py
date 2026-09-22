"""Idempotently ensure that Procrastinate's library-owned schema exists."""

from __future__ import annotations

import logging
from typing import Any

from homelab.jobs.app import app

logger = logging.getLogger(__name__)


def ensure_schema(jobs_app: Any = app) -> bool:
    """Apply the schema only when Procrastinate reports that it is absent."""
    with jobs_app.open():
        if jobs_app.check_connection():
            return False
        jobs_app.schema_manager.apply_schema()
        return True


def main() -> None:
    logging.basicConfig(level=logging.INFO)
    applied = ensure_schema()
    logger.info("Procrastinate schema ready applied=%s", applied)


if __name__ == "__main__":
    main()
