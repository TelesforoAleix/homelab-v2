"""The status command exposes only the requested slice's task outcomes."""

import json
import os
import subprocess
import sys

from homelab.tasks import status
from tests.test_task_store import connection, encoded, entry, parse, submit, task_file  # noqa: F401


def test_slice_status_order_fields_and_isolation(connection):  # noqa: F811
    ids = submit(connection, parse(encoded(task_file(entry(3), entry(1)))))
    submit(connection, parse(encoded(task_file(entry(2), slice="other"))))
    connection.execute(
        "UPDATE homelab_tasks SET status = 'failed', reason = 'check_failed' WHERE id = %s",
        (ids[1],),
    )

    assert status.status(connection, "sample") == [
        {"id": ids[0], "position": 1, "title": "Task 1", "status": "queued", "reason": None},
        {
            "id": ids[1],
            "position": 3,
            "title": "Task 3",
            "status": "failed",
            "reason": "check_failed",
        },
    ]
    assert status.status(connection, "unknown") == []


def test_command_prints_json(monkeypatch, capsys):
    class Connection:
        def __enter__(self):
            return self

        def __exit__(self, *args):
            pass

        def execute(self, query, params):
            assert params == ("unknown",)
            return self

        def fetchall(self):
            return []

    monkeypatch.setattr(status.psycopg, "connect", lambda url: Connection())
    monkeypatch.setattr(sys, "argv", ["status", "unknown"])
    assert status.main() == 0
    output = capsys.readouterr()
    assert json.loads(output.out) == []
    assert output.err == ""


def test_database_failure_does_not_expose_connection_string():
    url = "postgresql://private-user:private-password@127.0.0.1:9/none"
    result = subprocess.run(
        [sys.executable, "-m", "homelab.tasks.status", "sample"],
        capture_output=True,
        env={**os.environ, "HOMELAB_DATABASE_URL": url},
    )
    assert result.returncode != 0
    assert result.stdout == b""
    assert result.stderr == b"homelab.tasks.status: database failure\n"
