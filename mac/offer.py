"""Publish only fixed, public observations; never accept network instructions."""

import configparser
import contextlib
import math
import plistlib
import subprocess
from datetime import UTC, datetime


def watts():
    """Use Apple's measured system input power (mW), not the adapter's rated wattage."""
    try:
        raw = subprocess.check_output(
            ["/usr/sbin/ioreg", "-a", "-r", "-c", "AppleSmartBattery"], timeout=2
        )
        value = plistlib.loads(raw)[0].get("PowerTelemetryData", {}).get("SystemPowerIn")
        if type(value) in (int, float) and math.isfinite(value) and value >= 0:
            return value / 1000
    except (OSError, ValueError, IndexError, TypeError, subprocess.SubprocessError):
        pass
    return None


def build(host, cfg):
    """Construct an allowlisted snapshot, independently of the private status report."""
    load = host["load"]
    record = load(host["STATE"] / "session.json", {})
    profile = "inference" if record.get("return_pending") else "working"
    queued = host["queue"].read()
    paused = bool(record.get("paused") or queued["paused_on_battery"])
    lib = host["router"]()
    policy = load(lib["STATE"] / "allowed.json", None)
    modern = isinstance(policy, dict) and policy.get("version") == 2
    services = host["model_policy"]["services"](
        policy["services"] if modern else cfg["profiles"][profile]["services"]
    )
    observed = host["router_models"]()
    models = []
    registry = load(host["REGISTRY"], None)
    if registry is not None:
        registry = host["model_policy"]["validate_registry"](registry)
        entries = [
            (service, name, f"{service}-{name}" if modern else name, entry["flags"])
            for service, values in registry["services"].items()
            for name, entry in values.items()
        ]
    else:
        # Legacy installations use the same template as the native router.
        presets = configparser.ConfigParser(interpolation=None)
        template = host["SCRIPT"].with_name("router-models.ini.in").read_text()
        presets.read_string("[*]" + template.split("[*]", 1)[1])
        entries = [
            (service, name, name, dict(presets[name])) for service, name in host["MODELS"].items()
        ]
    for service, name, native_id, flags in entries:
        setting = services[service]
        enabled = setting["state"] != "off" and name in setting["allowed"]
        # The profile pause closes model services; a queue pause alone does not.
        state = "off"
        if not record.get("paused") and enabled and native_id in observed:
            state = "loaded" if observed[native_id] == "loaded" else "on request"
        context = None
        with contextlib.suppress(TypeError, ValueError):
            configured = int(flags.get("c"))
            if configured > 0:
                context = configured
        models.append(
            {
                "id": native_id,
                "kind": "vision"
                if "mmproj" in flags
                else ("embedding" if flags.get("embeddings") == "true" else "chat"),
                "state": state,
                "context": context,
            }
        )
    night = host["schedule"]()
    offer = {
        "at": datetime.now(UTC).isoformat(),
        "profile": profile,
        "power": "ac" if host["on_ac_power"]() else "battery",
        "paused": paused,
        "models": models,
        "memory_free_mb": host["queue"].memory()[1],
        "queue": host["queue"].counts(queued),
        "night_window": {key: night[key] for key in ("start", "end")} if night["enabled"] else None,
    }
    measured = watts()
    if measured is not None:
        offer["watts"] = measured
    return offer


def publish(host, cfg):
    # Observation must never prevent a profile change or a queue operation. An old file
    # retains its timestamp if measurement fails. No private exception text is logged.
    with contextlib.suppress(OSError, ValueError, KeyError, TypeError, subprocess.SubprocessError):
        offer = build(host, cfg)
        host["save"](host["router"]()["STATE"] / "public/homelab-offer.json", offer)
