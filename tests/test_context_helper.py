import importlib.machinery
import importlib.util
import os
import subprocess
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[1]
loader = importlib.machinery.SourceFileLoader(
    "context_helper", str(ROOT / "node/usr/local/sbin/homelab-context")
)
spec = importlib.util.spec_from_loader(loader.name, loader)
helper = importlib.util.module_from_spec(spec)
loader.exec_module(helper)


@pytest.fixture
def config(tmp_path, monkeypatch):
    volume = tmp_path / "volume"
    volume.mkdir()
    listing = tmp_path / "repositories"
    listing.write_text("example\n")
    listing.chmod(0o600)
    monkeypatch.setattr(helper, "VOLUME", volume)
    monkeypatch.setattr(helper, "ROOT", volume / ".context")
    monkeypatch.setattr(helper, "LIST", listing)
    monkeypatch.setattr(helper.os, "geteuid", lambda: 0)
    monkeypatch.setattr(helper.os, "chown", lambda *args: None)
    monkeypatch.setattr(helper, "ready", lambda: True)
    # The test list belongs to the test user; production requires root ownership.
    if os.stat(listing).st_uid != 0:
        original_fstat = helper.os.fstat

        def owned(fd):
            result = original_fstat(fd)
            return type("FileInfo", (), {"st_mode": result.st_mode, "st_uid": 0})()

        monkeypatch.setattr(helper.os, "fstat", owned)
    return volume, listing


def test_unknown_and_locked_fail_without_stdout(config, monkeypatch, capsys):
    monkeypatch.setattr(helper.sys, "argv", ["homelab-context", "unknown"])
    assert helper.main() == 1
    assert capsys.readouterr() == ("", "homelab-context: unknown repository\n")
    monkeypatch.setattr(helper, "ready", lambda: False)
    monkeypatch.setattr(helper.sys, "argv", ["homelab-context", "example"])
    assert helper.main() == 1
    assert capsys.readouterr() == ("", "homelab-context: data volume is locked\n")


@pytest.mark.parametrize("content", ["../escape\n", "example\nexample\n", "--refresh\n"])
def test_invalid_allowlist_rejected(config, content):
    _, listing = config
    listing.write_text(content)
    with pytest.raises(ValueError, match="invalid repository list"):
        helper.repositories()


def test_list_permissions_rejected(config):
    _, listing = config
    listing.chmod(0o644)
    with pytest.raises(ValueError, match="permissions"):
        helper.repositories()


def test_refresh_fetches_main_into_a_separate_copy(config, monkeypatch):
    volume, _ = config
    source = volume / "example"
    subprocess.run(["git", "init", "-q", "-b", "main", str(source)], check=True)
    subprocess.run(
        ["git", "-C", str(source), "config", "user.email", "test@example.test"], check=True
    )
    subprocess.run(["git", "-C", str(source), "config", "user.name", "Test"], check=True)
    (source / "a.py").write_text("first\n")
    subprocess.run(["git", "-C", str(source), "add", "a.py"], check=True)
    subprocess.run(["git", "-C", str(source), "commit", "-qm", "first"], check=True)
    original_run = helper.subprocess.run

    def run(args, *pos, **kwargs):
        if args[0] == "docker":
            return subprocess.CompletedProcess(args, 0, b"project_name: context\n")
        return original_run(args, *pos, **kwargs)

    monkeypatch.setattr(helper.subprocess, "run", run)
    helper.refresh()
    copy = volume / ".context/example/source"
    assert (copy / "a.py").read_text() == "first\n"
    assert (copy / "a.py").stat().st_ino != (source / "a.py").stat().st_ino
    (source / "a.py").write_text("second\n")
    subprocess.run(["git", "-C", str(source), "commit", "-qam", "second"], check=True)
    helper.refresh()
    assert (copy / "a.py").read_text() == "second\n"
    assert (copy / "a.py").stat().st_mode & 0o022 == 0
    assert (volume / ".context/example/state/memories").is_dir()


def test_serve_passes_only_the_repository_directory_to_docker(config, monkeypatch):
    volume, _ = config
    base = volume / ".context/example"
    (base / "source/.git").mkdir(parents=True)
    (base / "state").mkdir()
    (base / "state/project.yml").write_text("project_name: context\n")
    commands = []
    monkeypatch.setattr(helper.os, "execvp", lambda program, args: commands.append((program, args)))
    helper.serve("example")
    program, args = commands[0]
    assert program == "docker"
    assert "--network=none" in args
    assert "--read-only" in args
    assert "--user=10001:10001" in args
    assert args.count("--mount") == 1
    assert f"type=bind,src={base},dst=/workspace" in args
