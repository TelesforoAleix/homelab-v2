import copy
import importlib.machinery
import importlib.util
import json
import os
import subprocess
import sys
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
REAL_SERVICE = p.service


@pytest.fixture
def world(monkeypatch, tmp_path):
    monkeypatch.setattr(p, "STATE", tmp_path / "state")
    monkeypatch.setattr(p, "CONFIG", tmp_path / "profiles.json")
    monkeypatch.setattr(p, "SCHEDULE", tmp_path / "night-window.json")
    p.STATE.mkdir()
    cfg = {"profiles": copy.deepcopy(p.DEFAULTS), "return_seconds": 15}
    cfg["profiles"]["inference"]["close_apps"] = ["com.apple.TextEdit"]
    live = {"cleanup": True, "voiceink": True, "com.apple.TextEdit": True}
    models = {"Qwen3-Embedding-4B": "unloaded"}
    power = {"powermode": 1, "sleep": 10, "gpu_limit_mb": 0}
    monkeypatch.setattr(p, "on_ac_power", lambda: True)
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
    monkeypatch.setattr(p, "processes", lambda: {})
    monkeypatch.setattr(
        p,
        "set_models",
        lambda allowed, **kwargs: (
            models.clear(),
            models.update({p.MODELS[s]: "unloaded" for s in allowed if s in p.MODELS}),
        ),
    )

    def run(*args, **kwargs):
        if args[0] == "/usr/bin/sudo":
            name = args[-1]
            power.update(cfg["profiles"][name]["settings"])
            record = p.load(p.STATE / "session.json", {})
            if name == "working" and record.get("return_pending"):
                power.update({key: record["previous_" + key] for key in power})
        return ""

    monkeypatch.setattr(p, "run", run)
    return cfg, live, models, power


def test_observed_matching_and_custom(world):
    cfg, live, models, power = world
    assert p.snapshot(cfg)["profile"] == "working"
    for key in ["cleanup", "voiceink"]:
        live[key] = False
        assert p.snapshot(cfg)["profile"] == "custom"
        live[key] = True
    power["gpu_limit_mb"] = 10
    assert p.snapshot(cfg)["profile"] == "working"
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
    monkeypatch.setattr(p, "on_ac_power", lambda: power == "AC Power")
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

    def execute(*args, **kwargs):
        calls.append((args, kwargs))
        return subprocess.CompletedProcess(args, 0, "Now drawing from 'AC Power'\n")

    monkeypatch.setattr(helper.subprocess, "run", execute)
    state = tmp_path / ".local/state/homelab-profile"
    state.mkdir(parents=True)
    (state / "session.json").write_text(
        json.dumps(
            {
                "return_pending": True,
                "previous_powermode": 1,
                "previous_sleep": 10,
                "previous_gpu_limit_mb": 0,
            }
        )
    )
    helper.apply("inference")
    assert [call[0][0] for call in calls] == [
        ["/usr/bin/pmset", "-g", "batt"],
        ["/usr/bin/pmset", "-c", "powermode", "2"],
        ["/usr/bin/pmset", "-c", "sleep", "0"],
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
    assert len(calls) == 4


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
    result = subprocess.run([sys.executable, str(p.SCRIPT), *args], capture_output=True)
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


def test_worker_ancestry_prevents_switch_when_environment_is_stripped(world, monkeypatch):
    monkeypatch.delenv("HOMELAB_PROFILE_JOB", raising=False)
    monkeypatch.setattr(
        p,
        "processes",
        lambda: {
            os.getpid(): (20, "/usr/bin/python homelab-profile switch inference --confirm-kill"),
            20: (10, "/usr/bin/env -i"),
            10: (1, "/usr/bin/python /private/checkout/mac/homelab-profile job-worker example"),
        },
    )
    with pytest.raises(ValueError, match="forbidden"):
        p.switch("inference", world[0], confirm=True)


@pytest.mark.parametrize("mode", [0, 1, 2])
def test_working_ignores_owner_power_mode_and_legacy_setting(world, mode):
    cfg, _, _, power = world
    power["powermode"] = mode
    cfg["profiles"]["working"]["settings"]["powermode"] = 1
    assert p.snapshot(cfg)["profile"] == "working"


@pytest.mark.parametrize("mode", [0, 1, 2])
def test_ordinary_working_switch_preserves_owner_power_mode(world, mode):
    cfg, _, _, power = world
    power["powermode"] = mode
    p.switch("working", cfg)
    assert power["powermode"] == mode
    assert p.snapshot(cfg)["profile"] == "working"


def test_legacy_private_working_power_mode_removed_from_resolved_config(world):
    p.save(
        p.CONFIG,
        {
            "profiles": {
                "working": {
                    "settings": {
                        "powermode": 1,
                        "sleep": 1,
                        "gpu_limit_mb": 0,
                    }
                }
            }
        },
    )
    assert p.config()["profiles"]["working"]["settings"] == {}
    assert p.load(p.CONFIG, {})["profiles"]["working"]["settings"]["powermode"] == 1


@pytest.mark.parametrize("mode", [0, 1, 2])
def test_idle_return_restores_recorded_power_mode(world, monkeypatch, mode):
    cfg, _, _, power = world
    power.update(powermode=mode, sleep=17, gpu_limit_mb=16384)
    before = power.copy()
    p.switch("inference", cfg)
    assert power["powermode"] == 2
    record = p.load(p.STATE / "session.json", {})
    assert record["previous_powermode"] == mode
    monkeypatch.setattr(p.time, "time", lambda: record["last_active"] + 20)
    p.tick(cfg)
    assert power == before
    assert p.snapshot(cfg)["profile"] == "working"
    assert not p.snapshot(cfg)["return_pending"]


def test_battery_refusal_is_clear_and_precedes_side_effects(world, monkeypatch, capsys):
    cfg, live, models, power = world
    before = copy.deepcopy((live, models, power))
    monkeypatch.setattr(p, "on_ac_power", lambda: False)
    monkeypatch.setattr(sys, "argv", [str(p.SCRIPT), "switch", "inference", "--confirm-kill"])
    with pytest.raises(SystemExit) as exc:
        p.main()
    assert exc.value.code == 1
    assert "Inference requires AC power; connect the charger" in capsys.readouterr().err
    assert (live, models, power) == before
    assert not (p.STATE / "session.json").exists()


def test_short_unplug_keeps_inference_and_closed_apps(world, monkeypatch):
    cfg, live, models, power = world
    p.switch("inference", cfg)
    models["vision"] = "loaded"
    p.save(p.STATE / "jobs.json", {"example": {"pid": 1}})
    monkeypatch.setattr(p, "job_live", lambda record: True)
    monkeypatch.setattr(p, "on_ac_power", lambda: False)
    assert p.snapshot(cfg)["profile"] == "inference"
    p.tick(cfg)
    assert power["powermode"] == 2
    assert not live["cleanup"] and not live["voiceink"] and not live["com.apple.TextEdit"]
    assert p.snapshot(cfg)["profile"] == "inference"
    assert p.snapshot(cfg)["return_pending"]
    assert not p.snapshot(cfg)["paused"]


def test_failed_power_restoration_retains_recovery_record(world, monkeypatch):
    cfg, _, _, power = world
    p.switch("inference", cfg)
    monkeypatch.setattr(
        p, "run", lambda *args: power.update(cfg["profiles"]["working"]["settings"])
    )
    with pytest.raises(ValueError, match="previous settings were not restored"):
        p.switch("working", cfg)
    assert p.load(p.STATE / "session.json", {})["previous_powermode"] == 1
    assert p.snapshot(cfg)["return_pending"]


@pytest.mark.parametrize("source,expected", [("AC Power", True), ("Battery Power", False)])
def test_power_source_parsed_from_pmset(monkeypatch, source, expected):
    calls = []

    def run(*args):
        calls.append(args)
        return f"Now drawing from '{source}'\n -InternalBattery-0 100%; charged;\n"

    monkeypatch.setattr(p, "run", run)
    assert p.on_ac_power() is expected
    assert calls == [("/usr/bin/pmset", "-g", "batt")]


def test_unknown_power_source_refuses_inference(monkeypatch):
    monkeypatch.setattr(p, "run", lambda *args: "unrecognised power report")
    with pytest.raises(p.ProfileRefusal, match="Cannot determine power source"):
        p.on_ac_power()


@pytest.fixture
def root_world(monkeypatch, tmp_path):
    private = tmp_path / ".config/homelab"
    private.mkdir(parents=True)
    config_path = private / "profiles.json"
    config_path.write_text("{}")
    state = tmp_path / ".local/state/homelab-profile"
    state.mkdir(parents=True)
    monkeypatch.setattr(helper.os, "geteuid", lambda: 0)
    monkeypatch.setenv("SUDO_UID", str(os.getuid()))
    monkeypatch.setattr(
        helper.pwd, "getpwuid", lambda uid: type("User", (), {"pw_dir": str(tmp_path)})
    )
    calls = []

    def execute(argv, **kwargs):
        calls.append(argv)
        output = (
            "0\n" if argv[:2] == ["/usr/sbin/sysctl", "-n"] else "Now drawing from 'AC Power'\n"
        )
        return subprocess.CompletedProcess(argv, 0, output)

    monkeypatch.setattr(helper.subprocess, "run", execute)
    session = state / "session.json"
    session.write_text(
        json.dumps(
            {
                "return_pending": True,
                "previous_powermode": 1,
                "previous_sleep": 10,
                "previous_gpu_limit_mb": 12345,
            }
        )
    )
    return config_path, session, calls


@pytest.mark.parametrize("legacy_mode", [None, 1, 2])
def test_root_working_preserves_owner_settings_without_return(root_world, legacy_mode):
    config_path, session, calls = root_world
    session.unlink()
    settings = {"sleep": 1, "gpu_limit_mb": 0}
    if legacy_mode is not None:
        settings["powermode"] = legacy_mode
    config_path.write_text(json.dumps({"profiles": {"working": {"settings": settings}}}))
    helper.apply("working")
    assert calls == [["/usr/sbin/sysctl", "-n", "iogpu.wired_limit_mb"]]


@pytest.mark.parametrize("mode", [0, 1, 2])
@pytest.mark.parametrize("sleep,gpu", [(0, 0), (180, 45056), (15, 12345)])
def test_root_return_restores_exact_bounded_ac_settings_and_gpu(root_world, mode, sleep, gpu):
    _, session, calls = root_world
    session.write_text(
        json.dumps(
            {
                "return_pending": True,
                "previous_powermode": mode,
                "previous_sleep": sleep,
                "previous_gpu_limit_mb": gpu,
            }
        )
    )
    helper.apply("working")
    assert calls == [
        ["/usr/bin/pmset", "-c", "powermode", str(mode)],
        ["/usr/bin/pmset", "-c", "sleep", str(sleep)],
        ["/usr/sbin/sysctl", "-w", "iogpu.wired_limit_mb=" + str(gpu)],
    ]


@pytest.mark.parametrize(
    "field,value",
    [
        ("powermode", -1),
        ("powermode", 3),
        ("powermode", True),
        ("powermode", "2"),
        ("powermode", None),
        ("sleep", -1),
        ("sleep", 181),
        ("sleep", True),
        ("sleep", None),
        ("gpu_limit_mb", -1),
        ("gpu_limit_mb", 45057),
        ("gpu_limit_mb", "0; id"),
        ("gpu_limit_mb", None),
    ],
)
def test_root_rejects_invalid_record_before_settings(root_world, field, value):
    _, session, calls = root_world
    record = json.loads(session.read_text())
    record["previous_" + field] = value
    session.write_text(json.dumps(record))
    with pytest.raises(ValueError):
        helper.apply("working")
    assert not calls


def test_root_does_not_follow_recovery_symlink(root_world):
    config_path, session, calls = root_world
    session.unlink()
    session.symlink_to(config_path)
    with pytest.raises(OSError):
        helper.apply("working")
    assert not calls


def test_root_rechecks_ac_before_applying_inference(root_world, monkeypatch):
    _, _, calls = root_world

    def execute(argv, **kwargs):
        calls.append(argv)
        return subprocess.CompletedProcess(argv, 0, "Now drawing from 'Battery Power'\n")

    monkeypatch.setattr(helper.subprocess, "run", execute)
    with pytest.raises(ValueError, match="Inference requires AC power"):
        helper.apply("inference")
    assert calls == [["/usr/bin/pmset", "-g", "batt"]]


def test_inference_requires_high_power_in_private_configuration(world, root_world):
    config_path, _, calls = root_world
    values = {**p.DEFAULTS["inference"]["settings"], "powermode": 1}
    p.save(p.CONFIG, {"profiles": {"inference": {"settings": values}}})
    with pytest.raises(ValueError):
        p.config()
    config_path.write_text(json.dumps({"profiles": {"inference": {"settings": values}}}))
    with pytest.raises(ValueError, match="Inference requires High Power"):
        helper.apply("inference")
    assert not calls


@pytest.fixture
def inference_session(world, monkeypatch):
    cfg, live, models, power = world
    cfg["return_seconds"] = 900
    clock = {"now": 1000, "ac": True}
    monkeypatch.setattr(p.time, "time", lambda: clock["now"])
    monkeypatch.setattr(p, "on_ac_power", lambda: clock["ac"])
    p.switch("inference", cfg)
    return cfg, live, models, power, clock


def pause_session(session):
    cfg, _, models, _, clock = session
    models["vision"] = "loaded"
    clock["ac"] = False
    p.tick(cfg)
    clock["now"] += 600
    p.tick(cfg)


def test_battery_pause_waits_full_ten_minutes_and_keeps_recovery(inference_session):
    cfg, live, models, power, clock = inference_session
    original = p.load(p.STATE / "session.json", {})
    models["vision"] = "loaded"
    clock["ac"] = False
    power["powermode"] = 1  # macOS/owner may select a battery-specific mode.
    p.tick(cfg)
    clock["now"] += 599
    p.tick(cfg)
    assert "vision" in models and not p.snapshot(cfg)["paused"]
    clock["now"] += 1
    p.tick(cfg)
    status = p.snapshot(cfg)
    assert status["profile"] == "inference" and status["paused"]
    assert status["pause_reason"] == "on_battery"
    assert not models
    assert not any(live.values())
    assert all(not status["services"][name]["allowed"] for name in p.MODELS)
    record = p.load(p.STATE / "session.json", {})
    for key in ("previous_powermode", "closed", "stopped"):
        assert record[key] == original[key]


def test_short_replug_resets_continuous_battery_timer(inference_session):
    cfg, _, models, _, clock = inference_session
    models["vision"] = "loaded"
    clock["ac"] = False
    p.tick(cfg)
    clock.update(now=1500, ac=True)
    p.tick(cfg)
    assert "battery_since" not in p.load(p.STATE / "session.json", {})
    clock.update(now=1600, ac=False)
    p.tick(cfg)
    clock["now"] = 2100
    p.tick(cfg)
    assert not p.snapshot(cfg)["paused"]
    clock["now"] = 2200
    p.tick(cfg)
    assert p.snapshot(cfg)["paused"]


def test_pause_disables_idle_return_until_power_or_owner(inference_session):
    cfg, live, models, _, clock = inference_session
    pause_session(inference_session)
    clock["now"] += 100000
    p.tick(cfg)
    assert p.snapshot(cfg)["profile"] == "inference"
    assert p.snapshot(cfg)["paused"] and p.snapshot(cfg)["return_pending"]
    assert not models and not any(live.values())


def test_resume_restores_high_power_before_models_and_restarts_idle_clock(
    inference_session, monkeypatch
):
    cfg, live, models, power, clock = inference_session
    pause_session(inference_session)
    power["powermode"] = 1
    native_set = p.set_models
    calls = []

    def set_models(allowed, **kwargs):
        assert power["powermode"] == 2
        calls.append(set(allowed))
        native_set(allowed, **kwargs)

    monkeypatch.setattr(p, "set_models", set_models)
    clock.update(now=10000, ac=True)
    p.tick(cfg)
    status = p.snapshot(cfg)
    assert status["profile"] == "inference" and not status["paused"]
    assert status["pause_reason"] is None
    assert calls == [set(cfg["profiles"]["inference"]["services"])]
    assert not any(live.values())
    record = p.load(p.STATE / "session.json", {})
    assert record["previous_powermode"] == 1 and record["last_active"] == 10000
    assert "battery_since" not in record
    clock["now"] += 899
    p.tick(cfg)
    assert p.snapshot(cfg)["profile"] == "inference"
    clock["now"] += 1
    p.tick(cfg)
    assert p.snapshot(cfg)["profile"] == "working"
    assert power["powermode"] == 1 and all(live.values())


def test_owner_can_return_to_working_while_paused_on_battery(inference_session):
    cfg, live, _, power, _ = inference_session
    pause_session(inference_session)
    p.switch("working", cfg)
    status = p.snapshot(cfg)
    assert status["profile"] == "working" and not status["paused"]
    assert not status["return_pending"]
    assert power["powermode"] == 1 and all(live.values())


def test_manual_heavy_starts_cannot_bypass_pause(inference_session):
    pause_session(inference_session)
    for name in p.MODELS:
        with pytest.raises(p.ProfileRefusal, match="paused because the Mac is on battery"):
            REAL_SERVICE(name, True)
        REAL_SERVICE(name, False)  # Already stopped; must not erase the pause publication.
    assert not inference_session[2]


def test_paused_jobs_stay_blocked_even_after_replug_before_resume(world, monkeypatch):
    monkeypatch.setattr(p, "snapshot", lambda cfg: {"profile": "inference", "paused": True})
    monkeypatch.setattr(p, "idle_seconds", lambda: 10000)
    assert not p.eligible(world[0])


def test_failed_resume_keeps_models_blocked_and_recovery(inference_session, monkeypatch):
    cfg, live, models, _, clock = inference_session
    pause_session(inference_session)
    clock["ac"] = True

    def fail(*args):
        raise ValueError("settings application failed")

    monkeypatch.setattr(p, "run", fail)
    with pytest.raises(ValueError):
        p.tick(cfg)
    assert not models and not any(live.values())
    assert p.snapshot(cfg)["paused"] and p.snapshot(cfg)["return_pending"]


def test_failed_pause_reload_is_retried(inference_session, monkeypatch):
    cfg, _, _, _, clock = inference_session
    clock["ac"] = False
    p.tick(cfg)
    clock["now"] += 600
    native_set = p.set_models

    def fail(*args, **kwargs):
        raise ValueError("reload failed")

    monkeypatch.setattr(p, "set_models", fail)
    with pytest.raises(ValueError):
        p.tick(cfg)
    assert p.load(p.STATE / "session.json", {})["paused"]
    monkeypatch.setattr(p, "set_models", native_set)
    p.tick(cfg)
    assert not inference_session[2] and p.snapshot(cfg)["paused"]


def test_idle_return_still_applies_during_battery_grace(inference_session):
    cfg, live, _, _, clock = inference_session
    clock.update(now=1850, ac=False)
    p.tick(cfg)
    assert p.snapshot(cfg)["return_pending"]
    clock["now"] = 1900
    p.tick(cfg)
    assert p.snapshot(cfg)["profile"] == "working" and all(live.values())


def test_pause_publication_survives_reload_failures(monkeypatch, tmp_path):
    def api(*args, **kwargs):
        assert json.loads((tmp_path / "public/homelab-profile.json").read_text()) == {
            "paused_on_battery": True,
        }
        raise ValueError("reload failed")

    monkeypatch.setattr(
        p,
        "router",
        lambda: {
            "STATE": tmp_path,
            "write_presets": lambda: None,
            "record": lambda: {"address": "private"},
            "healthy": lambda saved: True,
            "api": api,
        },
    )
    with pytest.raises(ValueError):
        p.set_models(set(), paused=True)
    assert json.loads((tmp_path / "allowed.json").read_text()) == []
    # A failed resume must not erase the pause reason while models remain unavailable.
    with pytest.raises(ValueError):
        p.set_models({"vision", "embed-large-bulk"})
    assert json.loads((tmp_path / "public/homelab-profile.json").read_text()) == {
        "paused_on_battery": True,
    }


def test_failed_resume_reload_restores_empty_policy(inference_session, monkeypatch):
    cfg, _, models, _, clock = inference_session
    pause_session(inference_session)
    native_set = p.set_models
    calls = []

    def set_models(allowed, **kwargs):
        calls.append((set(allowed), kwargs))
        if allowed:
            # Simulate a partial reload followed by failed verification.
            native_set(allowed, **kwargs)
            raise ValueError("verification failed")
        native_set(allowed, **kwargs)

    monkeypatch.setattr(p, "set_models", set_models)
    clock["ac"] = True
    with pytest.raises(ValueError):
        p.tick(cfg)
    assert calls[-1] == (set(), {"paused": True})
    assert not models and p.snapshot(cfg)["paused"]


def test_failed_owner_return_is_retried_instead_of_resuming_inference(
    inference_session, monkeypatch
):
    cfg, live, models, power, clock = inference_session
    pause_session(inference_session)
    original_run = p.run

    def fail(*args, **kwargs):
        raise ValueError("settings restoration failed")

    monkeypatch.setattr(p, "run", fail)
    with pytest.raises(ValueError):
        p.switch("working", cfg)
    assert p.load(p.STATE / "session.json", {})["return_requested"]
    monkeypatch.setattr(p, "run", original_run)
    clock["ac"] = True
    p.tick(cfg)
    assert p.snapshot(cfg)["profile"] == "working"
    assert not p.snapshot(cfg)["paused"] and not p.snapshot(cfg)["return_pending"]
    assert power["powermode"] == 1 and all(live.values())
    assert "vision" not in models


@pytest.mark.parametrize("ac_sleep", [0, 10, 180])
def test_settings_reads_ac_even_when_battery_source_has_different_values(monkeypatch, ac_sleep):
    custom = (
        "Battery Power:\n powermode 1\n sleep 1\n displaysleep 2\n"
        f"AC Power:\n powermode 2\n sleep {ac_sleep}\n displaysleep 10\n"
    )
    calls = []

    def run(*args):
        calls.append(args)
        return custom if args[0] == "/usr/bin/pmset" else "12345\n"

    monkeypatch.setattr(p, "run", run)
    assert p.settings() == {"powermode": 2, "sleep": ac_sleep, "gpu_limit_mb": 12345}
    assert calls[0] == ("/usr/bin/pmset", "-g", "custom")


def test_missing_ac_settings_cannot_capture_battery_as_recovery(monkeypatch):
    monkeypatch.setattr(p, "run", lambda *args: "Battery Power:\n powermode 1\n sleep 1\n")
    with pytest.raises(ValueError, match="AC power settings unavailable"):
        p.settings()


def test_root_inference_requires_complete_recovery_before_writes(root_world):
    _, session, calls = root_world
    session.unlink()
    with pytest.raises(ValueError, match="requires recorded AC settings"):
        helper.apply("inference")
    assert not calls


@pytest.mark.parametrize("pending", [None, False])
def test_root_working_clears_only_orphan_gpu_limit(root_world, monkeypatch, pending):
    _, session, calls = root_world
    if pending is None:
        session.unlink()
    else:
        session.write_text(json.dumps({"return_pending": pending}))

    def execute(argv, **kwargs):
        calls.append(argv)
        return subprocess.CompletedProcess(argv, 0, "45056\n")

    monkeypatch.setattr(helper.subprocess, "run", execute)
    helper.apply("working")
    assert calls == [
        ["/usr/sbin/sysctl", "-n", "iogpu.wired_limit_mb"],
        ["/usr/sbin/sysctl", "-w", "iogpu.wired_limit_mb=0"],
    ]


@pytest.mark.parametrize("field", ["sleep", "gpu_limit_mb"])
def test_failed_exact_return_keeps_record_for_retry(world, monkeypatch, field):
    cfg, _, _, power = world
    p.switch("inference", cfg)
    original = p.load(p.STATE / "session.json", {})

    def incomplete(*args):
        power.update({key: original["previous_" + key] for key in power if key != field})
        power[field] = 99

    monkeypatch.setattr(p, "run", incomplete)
    with pytest.raises(ValueError, match="previous settings were not restored"):
        p.switch("working", cfg)
    retained = p.load(p.STATE / "session.json", {})
    assert retained["return_pending"] and retained["return_requested"]
    assert retained["previous_" + field] == original["previous_" + field]


@pytest.mark.parametrize("failure", [None, "inference", "working"])
def test_privileged_proof_cleans_up_without_app_switches(world, monkeypatch, tmp_path, failure):
    proof_path = ROOT / "tests/mac_profile_privileged_acceptance.py"
    spec = importlib.util.spec_from_file_location("privileged_proof_test", proof_path)
    proof = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(proof)
    monkeypatch.setattr(proof, "p", p)
    monkeypatch.setattr(proof, "BACKUP", p.STATE / "proof-backup.json")
    monkeypatch.setattr(proof.os, "geteuid", lambda: 501)
    monkeypatch.setattr(p, "AGENTS", tmp_path / "agents")
    p.AGENTS.mkdir()
    (p.AGENTS / "org.homelab.profile.plist").write_text("test")
    _, live, _, power = world
    power.update(powermode=2, sleep=0, gpu_limit_mb=12345)
    original_power = power.copy()
    original_live = live.copy()
    session = p.STATE / "session.json"
    p.save(session, {"skip_settings": True, "owner_marker": "preserve"})
    original_session = session.read_bytes()
    events = []

    class InstalledHelper:
        def read_bytes(self):
            return (ROOT / "mac/homelab-profile-apply").read_bytes()

        def stat(self):
            return type("Info", (), {"st_uid": 0, "st_mode": 0o755})()

        def __str__(self):
            return "/usr/local/sbin/homelab-profile-apply"

    monkeypatch.setattr(proof, "HELPER", InstalledHelper())

    def sources():
        return {
            "AC Power": {
                "powermode": power["powermode"],
                "sleep": power["sleep"],
                "displaysleep": 10,
            },
            "Battery Power": {"powermode": 1, "sleep": 1, "displaysleep": 2},
        }

    monkeypatch.setattr(proof, "custom_settings", sources)

    def run(*args):
        events.append(args)
        if args[0] == "/usr/bin/sudo":
            name = args[-1]
            if name == "inference":
                power.update(powermode=2, sleep=0, gpu_limit_mb=45056)
            if name == failure:
                raise RuntimeError("simulated helper failure")
            if name == "working":
                record = p.load(session, {})
                power.update({key: record["previous_" + key] for key in power})
        return ""

    monkeypatch.setattr(p, "run", run)
    monkeypatch.setattr(
        proof.subprocess, "run", lambda *a, **k: subprocess.CompletedProcess(a, 1, "", "refused")
    )
    if failure:
        with pytest.raises(RuntimeError, match="simulated helper failure"):
            proof.main()
    else:
        proof.main()
    assert live == original_live
    restarted = any(args[:2] == ("/bin/launchctl", "bootstrap") for args in events)
    if failure == "working":
        assert not restarted and proof.BACKUP.exists()
        assert p.load(session, {})["return_pending"]
    else:
        assert restarted and not proof.BACKUP.exists()
        assert session.read_bytes() == original_session
        assert power == original_power


def local(*args):
    return p.datetime(*args).astimezone()


MONDAY = local(2026, 10, 19, 22, 0)


@pytest.fixture
def night(world):
    cfg = world[0]
    p.save(p.SCHEDULE, {"enabled": True, "start": "22:00", "end": "07:00", "days": [0]})
    return cfg


def night_state():
    return p.load(p.STATE / "night-window.json", {})


def test_night_window_is_off_by_default(world):
    assert p.schedule_report() == {**p.SCHEDULE_DEFAULT, "last": None}
    p.schedule_tick(world[0], MONDAY)
    assert p.snapshot(world[0])["profile"] == "working"
    assert not (p.STATE / "night-window.json").exists()


def test_night_window_enters_from_working_through_the_guarded_switch(night, world):
    live = world[1]
    p.schedule_tick(night, MONDAY + p.timedelta(minutes=1))
    assert not live["cleanup"] and not live["com.apple.TextEdit"]
    assert p.load(p.STATE / "session.json", {})["return_pending"]
    assert night_state()["owned"] and night_state()["result"] == "entered"
    assert night_state()["night"] == "2026-10-19"


@pytest.mark.parametrize(
    "condition, reason",
    [
        ("custom", "not working"),
        ("battery", "on battery"),
        ("work", "work running"),
        ("job", "job context"),
        ("late", "missed start"),
    ],
)
def test_night_window_skips_the_whole_night(night, world, monkeypatch, condition, reason):
    cfg, live, models, power = world
    now = MONDAY
    if condition == "custom":
        live["cleanup"] = False
    elif condition == "battery":
        monkeypatch.setattr(p, "on_ac_power", lambda: False)
    elif condition == "work":
        monkeypatch.setattr(p, "vscode_work", lambda: [{"pid": 7, "command": "codex exec"}])
    elif condition == "job":
        monkeypatch.setenv("HOMELAB_PROFILE_JOB", "1")
    else:
        now = MONDAY + p.timedelta(minutes=6)
    before = copy.deepcopy((live, models, power))
    p.schedule_tick(cfg, now)
    assert (live, models, power) == before
    assert night_state() == {
        "night": "2026-10-19",
        "owned": False,
        "end": local(2026, 10, 20, 7, 0).isoformat(),
        "result": "skipped: " + reason,
    }
    # Conditions clearing later that night do not enter it.
    live["cleanup"] = True
    monkeypatch.setattr(p, "on_ac_power", lambda: True)
    monkeypatch.setattr(p, "vscode_work", lambda: [])
    monkeypatch.delenv("HOMELAB_PROFILE_JOB", raising=False)
    p.schedule_tick(cfg, MONDAY + p.timedelta(minutes=2))
    assert p.snapshot(cfg)["profile"] == "working"


def test_night_window_honours_days_and_window(night, world):
    cfg = world[0]
    for now in (
        local(2026, 10, 20, 22, 0),  # Tuesday: not selected
        local(2026, 10, 19, 21, 59),  # before start
        local(2026, 10, 19, 7, 0),  # Monday morning belongs to Sunday's night
    ):
        p.schedule_tick(cfg, now)
        assert p.snapshot(cfg)["profile"] == "working"
    assert not (p.STATE / "night-window.json").exists()


def test_night_window_returns_at_end_only_while_it_owns_the_session(night, world):
    cfg, live = world[0], world[1]
    p.schedule_tick(cfg, MONDAY)
    p.schedule_tick(cfg, local(2026, 10, 20, 6, 59))
    assert p.load(p.STATE / "session.json", {})["return_pending"]
    p.schedule_tick(cfg, local(2026, 10, 20, 7, 0))
    assert not p.load(p.STATE / "session.json", {}).get("return_pending")
    assert live["cleanup"] and live["com.apple.TextEdit"]
    assert night_state()["owned"] is False


@pytest.mark.parametrize("action", ["switch", "service"])
def test_manual_action_ends_ownership_without_reentry(night, world, monkeypatch, action):
    cfg = world[0]
    p.schedule_tick(cfg, MONDAY)
    if action == "switch":
        p.switch("working", cfg, skip=True)
        p.switch("inference", cfg, skip=True)
    else:
        monkeypatch.setattr(sys, "argv", [str(p.SCRIPT), "stop", "embed-large-bulk"])
        p.main()
    assert night_state()["owned"] is False
    p.schedule_tick(cfg, local(2026, 10, 20, 7, 0))
    assert p.load(p.STATE / "session.json", {})["return_pending"]


def test_manual_working_return_is_not_reentered_that_night(night, world):
    cfg = world[0]
    p.schedule_tick(cfg, MONDAY)
    p.switch("working", cfg, skip=True)
    p.schedule_tick(cfg, MONDAY + p.timedelta(minutes=1))
    assert p.snapshot(cfg)["profile"] == "working"


def test_scheduled_switch_keeps_every_guard(world, monkeypatch, capsys):
    cfg = world[0]
    monkeypatch.setattr(p, "vscode_work", lambda: [{"pid": 7, "command": "codex exec"}])
    with pytest.raises(ValueError, match="owner"):
        p.switch("inference", cfg, scheduled=True)
    assert "refused" in capsys.readouterr().out
    monkeypatch.setattr(p, "vscode_work", lambda: [])
    monkeypatch.setattr(p, "on_ac_power", lambda: False)
    with pytest.raises(p.ProfileRefusal, match="AC power"):
        p.switch("inference", cfg, scheduled=True)
    monkeypatch.setattr(p, "on_ac_power", lambda: True)
    monkeypatch.setenv("HOMELAB_PROFILE_JOB", "1")
    with pytest.raises(ValueError, match="forbidden"):
        p.switch("inference", cfg, scheduled=True)
    assert not (p.STATE / "session.json").exists()


def test_schedule_set_validates_and_never_enters_the_current_window(world, monkeypatch):
    value = p.schedule_set("22:00", "07:00", "mon,sun", now=MONDAY + p.timedelta(minutes=1))
    assert value == {"enabled": True, "start": "22:00", "end": "07:00", "days": [0, 6]}
    assert night_state()["result"] == "skipped: set during window"
    p.schedule_tick(world[0], MONDAY + p.timedelta(minutes=2))
    assert p.snapshot(world[0])["profile"] == "working"
    for args in (
        ("24:00", "07:00", "mon"),
        ("22:00", "22:00", "mon"),
        ("22:00", "07:00", ""),
        ("22:00", "07:00", "mon,mon"),
        ("22:00", "07:00", "monday"),
    ):
        with pytest.raises(ValueError):
            p.schedule_set(*args)
    monkeypatch.setenv("HOMELAB_PROFILE_JOB", "1")
    with pytest.raises(ValueError, match="jobs"):
        p.schedule_set("22:00", "07:00", "mon")


def test_schedule_off_ends_ownership(night, world):
    p.schedule_tick(world[0], MONDAY)
    assert p.schedule_off()["enabled"] is False
    assert night_state()["owned"] is False
    p.schedule_tick(world[0], local(2026, 10, 20, 7, 0))
    assert p.load(p.STATE / "session.json", {})["return_pending"]


def test_night_window_failure_does_not_stop_the_tick(world, monkeypatch):
    p.SCHEDULE.write_text("{broken")
    calls = []
    monkeypatch.setattr(p, "jobs", lambda: calls.append("jobs") or {})
    p.tick(world[0])
    assert calls


def test_inference_plan_lists_what_the_switch_would_stop(world, monkeypatch):
    work = [{"pid": 7, "command": "codex exec"}]
    monkeypatch.setattr(p, "vscode_work", lambda: work)
    assert p.inference_plan(world[0]) == {
        "apps_to_quit": ["com.apple.TextEdit"],
        "services_to_stop": ["cleanup", "voiceink"],
        "work": work,
        "on_ac_power": True,
        "refusal": None,
    }
    monkeypatch.setattr(p, "on_ac_power", lambda: False)
    assert p.inference_plan(world[0])["refusal"].startswith("Inference requires AC power")


def test_schedule_cli_prints_the_saved_window(world, monkeypatch, capsys):
    argv = [str(p.SCRIPT), "schedule", "set", "--start", "23:30", "--end", "06:00"]
    monkeypatch.setattr(sys, "argv", [*argv, "--days", "fri,sat"])
    assert p.main() == 0
    shown = json.loads(capsys.readouterr().out)
    assert shown["enabled"] and shown["days"] == [4, 5] and shown["start"] == "23:30"
    monkeypatch.setattr(sys, "argv", [*argv, "--days", "someday"])
    with pytest.raises(SystemExit) as exit_:
        p.main()
    assert exit_.value.code == 1
    assert p.schedule()["days"] == [4, 5]


def test_night_window_records_a_refused_entry(night, world, monkeypatch):
    calls = iter([[], [{"pid": 7, "command": "codex exec"}]])  # work appears between checks
    monkeypatch.setattr(p, "vscode_work", lambda: next(calls))
    with pytest.raises(ValueError):
        p.schedule_tick(night, MONDAY)
    assert night_state()["result"] == "failed"
    assert not p.load(p.STATE / "session.json", {}).get("return_pending")
