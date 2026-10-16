"""Validated private profile model policy and native llama.cpp preset rendering."""

import configparser
import copy
import io
import re
from pathlib import Path

ROUTED = {"vision": "vision", "embed-large-bulk": "Qwen3-Embedding-4B"}
SERVICES = set(ROUTED) | {"cleanup", "voiceink"}
REGISTRY = Path.home() / ".config/homelab/models.json"
# These are preset settings, not arbitrary llama-server arguments. In particular no
# listener, logging, cache discovery, remote download or router flags are accepted.
FLAGS = {
    "mmproj",
    "ngl",
    "c",
    "np",
    "cache-ram",
    "flash-attn",
    "embeddings",
    "pooling",
    "embd-normalize",
    "b",
    "ub",
    "t",
}


def identifier(value):
    return isinstance(value, str) and re.fullmatch(r"[A-Za-z0-9][A-Za-z0-9_.-]{0,95}", value)


def literal(value):
    return isinstance(value, str) and bool(value) and not any(c in value for c in "\n\r\x00")


def legacy_services(allowed):
    if not isinstance(allowed, list) or any(
        not isinstance(s, str) or s not in SERVICES for s in allowed
    ):
        raise ValueError("unknown service")
    return {
        s: (
            {"state": "on request" if s in allowed else "off", "allowed": [model], "default": model}
            if (model := ROUTED.get(s))
            else {"state": "on" if s in allowed else "off"}
        )
        for s in sorted(SERVICES)
    }


def services(value):
    """Resolve legacy input in memory; only the explicit migrate command writes it."""
    return legacy_services(value) if isinstance(value, list) else copy.deepcopy(value)


def enabled(value):
    return {s for s, policy in services(value).items() if policy["state"] != "off"}


def validate_services(value, registry):
    if not isinstance(value, dict) or set(value) != SERVICES:
        raise ValueError("every service needs a policy")
    for service, policy in value.items():
        routed = service in ROUTED
        fields = {"state", "allowed", "default"} if routed else {"state"}
        if not isinstance(policy, dict) or set(policy) != fields:
            raise ValueError("invalid service policy")
        if not isinstance(policy["state"], str) or policy["state"] not in (
            {"off", "on request", "on"} if routed else {"off", "on"}
        ):
            raise ValueError("invalid service state")
        if routed:
            allowed = policy["allowed"]
            if (
                not isinstance(allowed, list)
                or not allowed
                or any(not identifier(m) for m in allowed)
                or len(set(allowed)) != len(allowed)
                or set(allowed) - registry["services"][service].keys()
                or not isinstance(policy["default"], str)
                or policy["default"] not in allowed
            ):
                raise ValueError("invalid allowed models or default")


def validate_registry(value):
    if (
        not isinstance(value, dict)
        or set(value) != {"version", "services"}
        or type(value["version"]) is not int
        or value["version"] != 1
        or not isinstance(value["services"], dict)
        or set(value["services"]) != set(ROUTED)
    ):
        raise ValueError("invalid model registry")
    names = set()
    for models in value["services"].values():
        if not isinstance(models, dict) or not models:
            raise ValueError("empty model registry")
        for name, entry in models.items():
            if not identifier(name) or name in names or name in SERVICES - {"vision"}:
                raise ValueError("invalid or conflicting model id")
            names.add(name)
            if (
                not isinstance(entry, dict)
                or set(entry) != {"model", "flags", "memory_mb"}
                or not literal(entry["model"])
                or not Path(entry["model"]).is_absolute()
                or not isinstance(entry["flags"], dict)
                or set(entry["flags"]) - FLAGS
            ):
                raise ValueError("invalid model entry")
            for key, flag in entry["flags"].items():
                if not literal(flag) or (key == "mmproj" and not Path(flag).is_absolute()):
                    raise ValueError("invalid preset flag")
            memory = entry["memory_mb"]
            if memory is not None and (type(memory) is not int or memory <= 0):
                raise ValueError("invalid model memory")
    # A model id must not collide with any generated alias across services.
    aliases = {f"{s}-{m}" for s, models in value["services"].items() for m in models}
    if names & aliases:
        raise ValueError("model id conflicts with alias")
    return value


def initial_registry(template, vision, embed):
    text = template.read_text()
    paths = {
        "VISION_MODEL": vision / "Qwen3-VL-8B-Instruct-Q4_K_M.gguf",
        "PROJECTOR": vision / "mmproj-F16.gguf",
        "EMBED_MODEL": embed / "Qwen3-Embedding-4B-Q8_0.gguf",
    }
    for key, path in paths.items():
        text = text.replace("@" + key + "@", str(path))
    ini = configparser.ConfigParser(interpolation=None)
    ini.read_string("[*]" + text.split("[*]", 1)[1])
    result = {"version": 1, "services": {}}
    for service, name in ROUTED.items():
        flags = dict(ini[name])
        result["services"][service] = {
            name: {"model": flags.pop("model"), "flags": flags, "memory_mb": None}
        }
    return validate_registry(result)


def presets(policy, registry):
    """One child per model; native aliases select the default without duplicate weights."""
    validate_registry(registry)
    validate_services(policy, registry)
    result = {}
    for service in ROUTED:
        setting = policy[service]
        if setting["state"] == "off":
            continue
        for name in setting["allowed"]:
            entry = registry["services"][service][name]
            aliases = []
            if name == setting["default"]:
                aliases.append(service)
                if ROUTED[service] != service:
                    aliases.append(ROUTED[service])
            # Keep the legacy embedding upstream name and response model unchanged.
            result[f"{service}-{name}"] = {
                "model": entry["model"],
                **entry["flags"],
                "alias": ",".join(aliases),
                "load-on-startup": str(setting["state"] == "on").lower(),
                "sleep-idle-seconds": "-1" if setting["state"] == "on" else "600",
            }
    return result


def render(policy, registry):
    ini = configparser.ConfigParser(interpolation=None)
    ini["*"] = {"log-disable": "true"}
    for name, options in presets(policy, registry).items():
        ini[name] = options
    out = io.StringIO()
    ini.write(out)
    return "version = 1\n\n" + out.getvalue()
