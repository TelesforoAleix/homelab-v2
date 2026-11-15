import importlib.machinery
import importlib.util
import json
import os
import subprocess
from pathlib import Path

import pytest

SCRIPTS = Path(__file__).resolve().parents[1] / "node/usr/local/sbin"


def load(name):
    loader = importlib.machinery.SourceFileLoader(name, str(SCRIPTS / name))
    spec = importlib.util.spec_from_loader(name, loader)
    module = importlib.util.module_from_spec(spec)
    loader.exec_module(module)
    return module


def fake_docker(tmp_path, body):
    docker = tmp_path / "docker"
    docker.write_text("#!/bin/sh\n" + body)
    docker.chmod(0o755)
    return str(tmp_path) + os.pathsep + os.environ["PATH"]


def test_dump_rotation_and_permissions(tmp_path, monkeypatch, capsys):
    dump = load("homelab-dump")
    monkeypatch.setattr(dump, "DIRECTORY", tmp_path)
    monkeypatch.setenv(
        "PATH",
        fake_docker(
            tmp_path,
            """case "$*" in
 *pg_database*) echo '["homelab", "factory"]' ;;
 *pg_dump*) printf dump ;;
esac
""",
        ),
    )
    for database in ("homelab", "factory"):
        for day in range(1, 10):
            (tmp_path / f"{database}-2020-01-{day:02}.dump").write_text("old")
    assert dump.main() == 0
    for database in ("homelab", "factory"):
        files = sorted(tmp_path.glob(database + "-*.dump"))
        assert len(files) == 7
        assert not (tmp_path / f"{database}-2020-01-03.dump").exists()
        assert files[-1].read_bytes() == b"dump"
        assert files[-1].stat().st_mode & 0o777 == 0o640
    assert capsys.readouterr().out == "ok\n"


@pytest.mark.parametrize("missing", [True, False])
def test_dump_failure_is_fixed_line(tmp_path, monkeypatch, capsys, missing):
    dump = load("homelab-dump")
    monkeypatch.setattr(dump, "DIRECTORY", tmp_path / "missing" if missing else tmp_path)
    monkeypatch.setenv("PATH", fake_docker(tmp_path, "echo sensitive >&2\nexit 1\n"))
    assert dump.main() == 1
    output = capsys.readouterr()
    assert output.err == "homelab-dump: failed\n"
    assert output.out == ""
    assert not list(tmp_path.glob("*.dump"))


def test_restore_removes_container_after_failure(tmp_path, monkeypatch, capsys):
    check = load("homelab-restore-check")
    log = tmp_path / "calls"
    monkeypatch.setenv("CALLS", str(log))
    monkeypatch.setenv(
        "PATH",
        fake_docker(
            tmp_path,
            """echo "$*" >> "$CALLS"
case "$1" in
 run) exit 0 ;;
 exec) exit 1 ;;
 rm) exit 0 ;;
esac
""",
        ),
    )
    monkeypatch.setattr(check.time, "monotonic", iter([0, 61, 62]).__next__)
    monkeypatch.setattr("sys.argv", ["homelab-restore-check", str(tmp_path / "dump")])
    assert check.main() == 1
    assert "rm -f homelab-restore-check-" in log.read_text()
    assert capsys.readouterr().err == 'homelab-restore-check: failed database "homelab"\n'


def test_restore_compares_tables_and_counts(tmp_path, monkeypatch, capsys):
    check = load("homelab-restore-check")
    log = tmp_path / "calls"
    dump = tmp_path / "backup.dump"
    dump.write_bytes(b"dump")
    monkeypatch.setenv("CALLS", str(log))
    monkeypatch.setenv(
        "PATH",
        fake_docker(
            tmp_path,
            """echo "$*" >> "$CALLS"
case "$*" in
 *pg_restore*) cat >/dev/null ;;
 *pg_tables*) echo '[["public", "items"]]' ;;
 *count*) echo 3 ;;
esac
""",
        ),
    )
    monkeypatch.setattr("sys.argv", ["homelab-restore-check", str(dump)])
    assert check.main() == 0
    assert capsys.readouterr().out == "ok\n"
    calls = log.read_text()
    assert "--network none" in calls
    assert calls.count("SELECT count(*)") == 2
    assert "rm -f homelab-restore-check-" in calls


def test_restore_reports_first_count_difference(tmp_path, monkeypatch, capsys):
    check = load("homelab-restore-check")
    monkeypatch.setattr(check, "run", lambda *args, **kwargs: None)
    values = iter(['[["public", "items"]]', '[["public", "items"]]', "4", "3"])
    monkeypatch.setattr(check, "query", lambda *args: next(values))
    path = tmp_path / "dump"
    path.write_bytes(b"dump")
    monkeypatch.setattr(
        check.subprocess, "run", lambda *args, **kwargs: subprocess.CompletedProcess(args, 0)
    )
    assert check.check(path, "factory") == 1
    assert (
        capsys.readouterr().out
        == 'database "factory" count differs: "public"."items" live=4 restored=3\n'
    )


def test_one_database_failure_fails_dump_and_preserves_previous(tmp_path, monkeypatch, capsys):
    dump = load("homelab-dump")
    monkeypatch.setattr(dump, "DIRECTORY", tmp_path)
    date = dump.datetime.date.today().isoformat()
    previous = tmp_path / ("factory-" + date + ".dump")
    previous.write_bytes(b"previous")
    monkeypatch.setenv(
        "PATH",
        fake_docker(
            tmp_path,
            """case "$*" in
 *pg_database*) echo '["homelab", "factory"]' ;;
 *pg_dump*factory*) echo sensitive >&2; printf partial; exit 1 ;;
 *pg_dump*) printf dump ;;
esac
""",
        ),
    )
    assert dump.main() == 1
    assert (tmp_path / ("homelab-" + date + ".dump")).read_bytes() == b"dump"
    assert previous.read_bytes() == b"previous"
    assert not list(tmp_path.glob(".dump-*"))
    assert capsys.readouterr().err == "homelab-dump: failed\n"


@pytest.mark.parametrize("difference", [None, "count", "table", "missing"])
def test_restore_checks_every_database(tmp_path, monkeypatch, capsys, difference):
    check = load("homelab-restore-check")
    monkeypatch.setattr(check, "DIRECTORY", tmp_path)
    monkeypatch.setattr("sys.argv", ["homelab-restore-check"])
    date = check.datetime.date.today().isoformat()
    for database in ("homelab", "factory", "later"):
        (tmp_path / (database + "-" + date + ".dump")).write_bytes(b"dump")
    if difference == "missing":
        (tmp_path / ("factory-" + date + ".dump")).unlink()
    calls = []

    def run(args, **kwargs):
        calls.append(args)
        if "pg_database" in args[-1]:
            return subprocess.CompletedProcess(args, 0, b'["homelab", "factory", "later"]')
        if "pg_tables" in args[-1]:
            output = (
                b"[]"
                if difference == "table" and args[2] == check.LIVE and "dbname='factory'" in args
                else b'[["public","items"]]'
            )
            return subprocess.CompletedProcess(args, 0, output)
        if args[-1].startswith("SELECT count"):
            output = (
                b"4"
                if difference == "count" and args[2] == check.LIVE and "dbname='factory'" in args
                else b"3"
            )
            return subprocess.CompletedProcess(args, 0, output)
        return subprocess.CompletedProcess(args, 0, b"")

    monkeypatch.setattr(check.subprocess, "run", run)
    assert check.main() == (1 if difference else 0)
    output = capsys.readouterr()
    if difference:
        assert 'database "factory"' in output.out + output.err
        assert "ok" not in output.out
    else:
        assert output.out == "ok\n"
    containers = [args[args.index("--name") + 1] for args in calls if args[:2] == ["docker", "run"]]
    assert len(set(containers)) == (2 if difference else 3)
    assert sum(args[:3] == ["docker", "rm", "-f"] for args in calls) == len(containers)
    assert all(
        "--network" in args and "none" in args for args in calls if args[:2] == ["docker", "run"]
    )
    for database in ("homelab",) if difference == "missing" else ("homelab", "factory"):
        assert any(
            args[2] == check.LIVE
            and "-d" in args
            and args[args.index("-d") + 1] == "dbname='" + database + "'"
            for args in calls
            if len(args) > 2
        )


def test_database_names_are_literal_paths_and_connections(tmp_path, monkeypatch):
    dump = load("homelab-dump")
    restore = load("homelab-restore-check")
    monkeypatch.setattr(dump, "DIRECTORY", tmp_path)
    names = ["factory", "data[1]", "service=other", "quote'\\name"]
    calls = []

    def run(args, **kwargs):
        calls.append(args)
        if "pg_database" in args[-1]:
            return subprocess.CompletedProcess(args, 0, json.dumps(names).encode())
        if "pg_dump" in args:
            kwargs["stdout"].write(b"dump")
        return subprocess.CompletedProcess(args, 0, b"[]")

    monkeypatch.setattr(dump.subprocess, "run", run)
    (tmp_path / "data1-2020-01-01.dump").write_bytes(b"unrelated")
    assert dump.main() == 0
    for name in names:
        assert (tmp_path / (name + "-" + dump.datetime.date.today().isoformat() + ".dump")).exists()
        restore.query(restore.LIVE, restore.TABLES, name)
    connections = [args[args.index("--dbname") + 1] for args in calls if "pg_dump" in args]
    assert connections == [
        "dbname='factory'",
        "dbname='data[1]'",
        "dbname='service=other'",
        "dbname='quote\\'\\\\name'",
    ]
    assert [args[args.index("-d") + 1] for args in calls if restore.TABLES in args] == connections
    assert (tmp_path / "data1-2020-01-01.dump").read_bytes() == b"unrelated"
