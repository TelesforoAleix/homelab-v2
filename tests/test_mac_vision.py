import importlib.machinery
import importlib.util
import subprocess
import sys
from pathlib import Path

import pytest

SCRIPT = Path(__file__).resolve().parents[1] / "mac/homelab-vision"
loader = importlib.machinery.SourceFileLoader("mac_vision", str(SCRIPT))
spec = importlib.util.spec_from_loader(loader.name, loader)
vision = importlib.util.module_from_spec(spec)
loader.exec_module(vision)


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
        str(vision.ipaddress.IPv4Address(0x64800000)),
    ],
)
def test_rejects_non_tailnet_addresses(address):
    with pytest.raises(ValueError):
        vision.check_address(address)


def test_accepts_only_tailnet_range():
    for number in [0x64400001, 0x647FFFFE]:
        address = str(vision.ipaddress.IPv4Address(number))
        assert vision.check_address(address + "\n") == address


def test_tailscale_cli_address(monkeypatch):
    address = str(vision.ipaddress.IPv4Address(0x64400001))
    monkeypatch.setattr(vision.shutil, "which", lambda name: "/cli")
    monkeypatch.setattr(
        vision.subprocess, "run", lambda *a, **kw: subprocess.CompletedProcess(a, 0, address)
    )
    assert vision.tailscale_address() == address


def test_down_tailscale_refused(monkeypatch):
    monkeypatch.setattr(
        vision.subprocess, "run", lambda *a, **kw: subprocess.CompletedProcess(a, 1, "")
    )
    with pytest.raises(ValueError, match="down"):
        vision.tailscale_address()


def test_utun_fallback_ignores_wifi(monkeypatch):
    address = str(vision.ipaddress.IPv4Address(0x64400001))

    def run(args, **kw):
        return subprocess.CompletedProcess(
            args,
            0 if args == ["/sbin/ifconfig"] else 1,
            f"en0: flags\n\tinet 192.168.1.1\nutun4: flags\n\tinet {address}\n",
        )

    monkeypatch.setattr(vision.subprocess, "run", run)
    assert vision.tailscale_address() == address


def test_stop_does_not_signal_reused_pid(monkeypatch, tmp_path):
    monkeypatch.setattr(vision, "STATE", tmp_path)
    monkeypatch.setattr(vision, "identity", lambda pid: "new process")
    monkeypatch.setattr(vision.os, "kill", lambda *args: pytest.fail("signalled reused PID"))
    vision.stop({"pid": 42, "identity": "old process"})


def test_start_refuses_taken_port(monkeypatch):
    monkeypatch.setattr(vision, "tailscale_address", lambda: "127.0.0.1")
    with vision.socket.socket() as listener:
        listener.bind(("127.0.0.1", 0))
        listener.listen()
        monkeypatch.setattr(vision, "PORT", listener.getsockname()[1])
        with pytest.raises(ValueError, match="taken"):
            vision.start(None)


def test_launch_matches_measured_settings_and_keeps_state_private(monkeypatch, tmp_path):
    monkeypatch.setattr(vision, "STATE", tmp_path)
    monkeypatch.setattr(vision, "MODELS", tmp_path)
    for filename in ["Qwen3-VL-8B-Instruct-Q4_K_M.gguf", "mmproj-F16.gguf"]:
        (tmp_path / filename).touch()
    address = str(vision.ipaddress.IPv4Address(0x64400001))
    monkeypatch.setattr(vision, "tailscale_address", lambda: address)
    monkeypatch.setattr(vision, "identity", lambda pid: "process start")
    monkeypatch.setattr(vision, "healthy", lambda addr: addr == address)
    monkeypatch.setenv("LLAMA_ARG_HOST", "0.0.0.0")
    launches = []

    class Probe:
        def __enter__(self):
            return self

        def __exit__(self, *args):
            pass

        def bind(self, target):
            assert target == (address, 8090)

    class Process:
        pid = 42

        def poll(self):
            return None

    def launch(args, **kwargs):
        launches.append((args, kwargs))
        return Process()

    monkeypatch.setattr(vision.socket, "socket", Probe)
    monkeypatch.setattr(vision.subprocess, "Popen", launch)
    vision.start(None)
    args, kwargs = launches[0]
    for flag, value in [
        ("--host", address),
        ("--port", "8090"),
        ("-ngl", "99"),
        ("-c", "16384"),
        ("-np", "1"),
        ("--cache-ram", "0"),
        ("--flash-attn", "on"),
        ("--alias", "vision"),
    ]:
        assert args[args.index(flag) + 1] == value
    assert "--log-disable" in args
    assert "LLAMA_ARG_HOST" not in kwargs["env"]
    assert kwargs["start_new_session"] is True
    assert (tmp_path / "server.json").is_file()
