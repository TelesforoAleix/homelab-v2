import json
import subprocess
import sys
from datetime import datetime
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[1]
HELPER = ROOT / "node/usr/local/sbin/homelab-inbox"
RUNNER = """
import importlib.machinery, importlib.util, pathlib, sys
loader = importlib.machinery.SourceFileLoader('helper', sys.argv[1])
spec = importlib.util.spec_from_loader(loader.name, loader)
module = importlib.util.module_from_spec(spec)
loader.exec_module(module)
raise SystemExit(module.main(sys.argv[3:], pathlib.Path(sys.argv[2])))
"""


def run(inbox, *args):
    return subprocess.run(
        [sys.executable, "-I", "-c", RUNNER, str(HELPER), str(inbox), *args],
        capture_output=True,
        check=False,
    )


def item(inbox, number, kind="idea", target=None, status="new", **extra):
    ident = f"{number:032x}"
    directory = inbox / ident
    directory.mkdir(parents=True)
    data = {
        "id": ident,
        "created": f"2026-01-{number:02d}T00:00:00+00:00",
        "kind": kind,
        "target": target,
        "status": status,
        "text": "private content",
        **extra,
    }
    (directory / "item.json").write_text(json.dumps(data), encoding="utf-8")
    (directory / "item.json").chmod(0o600)
    return data


def test_list_routes_and_orders_new_items(tmp_path):
    inbox = tmp_path / "inbox"
    idea = item(inbox, 3)
    link = item(inbox, 1, "link")
    file = item(inbox, 2, "file")
    dev = item(inbox, 4, "message", "development")
    repo = item(inbox, 5, "message", "alpha")
    item(inbox, 6, "message", "beta")
    item(inbox, 7, status="read")
    result = run(inbox, "list", "development")
    assert result.returncode == 0, result.stderr
    assert json.loads(result.stdout) == [link, file, idea, dev]
    result = run(inbox, "list", "alpha")
    assert result.returncode == 0, result.stderr
    assert json.loads(result.stdout) == [repo]
    assert json.loads(run(inbox, "list", "empty-repo").stdout) == []


def test_list_usage_and_locked(tmp_path):
    inbox = tmp_path / "inbox"
    for args in [("list",), ("list", "../bad"), ("list", "A"), ("list", "a" * 65), ("bad",)]:
        result = run(inbox, *args)
        assert result.returncode == 2
    result = run(inbox, "list", "development")
    assert result.returncode == 3
    assert (result.stdout + result.stderr).strip() == b"locked"


def test_list_rejects_symlinked_item_and_metadata(tmp_path):
    inbox = tmp_path / "inbox"
    inbox.mkdir()
    outside = tmp_path / "outside"
    secret = item(outside, 1)
    (inbox / secret["id"]).symlink_to(outside / secret["id"], target_is_directory=True)
    local = item(inbox, 2)
    metadata = inbox / local["id"] / "item.json"
    metadata.unlink()
    metadata.symlink_to(outside / secret["id"] / "item.json")
    result = run(inbox, "list", "development")
    assert b"private content" not in result.stdout + result.stderr
    if result.returncode == 0:
        assert json.loads(result.stdout) == []


def test_file_streams_only_attachment_and_rejects_invalid(tmp_path):
    inbox = tmp_path / "inbox"
    data = item(inbox, 1, "file", file="file/payload.bin")
    folder = inbox / data["id"] / "file"
    folder.mkdir()
    payload = b"\x00\xff\nopaque\x00"
    (folder / "payload.bin").write_bytes(payload)
    result = run(inbox, "file", data["id"])
    assert result.returncode == 0 and result.stdout == payload
    plain = item(inbox, 2)
    for ident in [plain["id"], "f" * 32, "../escape", "A" * 32]:
        result = run(inbox, "file", ident)
        assert result.returncode == 2
        assert b"private content" not in result.stdout + result.stderr
    assert run(tmp_path / "missing", "file", data["id"]).returncode == 3


def test_file_never_follows_symlinks_or_escapes(tmp_path):
    inbox = tmp_path / "inbox"
    outside = tmp_path / "secret"
    outside.write_bytes(b"outside-secret")
    data = item(inbox, 1, "file", file="file/payload")
    folder = inbox / data["id"] / "file"
    folder.mkdir()
    (folder / "payload").symlink_to(outside)
    result = run(inbox, "file", data["id"])
    assert result.returncode != 0
    assert b"outside-secret" not in result.stdout + result.stderr
    meta = inbox / data["id"] / "item.json"
    data["file"] = "../../secret"
    meta.write_text(json.dumps(data))
    result = run(inbox, "file", data["id"])
    assert result.returncode != 0
    assert b"outside-secret" not in result.stdout + result.stderr


def test_done_preserves_metadata_and_is_idempotent(tmp_path):
    inbox = tmp_path / "inbox"
    first = item(inbox, 1)
    second = item(inbox, 2, "message", "alpha")
    paths = [inbox / data["id"] / "item.json" for data in (first, second)]
    before = [path.stat() for path in paths]
    result = run(inbox, "done", first["id"], second["id"])
    assert result.returncode == 0, result.stderr
    for path, original, stat in zip(paths, (first, second), before, strict=True):
        updated = json.loads(path.read_text())
        assert updated.pop("status") == "read"
        timestamp = datetime.fromisoformat(updated.pop("read_at"))
        assert timestamp.utcoffset() is not None
        assert updated == {k: v for k, v in original.items() if k != "status"}
        after = path.stat()
        assert (after.st_uid, after.st_gid, after.st_mode & 0o777) == (
            stat.st_uid,
            stat.st_gid,
            stat.st_mode & 0o777,
        )
        assert after.st_ino != stat.st_ino
    saved = [path.read_bytes() for path in paths]
    assert run(inbox, "done", first["id"], second["id"]).returncode == 0
    assert [path.read_bytes() for path in paths] == saved
    assert json.loads(run(inbox, "list", "development").stdout) == []


def test_done_invalid_locked_and_symlinks(tmp_path):
    inbox = tmp_path / "inbox"
    data = item(inbox, 1)
    path = inbox / data["id"] / "item.json"
    original = path.read_bytes()
    for ident in ["f" * 32, "../escape", "A" * 32]:
        result = run(inbox, "done", ident)
        assert result.returncode == 2
        assert b"private content" not in result.stdout + result.stderr
    assert run(inbox, "done").returncode == 2
    assert run(tmp_path / "missing", "done", data["id"]).returncode == 3
    outside = tmp_path / "outside.json"
    outside.write_bytes(original)
    path.unlink()
    path.symlink_to(outside)
    assert run(inbox, "done", data["id"]).returncode != 0
    assert outside.read_bytes() == original


def test_inbox_helper_documentation():
    operations = (ROOT / "docs/operations.md").read_text()
    readme = (ROOT / "README.md").read_text()
    bot = operations.split("### Bot inbox", 1)[1].split("\n### ", 1)[0]
    rebuild = operations.split("## Rebuilding the node", 1)[1].split("\n## ", 1)[0]
    for phrase in (
        "homelab-inbox list",
        "homelab-inbox file",
        "homelab-inbox done",
        "locked",
        "rollback",
    ):
        assert phrase in bot
    assert "exit code" in bot and "0755" in bot
    assert "homelab-inbox" in rebuild and "0755" in rebuild
    assert "homelab-inbox" in readme
