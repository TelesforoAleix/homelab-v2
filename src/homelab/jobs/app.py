"""Durable jobs (Procrastinate on Postgres).

A job is persisted before it is acknowledged and survives restarts. Tasks that touch
llama-server go on the `local` queue, which the worker runs with concurrency 1: one
local-model job at a time on a four-core node. Tasks are idempotent or declare
`retry=0`; the worker may re-run a task that was in flight when the node died.

Run the worker:   procrastinate --app homelab.jobs.app.app worker --queues local,default
Apply the schema: procrastinate --app homelab.jobs.app.app schema --apply
"""

from __future__ import annotations

import procrastinate

from homelab.settings import get_settings

app = procrastinate.App(
    connector=procrastinate.PsycopgConnector(conninfo=get_settings().database_url),
)


@app.task(queue="default", name="ping")
def ping(message: str = "pong") -> str:
    """The smallest task: proves the queue, the schema and the worker."""
    return message
