"""Orchestrator only, outside the sandbox, on AC. No app/service profile switch.

Exercise the installed helper against a captured recovery record. Pause the profile
agent while using that record; restore exact AC/Battery/GPU observations and private
session state before restarting it. Retain recovery files and pause the agent if
restoration fails. Never run this through pytest or as root.
"""

import importlib.machinery
import importlib.util
import json
import os
import re
import stat
import subprocess
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
loader = importlib.machinery.SourceFileLoader("profile_proof", str(ROOT / "mac/homelab-profile"))
spec = importlib.util.spec_from_loader(loader.name, loader)
p = importlib.util.module_from_spec(spec)
loader.exec_module(p)
HELPER = Path("/usr/local/sbin/homelab-profile-apply")
BACKUP = p.STATE / "privileged-acceptance-backup.json"


def require(condition, message):
    if not condition:
        raise RuntimeError(message)


def custom_settings():
    """All per-source keys, including display sleep, must survive the proof."""
    text = p.run("/usr/bin/pmset", "-g", "custom")
    sources = {}
    current = None
    for line in text.splitlines():
        if line.strip().endswith(":"):
            current = line.strip()[:-1]
            sources[current] = {}
        elif current:
            match = re.match(r"\s*(\S+)\s+(\d+)\b", line)
            if match:
                sources[current][match[1]] = int(match[2])
    require({"AC Power", "Battery Power"} <= sources.keys(), "Missing per-source pmset data")
    return sources


def apply(name):
    # -k ignores cached credentials; -n forbids prompting.
    p.run("/usr/bin/sudo", "-k", "-n", str(HELPER), name)


def main():
    require(os.geteuid() != 0, "Run as the owner, not root")
    require(
        HELPER.read_bytes() == (ROOT / "mac/homelab-profile-apply").read_bytes(),
        "Reinstall the corrected helper from this checkout first",
    )
    info = HELPER.stat()
    require(info.st_uid == 0 and not info.st_mode & 0o022, "Helper must be root-owned/protected")
    require(p.on_ac_power(), "Connect the charger before running this proof")
    require(not BACKUP.exists(), "Previous proof backup exists; recover it before retrying")
    domain = f"gui/{os.getuid()}"
    label = domain + "/org.homelab.profile"
    plist = p.AGENTS / "org.homelab.profile.plist"
    require(plist.is_file(), "Profile LaunchAgent plist missing")
    p.run("/bin/launchctl", "print", label)
    # Refuse before pausing an agent with an active pending return.
    require(
        not p.load(p.STATE / "session.json", {}).get("return_pending"),
        "Return to Working before this proof",
    )
    p.run("/bin/launchctl", "bootout", label)
    restart = True
    try:
        with p.lock():
            session = p.STATE / "session.json"
            cfg = p.config()
            require(p.snapshot(cfg)["profile"] == "working", "Establish Working services first")
            require(not p.snapshot(cfg)["running_jobs"], "Wait for running jobs to finish")
            old = session.read_text() if session.exists() else None
            old_mode = stat.S_IMODE(session.stat().st_mode) if old is not None else 0o600
            require(not p.load(session, {}).get("return_pending"), "Pending return appeared")
            before = custom_settings()
            previous = p.settings()
            p.validate_settings(previous)
            p.save(
                BACKUP, {"session": old, "mode": old_mode, "sources": before, "settings": previous}
            )
            # No apps or services belong to this helper-only test recovery record.
            p.save(
                session,
                {
                    "return_pending": True,
                    "return_requested": True,
                    "closed": [],
                    "stopped": [],
                    **{"previous_" + k: v for k, v in previous.items()},
                },
            )
            try:
                for args in ([], ["bogus"], ["working", "extra"]):
                    result = subprocess.run(
                        ["/usr/bin/sudo", "-k", "-n", str(HELPER), *args],
                        capture_output=True,
                        text=True,
                        timeout=30,
                    )
                    require(result.returncode != 0, "Unsupported helper arguments accepted")
                print("unsupported_arguments=refused", flush=True)
                require(p.on_ac_power(), "Charger disconnected before Inference proof")
                apply("inference")
                actual = p.settings()
                require(
                    actual == {"powermode": 2, "sleep": 0, "gpu_limit_mb": 45056},
                    "Inference did not apply High Power, no sleep, and 45056 MB",
                )
                during = custom_settings()
                expected = json.loads(json.dumps(before))
                expected["AC Power"].update(powermode=2, sleep=0)
                require(during == expected, "Inference changed other AC or Battery settings")
                print("inference=passed sudo_no_prompt=true " + json.dumps(actual), flush=True)
            finally:
                try:
                    apply("working")
                    require(p.settings() == previous, "Recorded AC/GPU values did not restore")
                    require(custom_settings() == before, "Per-source settings did not restore")
                    if old is None:
                        session.unlink()
                    else:
                        session.write_text(old)
                        session.chmod(old_mode)
                    BACKUP.unlink()
                except BaseException:
                    restart = False
                    print(
                        "RESTORE FAILED: agent paused; recovery record and backup retained",
                        flush=True,
                    )
                    raise
                print(
                    "working=passed sudo_no_prompt=true recorded_settings_restored=true "
                    "battery_settings_unchanged=true " + json.dumps(previous),
                    flush=True,
                )
    finally:
        if restart:
            p.run("/bin/launchctl", "bootstrap", domain, str(plist))
    require(p.snapshot(p.config())["profile"] == "working", "Final profile is not Working")
    p.run("/bin/launchctl", "print", label)
    print("final=working launchagent=running apps_untouched=true")


if __name__ == "__main__":
    main()
