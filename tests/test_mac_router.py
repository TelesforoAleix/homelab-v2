import importlib.machinery
import importlib.util
import subprocess
import sys
from pathlib import Path

import pytest

SCRIPT = Path(__file__).resolve().parents[1] / "mac/homelab-router"
loader = importlib.machinery.SourceFileLoader("mac_router", str(SCRIPT))
spec = importlib.util.spec_from_loader(loader.name, loader)
router = importlib.util.module_from_spec(spec)
loader.exec_module(router)


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
        str(router.ipaddress.IPv4Address(0x64800000)),
    ],
)
def test_rejects_non_tailnet_addresses(address):
    with pytest.raises(ValueError):
        router.check_address(address)


def test_accepts_only_tailnet_range():
    for number in [0x64400001, 0x647FFFFE]:
        address = str(router.ipaddress.IPv4Address(number))
        assert router.check_address(address + "\n") == address


def test_tailscale_cli_address(monkeypatch):
    address = str(router.ipaddress.IPv4Address(0x64400001))
    monkeypatch.setattr(router.shutil, "which", lambda name: "/cli")
    monkeypatch.setattr(
        router.subprocess, "run", lambda *a, **kw: subprocess.CompletedProcess(a, 0, address)
    )
    assert router.tailscale_address() == address


def test_down_tailscale_refused(monkeypatch):
    monkeypatch.setattr(
        router.subprocess, "run", lambda *a, **kw: subprocess.CompletedProcess(a, 1, "")
    )
    with pytest.raises(ValueError, match="down"):
        router.tailscale_address()


def test_utun_fallback_ignores_wifi(monkeypatch):
    address = str(router.ipaddress.IPv4Address(0x64400001))

    def run(args, **kw):
        return subprocess.CompletedProcess(
            args,
            0 if args == ["/sbin/ifconfig"] else 1,
            f"en0: flags\n\tinet 192.168.1.1\nutun4: flags\n\tinet {address}\n",
        )

    monkeypatch.setattr(router.subprocess, "run", run)
    assert router.tailscale_address() == address


def setup_paths(monkeypatch, tmp_path):
    state = tmp_path / "state"
    state.mkdir(mode=0o700)
    monkeypatch.setattr(router, "STATE", state)
    monkeypatch.setattr(router, "PLIST", tmp_path / "LaunchAgents/router.plist")
    monkeypatch.setattr(router, "VISION", tmp_path / "vision")
    monkeypatch.setattr(router, "EMBED", tmp_path / "embed")
    router.VISION.mkdir()
    router.EMBED.mkdir()
    for path in (
        router.VISION / "Qwen3-VL-8B-Instruct-Q4_K_M.gguf",
        router.VISION / "mmproj-F16.gguf",
        router.EMBED / "Qwen3-Embedding-4B-Q8_0.gguf",
    ):
        path.touch()
    return state


def test_presets_preserve_both_models_settings(monkeypatch, tmp_path):
    state = setup_paths(monkeypatch, tmp_path)
    router.write_presets()
    text = (state / "models.ini").read_text()
    import configparser

    config = configparser.ConfigParser()
    config.read_string(text[text.index("[*]") :])
    assert dict(config["vision"]) == {
        "model": str(router.VISION / "Qwen3-VL-8B-Instruct-Q4_K_M.gguf"),
        "mmproj": str(router.VISION / "mmproj-F16.gguf"),
        "ngl": "99",
        "c": "16384",
        "np": "1",
        "cache-ram": "0",
        "flash-attn": "on",
    }
    assert dict(config["Qwen3-Embedding-4B"]) == {
        "model": str(router.EMBED / "Qwen3-Embedding-4B-Q8_0.gguf"),
        "ngl": "99",
        "embeddings": "true",
        "pooling": "last",
        "embd-normalize": "2",
        "c": "8192",
        "b": "2048",
        "ub": "2048",
        "t": "4",
    }
    assert dict(config["*"]) == {"load-on-startup": "false", "log-disable": "true"}
    assert "@" not in text


def test_missing_weights_refuse_install(monkeypatch, tmp_path):
    setup_paths(monkeypatch, tmp_path)
    (router.VISION / "mmproj-F16.gguf").unlink()
    monkeypatch.setattr(router, "launchctl", lambda *a: pytest.fail("installed"))
    with pytest.raises(ValueError, match="missing"):
        router.write_presets()


def test_serve_executes_native_router_without_environment_overrides(monkeypatch, tmp_path):
    state = setup_paths(monkeypatch, tmp_path)
    address = str(router.ipaddress.IPv4Address(0x64400001))
    monkeypatch.setattr(router, "tailscale_address", lambda: address)
    monkeypatch.setattr(router, "identity", lambda pid: "start")
    monkeypatch.setenv("LLAMA_ARG_HOST", "0.0.0.0")
    monkeypatch.setenv("LLAMA_ARG_LOG_PROMPTS", "true")
    calls = []
    monkeypatch.setattr(router.os, "execve", lambda *a: calls.append(a))
    router.serve()
    binary, args, env = calls[0]
    assert binary == router.BINARY
    assert args == [
        binary,
        "--models-preset",
        str(state / "models.ini"),
        "--models-autoload",
        "--sleep-idle-seconds",
        "600",
        "--host",
        address,
        "--port",
        "8090",
        "--log-disable",
    ]
    assert not any(key.startswith("LLAMA_ARG_") for key in env)
    assert env["LLAMA_CACHE"] == str(state / "cache")
    assert router.record()["identity"] == "start"
    assert router.record()["pid"] == router.os.getpid()


def test_start_installs_login_agent_without_private_values_in_output(monkeypatch, tmp_path, capsys):
    setup_paths(monkeypatch, tmp_path)
    address = "127.0.0.1"
    monkeypatch.setattr(router, "tailscale_address", lambda: address)
    monkeypatch.setattr(router, "PORT", 0)
    checks = iter([False, True])
    monkeypatch.setattr(router, "healthy", lambda saved: next(checks))
    calls = []

    def launch(*args):
        calls.append(args)
        return subprocess.CompletedProcess(args, 1 if args[0] == "print" else 0)

    monkeypatch.setattr(router, "launchctl", launch)
    router.start()
    settings = router.plistlib.loads(router.PLIST.read_bytes())
    assert settings["ProgramArguments"] == [sys.executable, str(router.SCRIPT), "serve"]
    assert settings["RunAtLoad"] and settings["KeepAlive"]
    assert settings["Umask"] == 0o077
    assert settings["StandardOutPath"] == settings["StandardErrorPath"] == "/dev/null"
    assert calls[-1] == ("bootstrap", f"gui/{router.os.getuid()}", str(router.PLIST))
    output = capsys.readouterr().out
    assert address not in output and str(tmp_path) not in output


def test_start_refuses_occupied_port(monkeypatch):
    monkeypatch.setattr(router, "healthy", lambda saved: False)
    monkeypatch.setattr(router, "record", lambda: None)
    monkeypatch.setattr(router, "tailscale_address", lambda: "127.0.0.1")
    with router.socket.socket() as listener:
        listener.bind(("127.0.0.1", 0))
        listener.listen()
        monkeypatch.setattr(router, "PORT", listener.getsockname()[1])
        with pytest.raises(ValueError, match="taken"):
            router.start()


def test_stop_does_not_signal_reused_pid_and_removes_login_agent(monkeypatch, tmp_path):
    state = setup_paths(monkeypatch, tmp_path)
    (state / "server.json").write_text('{"pid":42,"identity":"old"}')
    router.PLIST.parent.mkdir()
    router.PLIST.touch()
    monkeypatch.setattr(router, "identity", lambda pid: "new")
    monkeypatch.setattr(router.os, "kill", lambda *a: pytest.fail("signalled reused pid"))
    monkeypatch.setattr(router, "launchctl", lambda *a: subprocess.CompletedProcess(a, 1))
    router.stop()
    assert not router.PLIST.exists()
    assert not (state / "server.json").exists()


def test_stop_retains_installation_on_launchctl_failure(monkeypatch, tmp_path):
    setup_paths(monkeypatch, tmp_path)
    router.PLIST.parent.mkdir()
    router.PLIST.touch()
    monkeypatch.setattr(router, "running", lambda saved: True)
    monkeypatch.setattr(router, "launchctl", lambda *a: subprocess.CompletedProcess(a, 1))
    with pytest.raises(ValueError, match="retained"):
        router.stop()
    assert router.PLIST.exists()


def test_status_does_not_wake_models_or_expose_upstream_metadata(monkeypatch, capsys):
    monkeypatch.setattr(router, "record", lambda: {"pid": 42, "address": "private"})
    monkeypatch.setattr(router, "healthy", lambda saved: True)
    paths = []

    def api(address, path):
        paths.append(path)
        return {
            "data": [
                {"id": "vision", "status": {"value": "sleeping", "args": ["private"]}},
                {"id": "Qwen3-Embedding-4B", "status": {"value": "unloaded"}, "path": "private"},
            ]
        }

    monkeypatch.setattr(router, "api", api)
    monkeypatch.setattr(router, "memory", lambda pid: {"family_physical_footprint_bytes": 123})
    assert router.status() == 0
    assert paths == ["/models"]
    output = capsys.readouterr().out
    assert "private" not in output
    assert '"sleeping"' in output and '"unloaded"' in output and "123" in output


def test_memory_counts_router_and_descendants_only(monkeypatch):
    monkeypatch.setattr(
        router.subprocess,
        "run",
        lambda *a, **k: subprocess.CompletedProcess(
            a, 0, "10 1 100\n11 10 200\n12 11 300\n13 1 400\n"
        ),
    )
    monkeypatch.setattr(router, "footprint", lambda pid: pid * 1000)
    assert router.memory(10) == {
        "router_rss_bytes": 100 * 1024,
        "family_rss_bytes": 600 * 1024,
        "family_physical_footprint_bytes": 33000,
    }


def test_status_creates_private_state(monkeypatch, tmp_path):
    monkeypatch.setattr(router, "STATE", tmp_path / "state")
    monkeypatch.setattr(sys, "argv", [str(SCRIPT), "status"])
    previous = router.os.umask(0o077)
    try:
        assert router.main() == 1
    finally:
        router.os.umask(previous)
    assert router.STATE.stat().st_mode & 0o777 == 0o700
    assert (router.STATE / "command.lock").stat().st_mode & 0o777 == 0o600


def test_start_refuses_live_legacy_before_install(monkeypatch, tmp_path):
    setup_paths(monkeypatch, tmp_path)
    monkeypatch.setattr(router.Path, "home", lambda: tmp_path)
    old = tmp_path / ".local/state/homelab-embed"
    old.mkdir(parents=True)
    (old / "server.json").write_text('{"pid":42,"identity":"start"}')
    monkeypatch.setattr(router, "identity", lambda pid: "start")
    monkeypatch.setattr(router, "healthy", lambda saved: False)
    monkeypatch.setattr(router, "launchctl", lambda *a: pytest.fail("installed"))
    with pytest.raises(ValueError, match="legacy"):
        router.start()


def test_failed_start_unloads_agent(monkeypatch, tmp_path):
    setup_paths(monkeypatch, tmp_path)
    monkeypatch.setattr(router, "tailscale_address", lambda: "127.0.0.1")
    monkeypatch.setattr(router, "PORT", 0)
    monkeypatch.setattr(router, "healthy", lambda saved: False)
    monkeypatch.setattr(
        router, "launchctl", lambda *a: subprocess.CompletedProcess(a, 1 if a[0] == "print" else 0)
    )
    times = iter([0, 121])
    monkeypatch.setattr(router.time, "monotonic", lambda: next(times))
    stopped = []
    monkeypatch.setattr(router, "stop", lambda: stopped.append(True))
    with pytest.raises(ValueError, match="health check"):
        router.start()
    assert stopped == [True]


@pytest.mark.parametrize("available", ["/usr/local/bin/tailscale", "/opt/homebrew/bin/tailscale"])
def test_address_discovery_with_launchd_minimal_path(monkeypatch, available):
    monkeypatch.setenv("PATH", "/usr/bin:/bin:/usr/sbin:/sbin")
    monkeypatch.setattr(router.shutil, "which", lambda name: None)
    address = str(router.ipaddress.IPv4Address(0x64400001))
    calls = []

    def run(args, **kwargs):
        calls.append(args)
        if args[0] == available:
            return subprocess.CompletedProcess(args, 0, address)
        assert args[0] == "/usr/local/bin/tailscale"
        raise FileNotFoundError("CLI missing")

    monkeypatch.setattr(router.subprocess, "run", run)
    assert router.tailscale_address() == address
    assert calls[-1] == [available, "ip", "-4"]


def test_invalid_cli_output_falls_through_to_next_cli(monkeypatch):
    address = str(router.ipaddress.IPv4Address(0x64400001))

    def run(args, **kwargs):
        output = address if args[0] == "/opt/homebrew/bin/tailscale" else "GUI failed"
        return subprocess.CompletedProcess(args, 0, output)

    monkeypatch.setattr(router.subprocess, "run", run)
    assert router.tailscale_address() == address


@pytest.mark.parametrize("stage", ["address", "presets", "exec"])
def test_failed_agent_start_records_only_fixed_stage(monkeypatch, tmp_path, capsys, stage):
    setup_paths(monkeypatch, tmp_path)
    address = str(router.ipaddress.IPv4Address(0x64400001))
    monkeypatch.setattr(router, "tailscale_address", lambda: address)
    monkeypatch.setattr(router, "identity", lambda pid: "start")
    monkeypatch.setattr(sys, "argv", [str(SCRIPT), "serve"])
    private_diagnostic = f"{address} {tmp_path} arbitrary upstream output"

    def fail(*args, **kwargs):
        raise ValueError(private_diagnostic)

    target, message = {
        "address": ("tailscale_address", "tailscale address unavailable"),
        "presets": ("write_presets", "model preset preparation failed"),
        "exec": ("execve", "router executable launch failed"),
    }[stage]
    monkeypatch.setattr(router.os if stage == "exec" else router, target, fail)
    previous = router.os.umask(0o077)
    try:
        with pytest.raises(SystemExit) as error:
            router.main()
    finally:
        router.os.umask(previous)
    assert error.value.code == 1
    path = router.STATE / "startup-failures.log"
    assert path.read_text() == message + "\n"
    assert path.stat().st_mode & 0o777 == 0o600
    captured = capsys.readouterr()
    assert private_diagnostic not in captured.out + captured.err


def test_manual_start_failure_is_recorded_and_survives_stop(monkeypatch, tmp_path):
    setup_paths(monkeypatch, tmp_path)
    monkeypatch.setattr(sys, "argv", [str(SCRIPT), "start"])

    def fail():
        raise router.StartupError("router health check timed out")

    monkeypatch.setattr(router, "start", fail)
    previous = router.os.umask(0o077)
    try:
        with pytest.raises(SystemExit):
            router.main()
    finally:
        router.os.umask(previous)
    monkeypatch.setattr(router, "launchctl", lambda *a: subprocess.CompletedProcess(a, 1))
    router.stop()
    assert (router.STATE / "startup-failures.log").read_text() == "router health check timed out\n"


def test_allowlist_omits_disallowed_and_future_presets(monkeypatch, tmp_path):
    state = setup_paths(monkeypatch, tmp_path)
    (state / "allowed.json").write_text('["Qwen3-Embedding-4B"]')
    router.write_presets()
    text = (state / "models.ini").read_text()
    assert "[vision]" not in text and "mmproj" not in text
    assert "[Qwen3-Embedding-4B]" in text
    assert "ngl = 99" in text and "embd-normalize = 2" in text
    (state / "allowed.json").write_text("[]")
    router.write_presets()
    text = (state / "models.ini").read_text()
    assert "[vision]" not in text and "[Qwen3-Embedding-4B]" not in text
