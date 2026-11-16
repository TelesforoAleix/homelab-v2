"""Exercise the API submission with the real queue and an asynchronously opened app."""

import asyncio
import json
from types import SimpleNamespace

import procrastinate
import pytest
from psycopg_pool import AsyncConnectionPool

from homelab.api import eval_tasks, runs
from homelab.jobs.app import app as jobs_app
from tests.test_eval_tasks import body
from tests.test_task_store import connection  # noqa: F401


def test_submit_with_open_job_app_and_rollback(connection, monkeypatch):  # noqa: F811
    # Procrastinate retains an externally supplied pool after close_async. Keep this
    # test's pool local to the test rather than leaking it into later CLI checks.
    monkeypatch.setattr(jobs_app.connector, "_async_pool", None)
    monkeypatch.setattr(jobs_app.connector, "_pool_externally_set", False)
    url = connection.info.dsn
    monkeypatch.setattr(runs, "get_settings", lambda: SimpleNamespace(database_url=url))
    monkeypatch.setattr(runs, "repository_default", lambda application, repository: "main")
    plan = eval_tasks.parse_submission(json.dumps(body()).encode())
    failed_plan = eval_tasks.parse_submission(json.dumps(body(attempt="case-2")).encode())

    async def exercise():
        async with AsyncConnectionPool(url, open=False) as pool:
            async with jobs_app.open_async(pool):
                response = await asyncio.to_thread(eval_tasks.create_task, plan)
                assert response.status_code == 201
                task_id = json.loads(response.body)["task"]
                assert connection.execute(
                    "SELECT slice, eval_set, eval_case, eval_configuration "
                    "FROM homelab_tasks WHERE id = %s",
                    (task_id,),
                ).fetchall() == [("eval:factory:case-1", "T", "docs", "base")]
                assert connection.execute(
                    "SELECT task_name, queue_name, args FROM procrastinate_jobs"
                ).fetchall() == [("run_coding_task", "tasks", {"task_id": task_id})]
                connection.execute("""
                    CREATE FUNCTION reject_eval_job() RETURNS trigger LANGUAGE plpgsql AS $$
                    BEGIN RAISE EXCEPTION 'fixture defer failure'; END $$
                """)
                connection.execute("""
                    CREATE TRIGGER reject_eval_job BEFORE INSERT ON procrastinate_jobs
                    FOR EACH ROW EXECUTE FUNCTION reject_eval_job()
                """)
                with pytest.raises(procrastinate.exceptions.ConnectorException):
                    await asyncio.to_thread(eval_tasks.create_task, failed_plan)
                assert connection.execute("SELECT count(*) FROM homelab_tasks").fetchone() == (1,)
                assert connection.execute("SELECT count(*) FROM procrastinate_jobs").fetchone() == (
                    1,
                )

    asyncio.run(exercise())
