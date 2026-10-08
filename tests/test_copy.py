import importlib.machinery
import importlib.util
import io
import json
import tarfile
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[1]


def load(name, path):
    loader = importlib.machinery.SourceFileLoader(name, str(ROOT / path))
    spec = importlib.util.spec_from_loader(name, loader)
    module = importlib.util.module_from_spec(spec)
    loader.exec_module(module)
    return module


node = load("node_copy", "node/usr/local/sbin/homelab-copy")
mac = load("mac_copy", "mac/homelab-copy")


def test_stream_takes_only_originals_and_counts_output(tmp_path, monkeypatch):
    for project in ["alpha", "beta"]:
        folder = tmp_path / f"{project}-data/originals"
        folder.mkdir(parents=True)
        (folder / "input").write_bytes(b"original")
    (tmp_path / "alpha-data/sources").mkdir()
    (tmp_path / "alpha-data/sources/book").write_bytes(b"excluded")
    age = tmp_path / "age"
    # Synthetic encryptor tests plumbing without needing age or a private identity.
    age.write_text('#!/bin/sh\nprintf "age-encryption.org/v1\\n"\ncat\n')
    age.chmod(0o755)
    monkeypatch.setenv("PATH", f"{tmp_path}:{node.os.environ['PATH']}")
    monkeypatch.setattr(node, "ROOT", tmp_path)
    monkeypatch.setattr(node.os.path, "ismount", lambda path: True)
    monkeypatch.setenv("COPYFILE_DISABLE", "1")
    output, report = io.BytesIO(), io.StringIO()
    node.stream(output, report)
    result = json.loads(report.getvalue())
    assert result["bytes"] == len(output.getvalue())
    assert [item["folder"] for item in result["manifest"]] == [
        "alpha-data/originals",
        "beta-data/originals",
    ]
    archive = output.getvalue().split(b"\n", 1)[1]
    with tarfile.open(fileobj=io.BytesIO(archive)) as handle:
        assert sorted(handle.getnames()) == [
            "alpha-data/originals",
            "alpha-data/originals/input",
            "beta-data/originals",
            "beta-data/originals/input",
        ]


def test_stream_failure_does_not_report_success(tmp_path, monkeypatch):
    (tmp_path / "alpha-data/originals").mkdir(parents=True)
    age = tmp_path / "age"
    age.write_text("#!/bin/sh\ncat >/dev/null\nexit 1\n")
    age.chmod(0o755)
    monkeypatch.setenv("PATH", f"{tmp_path}:{node.os.environ['PATH']}")
    monkeypatch.setattr(node, "ROOT", tmp_path)
    monkeypatch.setattr(node.os.path, "ismount", lambda path: True)
    report = io.StringIO()
    with pytest.raises(ValueError, match="tar or age"):
        node.stream(io.BytesIO(), report)
    assert report.getvalue() == ""


@pytest.mark.parametrize("locked,symlink", [(True, False), (False, True)])
def test_stream_rejects_locked_volume_or_redirected_originals(
    tmp_path, monkeypatch, locked, symlink
):
    monkeypatch.setattr(node, "ROOT", tmp_path)
    monkeypatch.setattr(node.os.path, "ismount", lambda path: not locked)
    if symlink:
        (tmp_path / "alpha-data").mkdir()
        (tmp_path / "outside").mkdir()
        (tmp_path / "alpha-data/originals").symlink_to(tmp_path / "outside")
    with pytest.raises(ValueError):
        node.stream(io.BytesIO(), io.StringIO())


def test_confirm_status_and_reminder_boundary(tmp_path, monkeypatch, capsys):
    monkeypatch.setattr(node, "STATE", tmp_path / "state/last-good.json")
    monkeypatch.setattr(node.time, "time", lambda: 1_000_000)
    messages = []
    monkeypatch.setattr(node.subprocess, "run", lambda args, **kw: messages.append(args))
    node.status(True)
    assert len(messages) == 1
    assert "no copy yet" in messages[0][1]
    node.confirm(1234)
    assert node.STATE.stat().st_mode & 0o777 == 0o600
    assert node.STATE.parent.stat().st_mode & 0o777 == 0o700
    node.status()
    assert json.loads(capsys.readouterr().out) == {"timestamp": 1_000_000, "bytes": 1234}
    node.status(True)
    assert len(messages) == 1
    monkeypatch.setattr(node.time, "time", lambda: 1_000_000 + 7 * 86400)
    node.status(True)
    assert len(messages) == 1
    monkeypatch.setattr(node.time, "time", lambda: 1_000_000 + 8 * 86400)
    node.status(True)
    assert messages[-1][1] == (
        "weekly copy is 8 days old; plug in the card and run `mac/homelab-copy`"
    )
    before = node.STATE.read_bytes()
    with pytest.raises(ValueError):
        node.confirm(0)
    assert node.STATE.read_bytes() == before


@pytest.mark.parametrize("path", ["/", "/tmp", "/Volumes/Card/subfolder"])
def test_mac_rejects_non_volume_path(path):
    with pytest.raises(ValueError):
        mac.volume_path(path)


@pytest.mark.parametrize("mounted,internal", [(False, False), (True, True), (True, False)])
def test_mac_checks_mount_and_disk_info(monkeypatch, mounted, internal):
    monkeypatch.setattr(mac.os.path, "ismount", lambda path: mounted)
    monkeypatch.setattr(
        mac.subprocess,
        "check_output",
        lambda args: mac.plistlib.dumps(
            {"DeviceIdentifier": "disk0s1", "APFSContainerReference": "disk0"}
            if args[-1] == "/"
            else {
                "Internal": internal,
                "RemovableMediaOrExternalDevice": not internal,
                "MountPoint": "/Volumes/Card",
            }
        ),
    )
    if mounted and not internal:
        assert mac.volume_path("Card") == Path("/Volumes/Card")
    else:
        with pytest.raises(ValueError):
            mac.volume_path("Card")


@pytest.mark.parametrize(
    "flag", ["Removable", "RemovableMedia", "RemovableMediaOrExternalDevice", "Ejectable"]
)
def test_mac_accepts_builtin_sd_reader(monkeypatch, flag):
    monkeypatch.setattr(mac.os.path, "ismount", lambda path: True)
    monkeypatch.setattr(
        mac.subprocess,
        "check_output",
        lambda args: mac.plistlib.dumps(
            {"DeviceIdentifier": "disk3s1s1", "APFSContainerReference": "disk3"}
            if args[-1] == "/"
            else {
                "Internal": True,
                flag: True,
                "MountPoint": "/Volumes/Card",
                "DeviceIdentifier": "disk7s1",
                "FilesystemType": "exfat",
            }
        ),
    )
    assert mac.volume_path("Card") == Path("/Volumes/Card")


@pytest.mark.parametrize(
    "identity",
    [
        {"DeviceIdentifier": "disk3s1s1"},
        {"VolumeUUID": "startup-uuid"},
        {"APFSContainerReference": "disk3", "FilesystemType": "apfs"},
    ],
)
def test_mac_refuses_startup_volume_and_boot_container(monkeypatch, identity):
    monkeypatch.setattr(mac.os.path, "ismount", lambda path: True)
    monkeypatch.setattr(
        mac.subprocess,
        "check_output",
        lambda args: mac.plistlib.dumps(
            {
                "DeviceIdentifier": "disk3s1s1",
                "VolumeUUID": "startup-uuid",
                "APFSContainerReference": "disk3",
            }
            if args[-1] == "/"
            else {"MountPoint": "/Volumes/Card", "Ejectable": True, **identity}
        ),
    )
    with pytest.raises(ValueError, match="startup volume"):
        mac.volume_path("Card")


def test_mac_refuses_fixed_internal_disk(monkeypatch):
    monkeypatch.setattr(mac.os.path, "ismount", lambda path: True)
    monkeypatch.setattr(
        mac.subprocess,
        "check_output",
        lambda args: mac.plistlib.dumps(
            {"DeviceIdentifier": "disk3s1s1", "APFSContainerReference": "disk3"}
            if args[-1] == "/"
            else {
                "Internal": True,
                "Ejectable": False,
                "RemovableMedia": False,
                "RemovableMediaOrExternalDevice": False,
                "DeviceIdentifier": "disk8s1",
                "MountPoint": "/Volumes/Card",
            }
        ),
    )
    with pytest.raises(ValueError, match="removable or ejectable"):
        mac.volume_path("Card")


@pytest.mark.parametrize(
    "reported,returncode,confirmed", [(4, 0, True), (5, 0, False), (4, 1, False)]
)
def test_mac_confirms_only_complete_matching_copy(
    tmp_path, monkeypatch, reported, returncode, confirmed
):
    calls = []

    class Stream:
        def __init__(self, args, stdout, **kw):
            calls.append(args)
            stdout.write(b"copy")
            self.returncode = returncode

        def communicate(self):
            report = {
                "bytes": reported,
                "manifest": [{"folder": "alpha-data/originals", "bytes": 8}],
            }
            return None, json.dumps(report).encode()

        def poll(self):
            return self.returncode

    monkeypatch.setattr(mac.subprocess, "Popen", Stream)
    monkeypatch.setattr(mac.subprocess, "run", lambda args, **kw: calls.append(args))
    if confirmed:
        destination = mac.copy(tmp_path)
        assert destination.read_bytes() == b"copy"
        assert calls[-1][-2:] == ["confirm", "4"]
        with pytest.raises(FileExistsError):
            mac.copy(tmp_path)
        assert len(calls) == 2
    else:
        with pytest.raises(ValueError):
            mac.copy(tmp_path)
        assert len(calls) == 1


def test_reminder_timer_and_credential_contract():
    service = (ROOT / "node/etc/systemd/system/homelab-copy-reminder.service").read_text()
    timer = (ROOT / "node/etc/systemd/system/homelab-copy-reminder.timer").read_text()
    assert "LoadCredentialEncrypted=bot-token:/etc/homelab-telegram-bot/token.cred" in service
    assert "ExecStart=/usr/local/sbin/homelab-copy status --remind" in service
    assert "OnCalendar=daily" in timer
    assert "Persistent=true" in timer
    assert "WantedBy=timers.target" in timer
