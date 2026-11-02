"""Run in the API container; isolated schema, fake charges, no provider calls.

Usage: python tests/video_store_proof.py --source-root /path/to/checkout
The named source tree supplies the migration and module under test. Each run creates
and removes only its own uniquely named schema, leaving application tables untouched.
"""

from __future__ import annotations

import argparse
import asyncio
import importlib.util
import os
from decimal import Decimal
from pathlib import Path
from uuid import uuid4

import psycopg
from psycopg import sql
from psycopg.conninfo import make_conninfo


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("--source-root", type=Path, required=True)
    args = parser.parse_args()
    api = args.source_root / "src/homelab/api"
    spec = importlib.util.spec_from_file_location("video_jobs", api / "video_jobs.py")
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    database_url = os.environ["HOMELAB_DATABASE_URL"]
    schema = "hm2_test_" + uuid4().hex
    with psycopg.connect(database_url) as connection:
        connection.execute(sql.SQL("CREATE SCHEMA {}").format(sql.Identifier(schema)))
    try:
        os.environ["HOMELAB_DATABASE_URL"] = make_conninfo(
            database_url, options="-c search_path=" + schema
        )
        with psycopg.connect(os.environ["HOMELAB_DATABASE_URL"]) as connection:
            connection.execute((api / "image_costs.sql").read_text())
            connection.execute((api / "video_jobs.sql").read_text())

        async def prove():
            store = module.PostgresStore()
            for terminal in ("completed", "failed"):
                job_id = str(uuid4())
                await store.create(job_id, "a" * 64, "image_to_video", "fixture-model")
                await asyncio.gather(
                    *(
                        store.update(job_id, status=terminal, cost=Decimal("0.02"))
                        for _ in range(20)
                    )
                )
                assert (await store.get(job_id, "a" * 64))["cost_usd"] == Decimal("0.02")
                assert await store.get(job_id, "b" * 64) is None

        asyncio.run(prove())
        with psycopg.connect(os.environ["HOMELAB_DATABASE_URL"]) as connection:
            row = connection.execute(
                "SELECT total_usd, charged_calls FROM homelab_image_costs"
            ).fetchone()
            assert row == (Decimal("0.04"), 2), row
        print(
            "Postgres proof: 40 concurrent terminal updates; USD 0.04; "
            "charged_calls 2; key isolation passed"
        )
    finally:
        with psycopg.connect(database_url) as connection:
            connection.execute(sql.SQL("DROP SCHEMA {} CASCADE").format(sql.Identifier(schema)))
        print("Isolated test schema removed; application rows unchanged")


if __name__ == "__main__":
    main()
