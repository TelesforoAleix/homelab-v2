"""Run ONLY by the orchestrator outside the sandbox, with disposable TextEdit state.

prepare: capture settings, install safe test profiles, and establish Working.
guard: prove the chat's codex exec is listed and unconfirmed switching refuses.
cycle: stop dictation/TextEdit, wait for launchd return, prove each app relaunches.
       From production config, prepares and restores the safe test config automatically.
restore: restore the original private config; leaves model availability in Working.
Never includes VS Code or Chrome in a quit list. Never invokes sudo.
"""

import importlib.machinery
import importlib.util
import json
import subprocess
import sys
import time
import urllib.request
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
loader = importlib.machinery.SourceFileLoader(
    "acceptance_profile", str(ROOT / "mac/homelab-profile")
)
spec = importlib.util.spec_from_loader(loader.name, loader)
p = importlib.util.module_from_spec(spec)
loader.exec_module(p)
BACKUP = p.STATE / "acceptance-original.json"
FIXTURE = p.STATE / "profile-acceptance.txt"


def cli(*args, success=True):
    result = subprocess.run([sys.executable, str(p.SCRIPT), *args], capture_output=True, text=True)
    print(result.stdout.strip())
    assert (result.returncode == 0) == success, result.stderr
    return result


def safe_config():
    cfg = p.config()
    assert cfg["profiles"]["working"]["close_apps"] == []
    assert cfg["profiles"]["inference"]["close_apps"] == ["com.apple.TextEdit"]
    assert cfg["return_seconds"] == 15
    return cfg


def main(stage):
    if stage == "cycle" and not BACKUP.exists():
        main("prepare")
        try:
            main("cycle")
        finally:
            if not p.load(p.STATE / "session.json", {}).get("return_pending"):
                main("restore")
        return
    if stage == "prepare":
        assert not BACKUP.exists(), "acceptance backup already exists; restore before rerunning"
        assert not p.load(p.STATE / "session.json", {}).get("return_pending"), (
            "recover Working first"
        )
        cli("install")
        p.save(BACKUP, p.load(p.CONFIG, {}))
        cfg = {"profiles": p.config()["profiles"], "return_seconds": 15}
        observed = p.settings()
        for name, profile in cfg["profiles"].items():
            profile["settings"] = observed
            profile["close_apps"] = ["com.apple.TextEdit"] if name == "inference" else []
            profile["jobs"] = False
        p.save(p.CONFIG, cfg)
        safe_config()
        # Run only after the node ingest precheck: release previously loaded test models
        # so the shortened profile idle interval can actually be observed.
        lib = p.router()
        saved = lib["record"]()
        address = lib["check_address"](saved["address"])
        opener = urllib.request.build_opener(urllib.request.ProxyHandler({}))
        for model, state in p.router_models().items():
            if state not in ("sleeping", "unloaded"):
                request = urllib.request.Request(
                    f"http://{address}:{lib['PORT']}/models/unload",
                    data=json.dumps({"model": model}).encode(),
                    headers={"Content-Type": "application/json"},
                )
                with opener.open(request, timeout=120) as response:
                    assert response.status == 200
        FIXTURE.write_text("Harmless profile acceptance document.\n")
        p.run("/usr/bin/open", "-b", "com.apple.TextEdit", str(FIXTURE))
        cli("switch", "working", "--skip-settings")
        assert p.snapshot(p.config())["profile"] == "working"
        print("prepare=passed system_settings_unchanged=true")
    elif stage == "guard":
        cfg = safe_config()
        work = p.vscode_work()
        assert any("codex" in row["command"] and "exec" in row["command"] for row in work), (
            "run from the live orchestrator VS Code terminal with a codex exec descendant"
        )
        before = p.snapshot(cfg)
        cli("check-work", success=False)
        cli("switch", "inference", "--skip-settings", success=False)
        assert p.snapshot(cfg) == before
        print("guard=passed side_effects=false")
    elif stage == "cycle":
        cfg = safe_config()
        before = p.snapshot(cfg)
        assert before["profile"] == "working"
        assert p.app_running("com.apple.TextEdit")
        # The explicit override is safe here only because safe_config verified the test list.
        cli("switch", "inference", "--skip-settings", "--confirm-kill")
        now = p.snapshot(cfg)
        assert now["profile"] == "inference"
        assert not now["services"]["cleanup"]["running"]
        assert not now["services"]["voiceink"]["running"]
        assert not p.app_running("com.apple.TextEdit")
        deadline = time.monotonic() + 100
        while p.snapshot(cfg)["return_pending"] and time.monotonic() < deadline:
            time.sleep(2)
        after = p.snapshot(cfg)
        assert after["profile"] == "working" and not after["return_pending"], after
        assert p.app_running("com.apple.TextEdit")
        assert FIXTURE.read_text() == "Harmless profile acceptance document.\n"
        cli("status")
        print("cycle=passed apps_relaunched=true dictation_restored=true settings_unchanged=true")
    elif stage == "restore":
        assert not p.load(p.STATE / "session.json", {}).get("return_pending"), (
            "recover Working first"
        )
        assert BACKUP.exists()
        p.save(p.CONFIG, p.load(BACKUP, {}))
        BACKUP.unlink()
        cli("status")
        print("restore=passed production_config_restored=true")
    else:
        raise ValueError("choose prepare, guard, cycle or restore")


if __name__ == "__main__":
    main(sys.argv[1])
