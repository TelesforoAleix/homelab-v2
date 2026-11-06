"""Report a slice's tasks as JSON: `python -m homelab.tasks.status <slice>`."""

from __future__ import annotations

import argparse
import json
import sys
from typing import Any

import psycopg

from homelab.settings import get_settings


def status(connection: Any, slice_name: str) -> list[dict[str, Any]]:
    rows = connection.execute(
        """SELECT id, position, title, status, reason FROM homelab_tasks
           WHERE slice = %s ORDER BY position""",
        (slice_name,),
    ).fetchall()
    return [
        dict(zip(("id", "position", "title", "status", "reason"), row, strict=True)) for row in rows
    ]


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("slice", help="slice to report")
    args = parser.parse_args()
    try:
        with psycopg.connect(get_settings().database_url) as connection:
            tasks = status(connection, args.slice)
    except psycopg.Error:
        print("homelab.tasks.status: database failure", file=sys.stderr)
        return 1
    print(json.dumps(tasks))
    return 0


if __name__ == "__main__":
    sys.exit(main())
