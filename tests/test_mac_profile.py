import copy
import importlib.machinery
import importlib.util
import json
import os
import subprocess
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[1]


def module(name, filename):
    loader = importlib.machinery.SourceFileLoader(name, str(ROOT / "mac" / filename))
    spec = importlib.util.spec_from_loader(name, loader)
    result = importlib.util.module_from_spec(spec)
    loader.exec_module(result)
    return result


p = module("profile_command", "homelab-profile")
helper = module("profile_helper", "homelab-profile-apply")


@pytest.fixture
def world(monkeypatch, tmp_path):
    monkeypatch.setattr(p, "STATE", tmp_path / "state")
    monkeypatch.setattr(p, "CONFIG", tmp_path / "profiles.json")
    p.STATE.mkdir()
    cfg = {"profiles": copy.deepcopy(p.DEFAULTS), "return_seconds": 15}
    cfg["profiles"]["inference"]["close_apps"] = ["com.apple.TextEdit"]
    live = {"cleanup": True, "voiceink": True, "com.apple.TextEdit": True}
    models = {"Qwen3-Embedding-4B": "unloaded"}
    power = p.DEFAULTS["working"]["settings"].copy()
    monkeypatch.setattr(p, "registered", lambda label: live["cleanup"])
    monkeypatch.setattr(p, "cleanup_running", lambda: live["cleanup"])
    monkeypatch.setattr(
        p, "app_running", lambda app: live.get("voiceink" if app == p.VOICE else app, False)
    )
    monkeypatch.setattr(p, "app", lambda app, start: live.update({app: start}))
    monkeypatch.setattr(p, "service", lambda name, start: live.update({name: start}))
    monkeypatch.setattr(p, "settings", lambda: power.copy())
    monkeypatch.setattr(p, "router_models", lambda: models.copy())
    monkeypatch.setattr(p, "vscode_work", lambda: [])
    monkeypatch.setattr(
        p,
        "set_models",
        lambda allowed: (
            models.clear(),
            models.update({p.MODELS[s]: "unloaded" for s in allowed if s in p.MODELS}),
        ),
    )
    monkeypatch.setattr(
        p,
        "run",
        lambda *args, **kwargs: (
            power.update(cfg["profiles"][args[-1]]["settings"])
            if args[0] == "/usr/bin/sudo"
            else ""
        ),
    )
    return cfg, live, models, power


def test_observed_matching_and_custom(world):
    cfg, live, models, power = world
    assert p.snapshot(cfg)["profile"] == "working"
    for key in ["cleanup", "voiceink"]:
        live[key] = False
        assert p.snapshot(cfg)["profile"] == "custom"
        live[key] = True
    power["gpu_limit_mb"] = 10
    assert p.snapshot(cfg)["profile"] == "custom"
    power["gpu_limit_mb"] = 0
    models["vision"] = "unloaded"
    assert p.snapshot(cfg)["profile"] == "custom"
    del models["vision"]
    assert p.snapshot(cfg)["profile"] == "working"


def test_switch_return_restores_exact_closed_apps_and_services(world):
    cfg, live, models, power = world
    live["unrelated"] = True
    p.switch("inference", cfg)
    assert p.snapshot(cfg)["profile"] == "inference"
    assert not live["cleanup"] and not live["voiceink"]
    assert not live["com.apple.TextEdit"] and live["unrelated"]
    record = p.load(p.STATE / "session.json", {})
    assert record["closed"] == ["com.apple.TextEdit"]
    assert record["stopped"] == ["cleanup", "voiceink"]
    p.switch("working", cfg)
    assert p.snapshot(cfg)["profile"] == "working"
    assert live["com.apple.TextEdit"] and live["unrelated"]
    assert not p.snapshot(cfg)["return_pending"]


def test_return_does_not_open_previously_stopped_service_or_app(world):
    cfg, live, _, _ = world
    live["voiceink"] = False
    live["com.apple.TextEdit"] = False
    p.switch("inference", cfg)
    p.switch("working", cfg)
    assert not live["voiceink"] and not live["com.apple.TextEdit"]
    assert p.snapshot(cfg)["profile"] == "custom"


def test_skip_settings_record_survives_automatic_return(world, monkeypatch):
    cfg, _, _, _ = world
    monkeypatch.setattr(p, "run", lambda *a, **k: pytest.fail("privileged call"))
    p.switch("inference", cfg, skip=True)
    assert p.snapshot(cfg)["profile"] == "custom"
    assert p.snapshot(cfg)["settings_skipped"]
    p.switch("working", cfg, automatic=True)
    assert p.snapshot(cfg)["profile"] == "working"


def test_guard_precedes_all_side_effects(world, monkeypatch, capsys):
    cfg, live, models, power = world
    before = copy.deepcopy((live, models, power))
    monkeypatch.setattr(p, "vscode_work", lambda: [{"pid": 12, "command": "codex exec example"}])
    with pytest.raises(ValueError, match="owner"):
        p.switch("inference", cfg, skip=True)
    assert json.loads(capsys.readouterr().out)["work"][0]["pid"] == 12
    assert (live, models, power) == before
    assert not (p.STATE / "session.json").exists()
    p.switch("inference", cfg, confirm=True, skip=True)
    assert not live["cleanup"]


def test_never_automatic_inference(world):
    with pytest.raises(ValueError, match="forbidden"):
        p.switch("inference", world[0], automatic=True)


def test_vscode_descendants_and_unrelated_work():
    rows = {
        1: (0, "/Applications/Visual Studio Code.app/Contents/MacOS/Electron"),
        2: (1, "Code Helper"),
        3: (2, "/bin/zsh"),
        4: (3, "/bin/codex exec task"),
        5: (3, "/bin/llama-server"),
        6: (0, "/bin/codex exec unrelated"),
        7: (3, "python -m mlx_lm.server"),
        8: (3, "python train.py"),
        9: (0, "unrelated"),
        10: (3, "python scripts/ordinary.py"),
        11: (3, "codex --model example exec task"),
    }
    assert {row["pid"] for row in p.vscode_work(rows)} == {4, 5, 7, 8, 10, 11}


@pytest.mark.parametrize("active", ["loaded", "loading", "sleeping", "unloaded"])
def test_auto_return_clock(world, monkeypatch, active):
    cfg, _, models, _ = world
    p.switch("inference", cfg, skip=True)
    record = p.load(p.STATE / "session.json", {})
    record["last_active"] = 0
    p.save(p.STATE / "session.json", record)
    monkeypatch.setattr(p.time, "time", lambda: 20)
    models["vision"] = active
    p.tick(cfg)
    assert p.snapshot(cfg)["return_pending"] == (active in ("loaded", "loading"))


def test_running_job_prevents_return(world, monkeypatch):
    cfg, _, _, _ = world
    p.switch("inference", cfg, skip=True)
    p.save(p.STATE / "jobs.json", {"example": {"pid": 1}})
    monkeypatch.setattr(p, "job_live", lambda job: True)
    monkeypatch.setattr(p.time, "time", lambda: 10000000000)
    p.tick(cfg)
    assert p.snapshot(cfg)["return_pending"]
    assert p.snapshot(cfg)["running_jobs"] == ["example"]


def test_failed_quit_keeps_recovery_record(world, monkeypatch):
    def failed(*args):
        raise ValueError("quit cancelled")

    monkeypatch.setattr(p, "app", failed)
    with pytest.raises(ValueError):
        p.switch("inference", world[0], skip=True)
    record = p.load(p.STATE / "session.json", {})
    assert record["return_pending"] and record["closed"] == ["com.apple.TextEdit"]


@pytest.mark.parametrize("value", [-1, 45057, "0", True])
def test_untrusted_gpu_setting_refused(value):
    values = {**p.DEFAULTS["working"]["settings"], "gpu_limit_mb": value}
    with pytest.raises(ValueError):
        p.validate_settings(values)


def test_config_editable_private_and_no_app_injection(world):
    p.save(p.CONFIG, {"profiles": {"inference": {"close_apps": ["com.apple.TextEdit"]}}})
    assert p.config()["profiles"]["inference"]["close_apps"] == ["com.apple.TextEdit"]
    assert p.CONFIG.stat().st_mode & 0o777 == 0o600
    p.save(p.CONFIG, {"profiles": {"inference": {"close_apps": ['a" quit']}}})
    with pytest.raises(ValueError):
        p.config()


@pytest.mark.parametrize(
    "profile,power,idle,expected",
    [
        ("working", "AC Power", 301, True),
        ("working", "Battery Power", 301, False),
        ("working", "AC Power", 299, False),
        ("custom", "AC Power", 301, False),
    ],
)
def test_job_gates(world, monkeypatch, profile, power, idle, expected):
    monkeypatch.setattr(p, "snapshot", lambda cfg: {"profile": profile})
    monkeypatch.setattr(p, "run", lambda *a: power)
    monkeypatch.setattr(p, "idle_seconds", lambda: idle)
    assert p.eligible(world[0]) is expected


def test_job_calendar_is_launchd_and_no_shell(world, monkeypatch):
    calls = []
    monkeypatch.setattr(p, "install_agent", lambda *a, **kw: calls.append((a, kw)))
    p.job_add("example", {"Hour": 4, "Minute": 0}, ["/usr/bin/true"])
    assert calls == [
        (
            ("org.homelab.profile.job.example", ["job-worker", "example"]),
            {"StartCalendarInterval": {"Hour": 4, "Minute": 0}},
        )
    ]
    assert not p.jobs()["example"]["pending"]
    for calendar in ({"Hour": 25}, {"Nope": 1}, {}):
        with pytest.raises(ValueError):
            p.job_add("invalid", calendar, ["/usr/bin/true"])
    with pytest.raises(ValueError):
        p.job_add("invalid", {"Minute": 0}, ["true"])


def test_missed_job_runs_next_idle_without_switch(world, monkeypatch):
    cfg = world[0]
    p.save(p.STATE / "jobs.json", {"example": {"command": ["/usr/bin/true"], "pending": False}})
    monkeypatch.setattr(p, "under_vscode", lambda: False)
    monkeypatch.setattr(p, "eligible", lambda cfg: False)
    p.job_worker("example", cfg)
    assert p.jobs()["example"]["pending"]
    monkeypatch.setattr(p, "eligible", lambda cfg: True)
    calls = []
    monkeypatch.setattr(p, "run", lambda *args: calls.append(args))
    p.tick(cfg)
    assert calls == [
        ("/bin/launchctl", "kickstart", f"gui/{os.getuid()}/org.homelab.profile.job.example")
    ]


def test_job_cannot_execute_under_vscode(world, monkeypatch):
    monkeypatch.setattr(p, "under_vscode", lambda: True)
    with pytest.raises(ValueError, match="outside VS Code"):
        p.job_worker("example", world[0])


def test_job_live_checks_pid_identity(monkeypatch):
    monkeypatch.setattr(p, "router", lambda: {"identity": lambda pid: "new"})
    assert not p.job_live({"pid": 12, "identity": "old"})
    assert p.job_live({"pid": 12, "identity": "new"})


def test_idle_nanoseconds(monkeypatch):
    monkeypatch.setattr(p, "run", lambda *a: '"HIDIdleTime" = 310000000000')
    assert p.idle_seconds() == 310


def test_root_helper_exact_commands(world, monkeypatch, tmp_path):
    private = tmp_path / ".config/homelab"
    private.mkdir(parents=True)
    (private / "profiles.json").write_text("{}")
    monkeypatch.setattr(helper.os, "geteuid", lambda: 0)
    monkeypatch.setenv("SUDO_UID", str(os.getuid()))
    monkeypatch.setattr(
        helper.pwd, "getpwuid", lambda uid: type("User", (), {"pw_dir": str(tmp_path)})
    )
    calls = []
    monkeypatch.setattr(helper.subprocess, "run", lambda *a, **kw: calls.append((a, kw)))
    helper.apply("inference")
    assert [call[0][0] for call in calls] == [
        ["/usr/bin/pmset", "-a", "powermode", "2"],
        ["/usr/bin/pmset", "-a", "sleep", "0"],
        ["/usr/sbin/sysctl", "-w", "iogpu.wired_limit_mb=45056"],
    ]
    assert all(call[1]["env"]["PATH"] == "/usr/bin:/bin:/usr/sbin:/sbin" for call in calls)
    with pytest.raises(ValueError):
        helper.apply("inference --anything")
    (private / "profiles.json").write_text(
        json.dumps({"profiles": {"inference": {"settings": {"gpu_limit_mb": "0; id"}}}})
    )
    with pytest.raises(ValueError):
        helper.apply("inference")
    assert len(calls) == 3


def test_root_helper_rejects_symlink(world, monkeypatch, tmp_path):
    private = tmp_path / ".config/homelab"
    private.mkdir(parents=True)
    (private / "profiles.json").symlink_to("/etc/passwd")
    monkeypatch.setattr(helper.os, "geteuid", lambda: 0)
    monkeypatch.setenv("SUDO_UID", str(os.getuid()))
    monkeypatch.setattr(
        helper.pwd, "getpwuid", lambda uid: type("User", (), {"pw_dir": str(tmp_path)})
    )
    with pytest.raises(OSError):
        helper.apply("working")


@pytest.mark.parametrize(
    "args", [[], ["switch", "custom"], ["switch", "inference", "--force"], ["start", "listener"]]
)
def test_cli_rejects_unsupported_arguments(args):
    result = subprocess.run([p.sys.executable, str(p.SCRIPT), *args], capture_output=True)
    assert result.returncode == 2


def test_job_context_cannot_switch_even_with_owner_flag(world, monkeypatch):
    monkeypatch.setenv("HOMELAB_PROFILE_JOB", "1")
    with pytest.raises(ValueError, match="forbidden"):
        p.switch("inference", world[0], confirm=True)


def test_native_reload_verified_and_preserves_allowed_model(monkeypatch, tmp_path):
    calls = []
    monkeypatch.setattr(
        p,
        "router",
        lambda: {
            "STATE": tmp_path,
            "write_presets": lambda: calls.append("render"),
            "record": lambda: {"address": "private"},
            "healthy": lambda saved: True,
            "api": lambda address, path, **kw: calls.append(path),
        },
    )
    monkeypatch.setattr(p, "router_models", lambda: {"Qwen3-Embedding-4B": "sleeping"})
    p.set_models({"embed-large-bulk"})
    assert json.loads((tmp_path / "allowed.json").read_text()) == ["Qwen3-Embedding-4B"]
    assert calls == ["render", "/models?reload=1"]
    monkeypatch.setattr(p, "router_models", lambda: {"vision": "loaded"})
    with pytest.raises(ValueError, match="did not enforce"):
        p.set_models({"embed-large-bulk"})


def test_job_worker_records_exit_and_propagates_context(world, monkeypatch):
    p.save(p.STATE / "jobs.json", {"example": {"command": ["/usr/bin/true"]}})
    monkeypatch.setattr(p, "under_vscode", lambda: False)
    monkeypatch.setattr(p, "eligible", lambda cfg: True)
    monkeypatch.setattr(p, "router", lambda: {"identity": lambda pid: "start"})
    calls = []

    def execute(*args, **kwargs):
        calls.append((args, kwargs))
        return subprocess.CompletedProcess(args, 0)

    monkeypatch.setattr(p.subprocess, "run", execute)
    p.job_worker("example", world[0])
    assert calls[0][1]["env"]["HOMELAB_PROFILE_JOB"] == "1"
    assert p.jobs()["example"]["last_exit"] == 0
    assert p.jobs()["example"]["pid"] is None


@pytest.mark.parametrize(
    "output,expected",
    [
        ("service = {\n\tstate = running\n}", True),
        ("service = {\n\tstate = waiting\n}", False),
        ("", False),
    ],
)
def test_cleanup_state_uses_running_process_not_loaded_agent(monkeypatch, output, expected):
    monkeypatch.setattr(p, "run", lambda *args, **kwargs: output)
    assert p.cleanup_running() is expected
