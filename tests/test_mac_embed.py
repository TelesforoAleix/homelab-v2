import importlib.machinery
import importlib.util
import subprocess
import sys
from pathlib import Path

import pytest

SCRIPT = Path(__file__).resolve().parents[1] / "mac/homelab-embed"
loader = importlib.machinery.SourceFileLoader("mac_embed", str(SCRIPT))
spec = importlib.util.spec_from_loader(loader.name, loader)
embed = importlib.util.module_from_spec(spec)
loader.exec_module(embed)


@pytest.mark.parametrize("args", [[], ["restart"], ["start", "extra"]])
def test_argument_handling(args):
    result = subprocess.run([sys.executable, str(SCRIPT), *args], capture_output=True)
    assert result.returncode == 2
    assert b"usage:" in result.stderr


@pytest.mark.parametrize(
    "address",
    [
        "127.0.0.1",
        "0.0.0.0",
        "192.168.1.1",
        "::1",
        "",
        "not-an-address",
        str(embed.ipaddress.IPv4Address(0x64800000)),
    ],
)
def test_rejects_non_tailnet_addresses(address):
    with pytest.raises(ValueError):
        embed.check_address(address)


def test_accepts_only_tailnet_range():
    for number in [0x64400001, 0x647FFFFE]:
        address = str(embed.ipaddress.IPv4Address(number))
        assert embed.check_address(address + "\n") == address


def test_tailscale_cli_address(monkeypatch):
    address = str(embed.ipaddress.IPv4Address(0x64400001))
    monkeypatch.setattr(embed.shutil, "which", lambda name: "/cli")
    monkeypatch.setattr(
        embed.subprocess, "run", lambda *a, **kw: subprocess.CompletedProcess(a, 0, address)
    )
    assert embed.tailscale_address() == address


def test_down_tailscale_refused(monkeypatch):
    monkeypatch.setattr(
        embed.subprocess, "run", lambda *a, **kw: subprocess.CompletedProcess(a, 1, "")
    )
    with pytest.raises(ValueError, match="down"):
        embed.tailscale_address()


def test_utun_fallback_ignores_wifi(monkeypatch):
    address = str(embed.ipaddress.IPv4Address(0x64400001))

    def run(args, **kw):
        return subprocess.CompletedProcess(
            args,
            0 if args == ["/sbin/ifconfig"] else 1,
            f"en0: flags\n\tinet 192.168.1.1\nutun4: flags\n\tinet {address}\n",
        )

    monkeypatch.setattr(embed.subprocess, "run", run)
    assert embed.tailscale_address() == address


def test_stop_does_not_signal_reused_pid(monkeypatch, tmp_path):
    monkeypatch.setattr(embed, "STATE", tmp_path)
    monkeypatch.setattr(embed, "identity", lambda pid: "new process")
    monkeypatch.setattr(embed.os, "kill", lambda *args: pytest.fail("signalled reused PID"))
    embed.stop({"pid": 42, "identity": "old process"})


def test_start_refuses_taken_port(monkeypatch):
    monkeypatch.setattr(embed, "tailscale_address", lambda: "127.0.0.1")
    with embed.socket.socket() as listener:
        listener.bind(("127.0.0.1", 0))
        listener.listen()
        monkeypatch.setattr(embed, "PORT", listener.getsockname()[1])
        with pytest.raises(ValueError, match="taken"):
            embed.start(None)


def test_launch_matches_measured_settings_and_keeps_state_private(monkeypatch, tmp_path):
    monkeypatch.setattr(embed, "STATE", tmp_path)
    monkeypatch.setattr(embed, "MODELS", tmp_path)
    for filename in ["Qwen3-Embedding-4B-Q8_0.gguf"]:
        (tmp_path / filename).touch()
    address = str(embed.ipaddress.IPv4Address(0x64400001))
    monkeypatch.setattr(embed, "tailscale_address", lambda: address)
    monkeypatch.setattr(embed, "identity", lambda pid: "process start")
    monkeypatch.setattr(embed, "healthy", lambda addr: addr == address)
    monkeypatch.setenv("LLAMA_ARG_HOST", "0.0.0.0")
    launches = []

    class Probe:
        def __enter__(self):
            return self

        def __exit__(self, *args):
            pass

        def bind(self, target):
            assert target == (address, 8092)

    class Process:
        pid = 42

        def poll(self):
            return None

    def launch(args, **kwargs):
        launches.append((args, kwargs))
        return Process()

    monkeypatch.setattr(embed.socket, "socket", Probe)
    monkeypatch.setattr(embed.subprocess, "Popen", launch)
    embed.start(None)
    args, kwargs = launches[0]
    for flag, value in [
        ("--host", address),
        ("--port", "8092"),
        ("-ngl", "99"),
        ("-c", "8192"),
        ("-b", "2048"),
        ("-ub", "2048"),
        ("-t", "4"),
        ("--pooling", "last"),
        ("--embd-normalize", "2"),
        ("--alias", "Qwen3-Embedding-4B"),
    ]:
        assert args[args.index(flag) + 1] == value
    assert "--embeddings" in args
    assert "--log-disable" in args
    assert "LLAMA_ARG_HOST" not in kwargs["env"]
    assert kwargs["start_new_session"] is True
    assert (tmp_path / "server.json").is_file()


def test_start_refuses_missing_model_without_launch(monkeypatch, tmp_path):
    monkeypatch.setattr(embed, "MODELS", tmp_path)
    monkeypatch.setattr(embed, "tailscale_address", lambda: "127.0.0.1")
    monkeypatch.setattr(embed, "PORT", 0)
    monkeypatch.setattr(embed.subprocess, "Popen", lambda *a, **kw: pytest.fail("launched"))
    with pytest.raises(ValueError, match="model missing"):
        embed.start(None)


def test_failed_start_stops_child(monkeypatch, tmp_path):
    monkeypatch.setattr(embed, "STATE", tmp_path)
    monkeypatch.setattr(embed, "MODELS", tmp_path)
    (tmp_path / "Qwen3-Embedding-4B-Q8_0.gguf").touch()
    monkeypatch.setattr(embed, "tailscale_address", lambda: "127.0.0.1")
    monkeypatch.setattr(embed, "PORT", 0)
    monkeypatch.setattr(embed, "identity", lambda pid: "start time")

    class FailedProcess:
        pid = 42

        def poll(self):
            return 1

    monkeypatch.setattr(embed.subprocess, "Popen", lambda *a, **kw: FailedProcess())
    stopped = []
    monkeypatch.setattr(embed, "stop", stopped.append)
    with pytest.raises(ValueError, match="did not become healthy"):
        embed.start(None)
    assert stopped == [{"pid": 42, "identity": "start time", "address": "127.0.0.1"}]


def test_status_creates_private_state(monkeypatch, tmp_path, capsys):
    state = tmp_path / "state"
    monkeypatch.setattr(embed, "STATE", state)
    monkeypatch.setattr(sys, "argv", [str(SCRIPT), "status"])
    previous_umask = embed.os.umask(0o077)
    try:
        assert embed.main() == 1
    finally:
        embed.os.umask(previous_umask)
    assert state.stat().st_mode & 0o777 == 0o700
    assert (state / "command.lock").stat().st_mode & 0o777 == 0o600
    assert "not serving" in capsys.readouterr().out
