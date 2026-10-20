import importlib.machinery
import importlib.util
import json
import os
import stat
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[1]
HELPER = ROOT / "node/usr/local/sbin/homelab-inbox"
loader = importlib.machinery.SourceFileLoader("inbox_helper", str(HELPER))
spec = importlib.util.spec_from_loader(loader.name, loader)
helper = importlib.util.module_from_spec(spec)
loader.exec_module(helper)


@pytest.fixture
def inbox(tmp_path, monkeypatch):
    path = tmp_path.resolve() / "inbox"
    path.mkdir(mode=0o700)
    monkeypatch.setattr(helper, "INBOX", path)
    return path


def store(inbox, number=1, **fields):
    identity = f"{number:032x}"
    folder = inbox / identity
    folder.mkdir()
    item = dict(
        id=identity,
        kind="idea",
        target=None,
        status="new",
        created=f"2026-10-20T00:00:{number:02d}+00:00",
        text="private synthetic content",
    )
    item.update(fields)
    (folder / "item.json").write_text(json.dumps(item))
    (folder / "item.json").chmod(0o600)
    return folder, item


def test_target_rules_and_oldest_first(inbox, capsys):
    expected = []
    for number, kind in [(3, "file"), (1, "idea"), (2, "link")]:
        _, item = store(inbox, number, kind=kind)
        expected.append(item)
    _, development = store(inbox, 4, kind="message", target="development")
    _, repo = store(inbox, 5, kind="message", target="med-ask")
    store(inbox, 6, kind="message", target="elsewhere")
    store(inbox, 7, status="read")
    (inbox / ".item-unpublished").mkdir()
    assert helper.main(["list", "development"]) == 0
    assert json.loads(capsys.readouterr().out) == sorted(expected, key=lambda i: i["created"]) + [
        development
    ]
    assert helper.main(["list", "med-ask"]) == 0
    assert json.loads(capsys.readouterr().out) == [repo]
    assert helper.main(["list", "unused"]) == 0
    assert capsys.readouterr().out == "[]\n"


def test_file_bytes_unchanged(inbox, capsysbinary):
    folder, item = store(inbox, kind="file", file="file/data.bin")
    (folder / "file").mkdir()
    payload = bytes(range(256)) + b"\x00\xff\r\n"
    (folder / "file/data.bin").write_bytes(payload)
    assert helper.main(["file", item["id"]]) == 0
    captured = capsysbinary.readouterr()
    assert captured.out == payload
    assert captured.err == b""


@pytest.mark.parametrize("mode", [0o600, 0o640])
def test_done_preserves_fields_owner_group_mode_and_is_idempotent(inbox, capsys, mode):
    folders = [store(inbox, n)[0] for n in (1, 2)]
    for folder in folders:
        (folder / "item.json").chmod(mode)
        if os.geteuid() == 0:
            os.chown(folder / "item.json", 65534, 65534)
    before = [(folder / "item.json").stat() for folder in folders]
    originals = [json.loads((folder / "item.json").read_text()) for folder in folders]
    assert helper.main(["done", *[folder.name for folder in folders]]) == 0
    for folder, previous, original in zip(folders, before, originals, strict=True):
        path = folder / "item.json"
        after = path.stat()
        item = json.loads(path.read_text())
        assert item.pop("status") == "read"
        assert helper.datetime.fromisoformat(item.pop("read_at")).tzinfo is not None
        original.pop("status")
        assert item == original
        assert (after.st_uid, after.st_gid, stat.S_IMODE(after.st_mode)) == (
            previous.st_uid,
            previous.st_gid,
            stat.S_IMODE(previous.st_mode),
        )
        assert after.st_ino != previous.st_ino
        content = path.read_bytes()
        assert helper.main(["done", folder.name]) == 0
        assert path.read_bytes() == content
        assert path.stat().st_ino == after.st_ino
        assert list(folder.iterdir()) == [path]
    assert helper.main(["list", "development"]) == 0
    assert capsys.readouterr().out == "[]\n"


@pytest.mark.parametrize(
    "args",
    [
        [],
        ["list"],
        ["list", "../bad"],
        ["list", "A"],
        ["list", "a" * 65],
        ["file"],
        ["done"],
        ["file", "a" * 31],
        ["file", "A" * 32],
        ["done", "../escape"],
        ["done", "a" * 32, "bad"],
        ["file", "a" * 32, "extra"],
        ["bad"],
    ],
)
def test_bad_arguments(inbox, capsys, args):
    assert helper.main(args) == 2
    assert capsys.readouterr().out == ""


@pytest.mark.parametrize("command", ["file", "done"])
def test_unknown_id(inbox, capsys, command):
    assert helper.main([command, "0" * 32]) == 2
    captured = capsys.readouterr()
    assert captured.out == ""
    assert captured.err == "homelab-inbox: operation failed\n"


@pytest.mark.parametrize(
    "command", [["list", "development"], ["file", "0" * 32], ["done", "0" * 32]]
)
def test_locked_missing(inbox, capsys, command):
    inbox.rmdir()
    assert helper.main(command) == 3
    captured = capsys.readouterr()
    assert captured.out == ""
    assert captured.err == "locked\n"


def test_locked_unreadable(inbox, monkeypatch, capsys):
    real_listdir = os.listdir

    def denied(path):
        if isinstance(path, int):
            raise PermissionError
        return real_listdir(path)

    monkeypatch.setattr(helper.os, "listdir", denied)
    assert helper.main(["list", "development"]) == 3
    assert capsys.readouterr().err == "locked\n"


@pytest.mark.parametrize("command", ["list", "file", "done"])
@pytest.mark.parametrize("redirect", ["item", "record", "filedir", "attachment", "ancestor"])
def test_symlinks_rejected(inbox, tmp_path, capsys, command, redirect):
    folder, item = store(inbox, kind="file", file="file/data")
    (folder / "file").mkdir()
    (folder / "file/data").write_bytes(b"opaque")
    outside = tmp_path / "outside"
    if redirect == "item":
        folder.rename(outside)
        folder.symlink_to(outside, target_is_directory=True)
    elif redirect == "record":
        (folder / "item.json").rename(outside)
        (folder / "item.json").symlink_to(outside)
    elif redirect == "ancestor":
        inbox.rename(outside)
        inbox.symlink_to(outside, target_is_directory=True)
    elif redirect == "filedir":
        (folder / "file").rename(outside)
        (folder / "file").symlink_to(outside, target_is_directory=True)
    else:
        (folder / "file/data").rename(outside)
        (folder / "file/data").symlink_to(outside)
    if redirect in ("filedir", "attachment") and command != "file":
        # list/done must not inspect attachments at all.
        expected = 0
    else:
        expected = 2
    args = [command, "development" if command == "list" else item["id"]]
    assert helper.main(args) == expected
    captured = capsys.readouterr()
    if expected:
        assert captured.out == ""
        assert captured.err == "homelab-inbox: operation failed\n"


@pytest.mark.parametrize(
    "path", [None, "../outside", "file/../../outside", "/etc/passwd", "file/.."]
)
def test_no_file_or_escaping_path(inbox, capsys, path):
    _, item = store(inbox, file=path)
    assert helper.main(["file", item["id"]]) == 2
    assert capsys.readouterr().out == ""


def test_failed_atomic_replace_leaves_original(inbox, monkeypatch, capsys):
    folder, item = store(inbox)
    path = folder / "item.json"
    before = path.read_bytes()

    def fail(*args, **kwargs):
        raise OSError("private synthetic content")

    monkeypatch.setattr(helper.os, "replace", fail)
    assert helper.main(["done", item["id"]]) == 2
    assert path.read_bytes() == before
    assert list(folder.iterdir()) == [path]
    assert "private" not in capsys.readouterr().err


def test_malformed_record_error_has_no_content(inbox, capsys):
    folder, _ = store(inbox)
    (folder / "item.json").write_text("private synthetic content")
    assert helper.main(["list", "development"]) == 2
    captured = capsys.readouterr()
    assert captured.out == ""
    assert captured.err == "homelab-inbox: operation failed\n"


def test_concurrent_receipts_keep_first_timestamp(inbox):
    from concurrent.futures import ThreadPoolExecutor

    folder, item = store(inbox)
    with ThreadPoolExecutor(max_workers=8) as pool:
        assert list(pool.map(lambda _: helper.main(["done", item["id"]]), range(16))) == [0] * 16
    path = folder / "item.json"
    content = path.read_bytes()
    assert json.loads(content)["status"] == "read"
    assert helper.main(["done", item["id"]]) == 0
    assert path.read_bytes() == content


def test_bot_displays_read_status(inbox, monkeypatch):
    import importlib
    from types import SimpleNamespace

    monkeypatch.syspath_prepend(str(ROOT / "node/opt/homelab-telegram-bot"))
    bot_inbox = importlib.import_module("inbox")
    folder, item = store(inbox)
    planning = SimpleNamespace(directory=inbox.parent, ready=lambda: True)
    from threading import RLock

    planning.lock = RLock()
    bot = bot_inbox.Inbox(planning)
    assert " — new" in bot.command([])
    assert helper.main(["done", folder.name]) == 0
    assert " — read" in bot.command([])
    assert "status: read" in bot.command([item["id"]])
    assert "read_at:" in bot.command([item["id"]])


@pytest.mark.parametrize("command", ["list", "file", "done"])
def test_hardlinked_record_rejected(inbox, tmp_path, capsys, command):
    folder, item = store(inbox)
    os.link(folder / "item.json", tmp_path / "outside")
    assert helper.main([command, "development" if command == "list" else item["id"]]) == 2
    assert capsys.readouterr().out == ""


def test_nonregular_attachment_rejected_without_blocking(inbox, capsys):
    folder, item = store(inbox, file="file/pipe")
    (folder / "file").mkdir()
    os.mkfifo(folder / "file/pipe")
    assert helper.main(["file", item["id"]]) == 2
    assert capsys.readouterr().out == ""


def test_symlinked_parent_of_inbox_rejected(inbox, tmp_path, monkeypatch, capsys):
    alias = tmp_path / "alias"
    alias.symlink_to(inbox.parent, target_is_directory=True)
    monkeypatch.setattr(helper, "INBOX", alias / "inbox")
    assert helper.main(["list", "development"]) == 2
    assert capsys.readouterr().out == ""


def test_multi_receipt_error_keeps_earlier_commit(inbox, capsys):
    folder, _ = store(inbox)
    assert helper.main(["done", folder.name, "0" * 32]) == 2
    assert json.loads((folder / "item.json").read_text())["status"] == "read"
    assert capsys.readouterr().out == ""
