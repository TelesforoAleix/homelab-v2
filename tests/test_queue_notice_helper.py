import importlib.machinery
import importlib.util
import io
import subprocess
from pathlib import Path
from types import SimpleNamespace

import pytest

ROOT = Path(__file__).resolve().parents[1]
loader = importlib.machinery.SourceFileLoader(
    "queue_notice_helper", str(ROOT / "node/usr/local/sbin/homelab-queue-notice")
)
spec = importlib.util.spec_from_loader(loader.name, loader)
helper = importlib.util.module_from_spec(spec)
loader.exec_module(helper)


def test_text_prefix_controls_and_unicode_cap():
    assert helper.message("job\nexit\r\n\t\x00\x1b\x7f\x85\u202etest") == (
        "Mac queue: job exit  test"
    )
    assert helper.message("--alert bot") == "Mac queue: --alert bot"
    assert helper.message("") == "Mac queue: "
    text = helper.message("猫" * 2000)
    assert len(text) == 1000
    assert text == "Mac queue: " + "猫" * 989


@pytest.fixture
def sender(monkeypatch):
    calls = []
    monkeypatch.setattr(helper.sys, "stdin", io.StringIO("test\nnotice\x00"))
    monkeypatch.setattr(helper.os, "geteuid", lambda: 0)

    def run(argv, **kwargs):
        calls.append((argv, kwargs))
        return SimpleNamespace(returncode=0)

    monkeypatch.setattr(helper.subprocess, "run", run)
    return calls


def test_transient_unit_fixed_credential_no_content_in_arguments(sender):
    assert helper.main([]) == 0
    argv, kwargs = sender[0]
    assert argv[0] == "/usr/bin/systemd-run"
    assert "--pipe" in argv and "--wait" in argv and "--collect" in argv
    assert "--property=User=homelab-bot" in argv
    assert "--property=LoadCredential=bot-token:/etc/homelab-telegram-bot/token" in argv
    assert "--property=RuntimeMaxSec=8" in argv
    assert argv[-2:] == [helper.HELPER, "--send"]
    assert "test" not in " ".join(argv)
    assert kwargs["input"] == "test\nnotice\x00"
    assert kwargs["timeout"] == 10
    assert kwargs["stderr"] == subprocess.DEVNULL


def test_send_uses_shared_path_with_systemd_credential(sender, monkeypatch):
    monkeypatch.setenv("CREDENTIALS_DIRECTORY", "/run/credentials/test")
    assert helper.main(["--send"]) == 0
    argv, kwargs = sender[0]
    assert argv == ["/usr/local/sbin/homelab-notify.sh", "Mac queue: test notice"]
    assert kwargs["timeout"] == 7
    assert kwargs["stdout"] == kwargs["stderr"] == subprocess.DEVNULL


def test_reject_outside_systemd_or_nonroot_and_unknown_arguments(sender, monkeypatch):
    monkeypatch.delenv("CREDENTIALS_DIRECTORY", raising=False)
    assert helper.main(["--send"]) == 2
    monkeypatch.setattr(helper.os, "geteuid", lambda: 994)
    assert helper.main([]) == 2
    assert helper.main(["--bad"]) == 2
    assert sender == []


@pytest.mark.parametrize("failure", ["timeout", "exit", "spawn"])
def test_failed_send_not_retried(sender, monkeypatch, failure):
    calls = []

    def fail(argv, **kwargs):
        calls.append(argv)
        if failure == "timeout":
            raise subprocess.TimeoutExpired(argv, 10)
        if failure == "spawn":
            raise OSError
        return SimpleNamespace(returncode=1)

    monkeypatch.setattr(helper.subprocess, "run", fail)
    assert helper.main([]) == 1
    assert len(calls) == 1
