"""Read eval attempt counts and mean usage from Postgres, without opening run content."""

from __future__ import annotations

from typing import Any

import psycopg

from homelab.settings import get_settings


def results(connection: Any, name: str) -> list[tuple]:
    # Collapse traces before grouping attempts: reviews must not multiply the run count.
    return connection.execute(
        """WITH usage AS (
               SELECT task_id, attempt, sum(steps) AS steps,
                      sum(fresh_tokens) AS fresh, sum(cached_tokens) AS cached,
                      sum(output_tokens) AS output,
                      CASE WHEN count(cost_usd) = count(*) THEN sum(cost_usd) END AS cost
               FROM homelab_task_traces GROUP BY task_id, attempt
           )
           SELECT task.eval_case, attempt.configuration_hash, count(*) AS runs,
                  count(*) FILTER (WHERE attempt.status = 'done'
                                     AND attempt.reason = 'eval_kept') AS passed,
                  avg(usage.steps), avg(usage.fresh), avg(usage.cached), avg(usage.output),
                  avg(extract(epoch FROM attempt.ended_at - attempt.started_at)),
                  CASE WHEN count(usage.cost) = count(*) THEN avg(usage.cost) END
           FROM homelab_tasks task
           JOIN homelab_task_attempts attempt ON attempt.task_id = task.id
           LEFT JOIN usage ON usage.task_id = attempt.task_id AND usage.attempt = attempt.number
           WHERE task.eval_set = %s AND attempt.ended_at IS NOT NULL
           GROUP BY task.eval_case, attempt.configuration_hash
           ORDER BY task.eval_case, attempt.configuration_hash""",
        (name,),
    ).fetchall()


def print_results(name: str) -> int:
    with psycopg.connect(get_settings().database_url) as connection:
        rows = results(connection, name)
    for row in rows:
        print("\t".join("" if value is None else str(value) for value in row))
    return 0
