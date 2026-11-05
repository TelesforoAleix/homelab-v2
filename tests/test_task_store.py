"""Task file validation runs everywhere; store checks need an isolated Postgres database."""

import json
import os
import subprocess
import sys
from pathlib import Path
from uuid import uuid4

import psycopg
import pytest
import yaml
from psycopg import sql
from psycopg.conninfo import make_conninfo

from homelab.jobs import schema as jobs_schema
from homelab.jobs.app import app
from homelab.tasks import store
from homelab.tasks.store import Refused
from homelab.tasks.submit import parse, submit

COMMIT = "a" * 40
SECRET = "do-not-repeat-this-goal"


def entry(position, *depends_on, **fields):
    return {
        "position": position,
        "depends_on": list(depends_on),
        "title": f"Task {position}",
        "goal": SECRET,
        "allowed_files": ["src/app.py"],
        "check_command": "uv run pytest",
        **fields,
    }


def task_file(*tasks, **fields):
    return {
        "slice": "sample",
        "repository": "example/repo",
        "branch": "main",
        "base_commit": COMMIT,
        "tasks": list(tasks) or [entry(1)],
        **fields,
    }


def encoded(document):
    return json.dumps(document).encode()


def test_good_file_parses_with_defaults():
    plan = parse(encoded(task_file(entry(1), entry(2, 1, pinned_paths=["tests/test_app.py"]))))

    assert [task.position for task in plan.tasks] == [1, 2]
    assert plan.tasks[0].priority == 0
    assert plan.tasks[0].severity == "normal"
    assert plan.tasks[0].pinned_paths == []
    assert plan.tasks[1].pinned_paths == ["tests/test_app.py"]


@pytest.mark.parametrize(
    "document",
    [
        task_file(entry(1), entry(1)),
        task_file(entry(1, 1)),
        task_file(entry(1, 3), entry(2, 1), entry(3, 2)),
        task_file(entry(2, 1, 1)),
        task_file(entry(0)),
        task_file(entry(2**31)),
        task_file(entry("1")),
        task_file(entry(True)),
        task_file(entry(1, title=" ")),
        task_file(entry(1, goal="nul\x00byte")),
        task_file(entry(1, allowed_files=[])),
        task_file(entry(1, allowed_files=["../outside"])),
        task_file(entry(1, allowed_files=["/etc/passwd"])),
        task_file(entry(1, pinned_paths=[".git/config"])),
        task_file(entry(1, pinned_paths=[".venv/bin/pytest"])),
        task_file(entry(1, pinned_paths=["--flag"])),
        task_file(entry(1, priority=2**31)),
        task_file(entry(1, status="done")),
        task_file(base_commit="main"),
        task_file(branch="-delete"),
        task_file(extra="field"),
        {**task_file(), "tasks": []},
        [],
    ],
)
def test_bad_file_is_refused(document):
    with pytest.raises(Refused):
        parse(encoded(document))


@pytest.mark.parametrize("raw", [b"", b"not json", b"\xff\xfe"])
def test_unreadable_file_is_refused(raw):
    with pytest.raises(Refused):
        parse(raw)


def test_refusal_never_repeats_content():
    document = task_file(entry(1, allowed_files=[f"../{SECRET}"]))

    with pytest.raises(Refused) as refused:
        parse(encoded(document))

    assert "allowed_files" in str(refused.value)
    assert SECRET not in str(refused.value)


def test_command_refuses_before_touching_the_database():
    result = subprocess.run(
        [sys.executable, "-m", "homelab.tasks.submit"],
        input=encoded(task_file(entry(1, 1))),
        capture_output=True,
        env={**os.environ, "HOMELAB_DATABASE_URL": "postgresql://nobody@127.0.0.1:9/none"},
    )

    assert result.returncode == 1
    assert result.stdout == b""
    assert b"cycle" in result.stderr
    assert SECRET.encode() not in result.stderr


def test_job_is_registered_on_its_own_queue():
    app.perform_import_paths()

    task = app.tasks[store.JOB]
    assert task.queue == "tasks"
    assert task.retry_strategy is None


def test_no_service_consumes_the_tasks_queue_or_reaches_docker():
    compose = yaml.safe_load(Path("compose.yaml").read_text())
    assert "--queues local,default " in " ".join(compose["services"]["worker"]["command"])
    assert "docker.sock" not in Path("compose.yaml").read_text()


def test_start_step_applies_procrastinate_then_task_tables(monkeypatch):
    calls = []
    monkeypatch.setattr(jobs_schema, "ensure_schema", lambda: calls.append("jobs"))
    monkeypatch.setattr(jobs_schema, "ensure_task_tables", lambda: calls.append("tasks"))

    jobs_schema.main()

    assert calls == ["jobs", "tasks"]


TABLES = ("homelab_roles", "homelab_tasks", "homelab_task_attempts", "homelab_task_traces")


@pytest.fixture
def connection():
    database_url = os.environ.get("HOMELAB_TEST_DATABASE_URL")
    if not database_url:
        pytest.skip("Set HOMELAB_TEST_DATABASE_URL to an isolated Postgres database")
    name = "task_test_" + uuid4().hex
    with psycopg.connect(database_url) as admin:
        admin.execute(sql.SQL("CREATE SCHEMA {}").format(sql.Identifier(name)))
    url = make_conninfo(database_url, options="-c search_path=" + name)
    try:
        with psycopg.connect(url, autocommit=True) as database:
            database.execute(app.schema_manager.get_schema())
            store.apply_schema(database)
            yield database
    finally:
        with psycopg.connect(database_url) as admin:
            admin.execute(sql.SQL("DROP SCHEMA {} CASCADE").format(sql.Identifier(name)))


def snapshot(database):
    """Every column, constraint, index and trigger, and every row, of the task tables."""
    catalogue = database.execute(
        """SELECT table_name, column_name, data_type, column_default, is_nullable
           FROM information_schema.columns
           WHERE table_schema = current_schema() AND table_name = ANY(%(tables)s)
           UNION ALL
           SELECT conrelid::regclass::text, conname, contype::text,
                  pg_get_constraintdef(oid), ''
           FROM pg_constraint
           WHERE connamespace = current_schema()::regnamespace
             AND conrelid::regclass::text = ANY(%(tables)s)
           UNION ALL
           SELECT tablename, indexname, 'index', indexdef, ''
           FROM pg_indexes
           WHERE schemaname = current_schema() AND tablename = ANY(%(tables)s)
           UNION ALL
           SELECT tgrelid::regclass::text, tgname, 'trigger', pg_get_triggerdef(oid), ''
           FROM pg_trigger
           WHERE NOT tgisinternal AND tgrelid::regclass::text = ANY(%(tables)s)
           ORDER BY 1, 2, 3""",
        {"tables": list(TABLES)},
    ).fetchall()
    rows = {
        table: database.execute(
            sql.SQL("SELECT to_jsonb(t)::text FROM {} t ORDER BY 1").format(sql.Identifier(table))
        ).fetchall()
        for table in TABLES
    }
    return catalogue, rows


def counts(database):
    return (
        database.execute("SELECT count(*) FROM homelab_tasks").fetchone()[0],
        database.execute("SELECT count(*) FROM procrastinate_jobs").fetchone()[0],
    )


def jobs(database):
    return database.execute(
        """SELECT queue_name, task_name, args, status::text, priority
           FROM procrastinate_jobs ORDER BY id"""
    ).fetchall()


def test_schema_applies_twice_with_no_change(connection):
    connection.execute(
        """INSERT INTO homelab_tasks (slice, repository, branch, base_commit, position, title,
           goal, allowed_files, check_command)
           VALUES ('s', 'r', 'b', 'c', 1, 't', 'g', '{a}', 'true')"""
    )
    before = snapshot(connection)
    assert len(before[0]) > 40

    store.apply_schema(connection)

    assert snapshot(connection) == before


def test_only_the_three_roles_are_seeded(connection):
    rows = connection.execute("SELECT role, routes FROM homelab_roles ORDER BY role").fetchall()

    route = [{"kind": "litellm", "purpose": "task-worker"}]
    assert rows == [("side-job", route), ("ticket-reviewer", route), ("worker", route)]


def test_seeds_do_not_overwrite_an_edited_row(connection):
    edited = [
        {"kind": "litellm", "purpose": "task-worker"},
        {"kind": "cli", "tool": "some-tool", "model": "some-model"},
    ]
    stale = connection.execute(
        "SELECT updated_at FROM homelab_roles WHERE role = 'worker'"
    ).fetchone()[0]
    connection.execute(
        "UPDATE homelab_roles SET routes = %s::jsonb WHERE role = 'worker'",
        (json.dumps(edited),),
    )
    connection.execute("DELETE FROM homelab_roles WHERE role = 'side-job'")

    store.apply_schema(connection)

    routes, touched = connection.execute(
        "SELECT routes, updated_at FROM homelab_roles WHERE role = 'worker'"
    ).fetchone()
    assert routes == edited
    assert touched >= stale
    assert connection.execute("SELECT 1 FROM homelab_roles WHERE role = 'side-job'").fetchone() == (
        1,
    )


def test_good_file_writes_tasks_and_defers_only_the_ready(connection):
    plan = parse(
        encoded(
            task_file(
                entry(3, 1, 2),
                entry(1, priority=5, pinned_paths=["tests/test_app.py"]),
                entry(2, 1),
                entry(4, severity="high"),
            )
        )
    )

    ids = submit(connection, plan)

    rows = connection.execute(
        """SELECT id, position, depends_on, status, priority, severity, pinned_paths,
                  allowed_files, slice, repository, branch, base_commit, reason
           FROM homelab_tasks ORDER BY position"""
    ).fetchall()
    assert [row[0] for row in rows] == ids
    first, second, third, fourth = ids
    assert [row[2] for row in rows] == [[], [first], [first, second], []]
    assert {row[3] for row in rows} == {"queued"}
    assert rows[0][4:7] == (5, "normal", ["tests/test_app.py"])
    assert rows[3][5] == "high"
    assert rows[0][7:] == (["src/app.py"], "sample", "example/repo", "main", COMMIT, None)
    assert jobs(connection) == [
        ("tasks", store.JOB, {"task_id": first}, "todo", 5),
        ("tasks", store.JOB, {"task_id": fourth}, "todo", 0),
    ]


def refused_without_change(database, raw):
    before = counts(database)
    with pytest.raises(Refused):
        submit(database, parse(raw))
    assert counts(database) == before


def test_bad_files_change_nothing(connection):
    for document in (
        task_file(entry(1, title="")),
        task_file(entry(1, 2), entry(2, 1)),
        task_file(entry(1), entry(1)),
    ):
        refused_without_change(connection, encoded(document))
    assert counts(connection) == (0, 0)


def test_unknown_dependency_changes_nothing(connection):
    refused_without_change(connection, encoded(task_file(entry(1), entry(2, 1), entry(3, 9))))
    assert counts(connection) == (0, 0)


def test_dependency_in_another_slice_is_unknown(connection):
    submit(connection, parse(encoded(task_file(entry(1), slice="other"))))

    refused_without_change(connection, encoded(task_file(entry(2, 1))))


def test_repeated_position_changes_nothing(connection):
    submit(connection, parse(encoded(task_file(entry(1), entry(2)))))
    before = snapshot(connection)

    refused_without_change(connection, encoded(task_file(entry(3), entry(2))))

    assert snapshot(connection) == before
    assert counts(connection) == (2, 2)


def test_failed_defer_leaves_no_tasks(connection, monkeypatch):
    def fail(*args, **kwargs):
        raise RuntimeError("defer failed")

    monkeypatch.setattr("homelab.tasks.submit.run_coding_task.configure", fail)

    with pytest.raises(RuntimeError):
        submit(connection, parse(encoded(task_file())))

    assert counts(connection) == (0, 0)


def test_later_file_in_a_slice_waits_only_for_tasks_not_done(connection):
    (first, second) = submit(connection, parse(encoded(task_file(entry(1), entry(2)))))
    connection.execute("UPDATE homelab_tasks SET status = 'done' WHERE id = %s", (first,))

    third, fourth = submit(connection, parse(encoded(task_file(entry(3, 1), entry(4, 2)))))

    rows = connection.execute(
        "SELECT depends_on FROM homelab_tasks WHERE id = ANY(%s) ORDER BY id",
        ([third, fourth],),
    ).fetchall()
    assert sorted(row[0] for row in rows) == [[first], [second]]
    assert [job[2] for job in jobs(connection)] == [
        {"task_id": first},
        {"task_id": second},
        {"task_id": third},
    ]


def test_runs_compare_by_role_and_route_in_plain_sql(connection):
    (task,) = submit(connection, parse(encoded(task_file())))
    route = json.dumps({"kind": "litellm", "purpose": "task-worker"})
    connection.execute(
        """INSERT INTO homelab_task_attempts (task_id, number, role, route)
           VALUES (%s, 1, 'worker', %s::jsonb)""",
        (task, route),
    )
    for kind, attempt, cost in (("lead", 1, 0.25), ("lead", 1, None), ("side", None, 0.5)):
        connection.execute(
            """INSERT INTO homelab_task_traces
               (task_id, attempt, role, route, kind, steps, fresh_tokens, cached_tokens,
                output_tokens, cost_usd, seconds, result)
               VALUES (%s, %s, 'worker', %s::jsonb, %s, 3, 10, 20, 30, %s, 1.5, 'stop')""",
            (task, attempt, route, kind, cost),
        )

    rows = connection.execute(
        """SELECT role, route, count(*), sum(output_tokens)::bigint, sum(cost_usd)::float
           FROM homelab_task_traces GROUP BY role, route"""
    ).fetchall()

    assert rows == [("worker", json.loads(route), 3, 90, 0.75)]
    with pytest.raises(psycopg.errors.ForeignKeyViolation):
        connection.execute(
            """INSERT INTO homelab_task_traces (task_id, attempt, role, route, kind)
               VALUES (%s, 2, 'worker', %s::jsonb, 'review')""",
            (task, route),
        )
