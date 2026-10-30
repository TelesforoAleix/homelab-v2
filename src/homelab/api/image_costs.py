"""Content-free image accounting and host-driven budget notifications."""

from __future__ import annotations

import hashlib
import json
import os
import sys
from datetime import UTC, datetime
from decimal import Decimal
from pathlib import Path

import psycopg


def connect():
    return psycopg.connect(os.environ["HOMELAB_DATABASE_URL"], connect_timeout=5)


def month_at(now=None):
    return (now or datetime.now(UTC)).astimezone(UTC).date().replace(day=1)


def key_id(key):
    return hashlib.sha256(key.encode()).hexdigest()


def migrate():
    with connect() as connection:
        connection.execute(Path(__file__).with_name("image_costs.sql").read_text())


def total_for(key, now=None):
    month = month_at(now)
    with connect() as connection:
        row = connection.execute(
            """SELECT total_usd, charged_calls FROM homelab_image_costs
            WHERE client_key_hash = %s AND month = %s""",
            (key_id(key), month),
        ).fetchone()
    return {
        "month": month.strftime("%Y-%m"),
        "total_usd": format(row[0], "f") if row else "0",
        "charged_calls": row[1] if row else 0,
    }


def current_total(key):
    return total_for(key)


def send_alerts(send, now=None):
    month = month_at(now)
    budget = Decimal(os.environ["HOMELAB_IMAGE_MONTHLY_BUDGET_USD"])
    if not budget.is_finite() or budget <= 0:
        raise ValueError("Monthly image budget must be positive")
    for threshold in (80, 100):
        with connect() as connection:
            # The monthly sentinel keeps delivery flags in the same table. Lock and
            # re-read for each threshold, including when several senders overlap.
            connection.execute(
                """INSERT INTO homelab_image_costs (client_key_hash, month)
                VALUES ('', %s) ON CONFLICT DO NOTHING""",
                (month,),
            )
            already_sent = connection.execute(
                f"SELECT sent_{threshold} FROM homelab_image_costs "
                "WHERE client_key_hash = '' AND month = %s FOR UPDATE",
                (month,),
            ).fetchone()[0]
            total = connection.execute(
                """SELECT COALESCE(SUM(total_usd), 0) FROM homelab_image_costs
                WHERE month = %s AND client_key_hash <> ''""",
                (month,),
            ).fetchone()[0]
            if not already_sent and total >= budget * threshold / 100:
                send(
                    f"Home Lab image budget: {threshold}% reached for {month:%Y-%m}; "
                    f"recorded USD {total:f} of {budget:f}. Resets on the 1st at 00:00 UTC."
                )
                connection.execute(
                    f"UPDATE homelab_image_costs SET sent_{threshold} = true "
                    "WHERE client_key_hash = '' AND month = %s",
                    (month,),
                )


def main():
    if sys.argv[1:] == ["alerts"]:

        def send(message):
            print(json.dumps({"message": message}), flush=True)
            if sys.stdin.readline().strip() != "sent":
                raise OSError("Host notifier did not confirm delivery")

        send_alerts(send)
        print(json.dumps({"done": True}), flush=True)
    elif sys.argv[1:] == ["migrate"]:
        migrate()
    else:
        raise SystemExit(2)


if __name__ == "__main__":
    main()
