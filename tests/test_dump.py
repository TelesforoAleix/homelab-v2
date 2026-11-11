import importlib.machinery
import importlib.util
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
    monkeypatch.setenv("PATH", fake_docker(tmp_path, "printf dump\n"))
    for day in range(1, 10):
        (tmp_path / f"homelab-2020-01-{day:02}.dump").write_text("old")
    assert dump.main() == 0
    files = sorted(tmp_path.glob("*.dump"))
    assert len(files) == 7
    assert not (tmp_path / "homelab-2020-01-03.dump").exists()
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
    assert capsys.readouterr().err == "homelab-restore-check: failed\n"


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
    assert check.check(path) == 1
    assert capsys.readouterr().out == 'count differs: "public"."items" live=4 restored=3\n'
