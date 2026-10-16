import copy
import importlib.machinery
import importlib.util
import json
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[1]


def module(name, script):
    loader = importlib.machinery.SourceFileLoader(name, str(ROOT / "mac" / script))
    spec = importlib.util.spec_from_loader(name, loader)
    result = importlib.util.module_from_spec(spec)
    loader.exec_module(result)
    return result


p = module("models_profile", "homelab-profile")
m = module("model_policy", "profile_models.py")
r = module("models_router", "homelab-router")


@pytest.fixture
def files(monkeypatch, tmp_path):
    monkeypatch.setattr(p, "CONFIG", tmp_path / "profiles.json")
    monkeypatch.setattr(p, "REGISTRY", tmp_path / "models.json")
    monkeypatch.setattr(p, "STATE", tmp_path / "profile-state")
    monkeypatch.setattr(r, "STATE", tmp_path / "router-state")
    monkeypatch.setitem(r.model_policy, "REGISTRY", p.REGISTRY)
    monkeypatch.setattr(r, "VISION", tmp_path / "vision")
    monkeypatch.setattr(r, "EMBED", tmp_path / "embed")
    monkeypatch.setattr(p, "router", lambda: vars(r))
    p.STATE.mkdir()
    r.STATE.mkdir()
    for folder, names in (
        (r.VISION, ["Qwen3-VL-8B-Instruct-Q4_K_M.gguf", "mmproj-F16.gguf"]),
        (r.EMBED, ["Qwen3-Embedding-4B-Q8_0.gguf"]),
    ):
        folder.mkdir()
        for name in names:
            (folder / name).touch()
    return tmp_path


def migrate():
    original = {"profiles": copy.deepcopy(p.DEFAULTS), "idle_seconds": 42}
    original["profiles"]["working"]["settings"] = {"sleep": 10}
    p.save(p.CONFIG, original)
    before = p.CONFIG.read_bytes()
    p.migrate()
    return before, p.config(), json.loads(p.REGISTRY.read_text())


def test_migration_keeps_backup_is_idempotent_and_preserves_services(files):
    before, cfg, registry = migrate()
    assert p.CONFIG.with_suffix(".json.v1.bak").read_bytes() == before
    assert cfg["version"] == 2 and cfg["idle_seconds"] == 42
    for name, profile in p.DEFAULTS.items():
        assert m.enabled(cfg["profiles"][name]["services"]) == set(profile["services"])
        for service in m.ROUTED:
            setting = cfg["profiles"][name]["services"][service]
            assert setting["state"] == ("on request" if service in profile["services"] else "off")
            assert setting["allowed"] == [m.ROUTED[service]]
            assert setting["default"] == m.ROUTED[service]
    assert cfg["profiles"]["working"]["settings"] == {}
    assert registry["version"] == 1
    assert registry["services"]["vision"]["vision"]["flags"] == {
        "mmproj": str(r.VISION / "mmproj-F16.gguf"),
        "ngl": "99",
        "c": "16384",
        "np": "1",
        "cache-ram": "0",
        "flash-attn": "on",
    }
    assert registry["services"]["embed-large-bulk"]["Qwen3-Embedding-4B"]["flags"] == {
        "ngl": "99",
        "embeddings": "true",
        "pooling": "last",
        "embd-normalize": "2",
        "c": "8192",
        "b": "2048",
        "ub": "2048",
        "t": "4",
    }
    saved = (p.CONFIG.read_bytes(), p.REGISTRY.read_bytes())
    p.migrate()
    assert saved == (p.CONFIG.read_bytes(), p.REGISTRY.read_bytes())
    assert all(
        path.stat().st_mode & 0o777 == 0o600
        for path in [p.CONFIG, p.REGISTRY, p.CONFIG.with_suffix(".json.v1.bak")]
    )


@pytest.mark.parametrize("state", ["off", "on request", "on"])
def test_render_native_alias_resolution_and_states(files, state):
    _, cfg, registry = migrate()
    entry = copy.deepcopy(registry["services"]["vision"]["vision"])
    registry["services"]["vision"]["small"] = entry
    registry["services"]["vision"]["forbidden"] = copy.deepcopy(entry)
    policy = cfg["profiles"]["inference"]["services"]
    policy["vision"] = {"state": state, "allowed": ["vision", "small"], "default": "small"}
    presets = m.presets(policy, registry)
    names = {
        name: canonical
        for canonical, options in presets.items()
        for name in [canonical, *options["alias"].split(",")]
        if name
    }
    if state == "off":
        assert "vision" not in names and "vision-small" not in names
    else:
        assert names["vision"] == names["vision-small"] == "vision-small"
        assert names["vision-vision"] == "vision-vision"
        assert len(presets) == 3  # aliases never duplicate a child
        assert presets["vision-small"]["sleep-idle-seconds"] == ("-1" if state == "on" else "600")
        assert presets["vision-small"]["load-on-startup"] == str(state == "on").lower()
    assert "vision-forbidden" not in names
    assert names["embed-large-bulk"] == names["Qwen3-Embedding-4B"]
    assert names["embed-large-bulk-Qwen3-Embedding-4B"] == names["embed-large-bulk"]
    p.save(p.REGISTRY, registry)
    p.save(r.STATE / "allowed.json", {"version": 2, "services": policy})
    r.write_presets()
    assert (r.STATE / "models.ini").read_text() == m.render(policy, registry)


@pytest.mark.parametrize(
    "mutation",
    [
        lambda policy: policy["cleanup"].update(state="on request"),
        lambda policy: policy["vision"].update(state="invalid"),
        lambda policy: policy["vision"].update(allowed=[]),
        lambda policy: policy["vision"].update(allowed=["unknown"]),
        lambda policy: policy["vision"].update(default="unknown"),
        lambda policy: policy["vision"].update(allowed=["vision", "vision"]),
        lambda policy: policy["vision"].update(allowed=["bad\n[section]"]),
        lambda policy: policy.pop("cleanup"),
    ],
)
def test_invalid_policy_never_written(files, mutation):
    _, cfg, _ = migrate()
    before = p.CONFIG.read_bytes()
    mutation(cfg["profiles"]["working"]["services"])
    with pytest.raises(ValueError):
        p.write_profiles(cfg)
    assert p.CONFIG.read_bytes() == before


def test_switch_policy_reloads_and_loads_only_on_models(files, monkeypatch):
    _, cfg, registry = migrate()
    policy = cfg["profiles"]["inference"]["services"]
    policy["vision"]["state"] = "on"
    live = {name: "unloaded" for name in m.presets(policy, registry)}
    calls = []
    monkeypatch.setattr(r, "record", lambda: {"address": "private"})
    monkeypatch.setattr(r, "healthy", lambda saved: True)
    monkeypatch.setattr(p, "router_models", lambda: live.copy())

    def api(address, path, **kwargs):
        calls.append((path, kwargs))
        if path == "/models/load":
            live[kwargs["data"]["model"]] = "loading"
        elif path.startswith("/props?model="):
            live[path.split("=", 1)[1]] = "loaded"

    monkeypatch.setattr(r, "api", api)
    p.set_models(policy)
    assert calls == [
        ("/models?reload=1", {"timeout": 120}),
        ("/models/load", {"timeout": 300, "data": {"model": "vision-vision"}}),
        ("/props?model=vision-vision", {"timeout": 300}),
    ]
    assert live["vision-vision"] == "loaded"
    calls.clear()
    p.set_models(policy)
    assert all(path != "/models/load" for path, _ in calls)
    assert live["embed-large-bulk-Qwen3-Embedding-4B"] == "unloaded"
    assert "--sleep-idle-seconds" not in r.router_args(str(r.ipaddress.IPv4Address(0x64400001)))


def test_status_includes_policy_and_observes_on_model_failure(files, monkeypatch):
    _, cfg, registry = migrate()
    policy = cfg["profiles"]["working"]["services"]
    policy["embed-large-bulk"]["state"] = "on"
    p.save(r.STATE / "allowed.json", {"version": 2, "services": policy})
    live = {name: "loaded" for name in m.presets(policy, registry)}
    monkeypatch.setattr(p, "router_models", lambda: live)
    monkeypatch.setattr(p, "cleanup_running", lambda: True)
    monkeypatch.setattr(p, "app_running", lambda bundle: bundle == p.VOICE)
    monkeypatch.setattr(p, "settings", lambda: {})
    status = p.snapshot(cfg)
    assert status["profile"] == "working"
    assert status["services"]["embed-large-bulk"]["configured_state"] == "on"
    assert status["services"]["embed-large-bulk"]["allowed_models"] == ["Qwen3-Embedding-4B"]
    assert status["profiles"]["working"] == policy
    live["embed-large-bulk-Qwen3-Embedding-4B"] = "sleeping"
    assert p.snapshot(cfg)["profile"] == "custom"


def test_native_alias_and_idle_edits_are_custom(files, monkeypatch):
    _, cfg, registry = migrate()
    policy = cfg["profiles"]["working"]["services"]
    p.save(r.STATE / "allowed.json", {"version": 2, "services": policy})
    wanted = m.presets(policy, registry)
    live = p.ModelStates({name: "unloaded" for name in wanted})
    live.rows = {
        name: {
            "aliases": options["alias"].split(","),
            "status": {"args": ["--sleep-idle-seconds", "600"]},
        }
        for name, options in wanted.items()
    }
    monkeypatch.setattr(p, "router_models", lambda: live)
    monkeypatch.setattr(p, "cleanup_running", lambda: True)
    monkeypatch.setattr(p, "app_running", lambda bundle: bundle == p.VOICE)
    monkeypatch.setattr(p, "settings", lambda: {})
    assert p.snapshot(cfg)["profile"] == "working"
    row = live.rows[next(iter(wanted))]
    row["status"]["args"][-1] = "-1"
    assert p.snapshot(cfg)["profile"] == "custom"
    row["status"]["args"][-1] = "600"
    row["aliases"] = []
    assert p.snapshot(cfg)["profile"] == "custom"


def test_migrated_status_observes_legacy_policy_without_switching(files, monkeypatch):
    _, cfg, _ = migrate()
    p.save(r.STATE / "allowed.json", ["Qwen3-Embedding-4B"])
    monkeypatch.setattr(p, "router_models", lambda: {"Qwen3-Embedding-4B": "sleeping"})
    monkeypatch.setattr(p, "cleanup_running", lambda: True)
    monkeypatch.setattr(p, "app_running", lambda bundle: bundle == p.VOICE)
    monkeypatch.setattr(p, "settings", lambda: {})
    status = p.snapshot(cfg)
    assert status["profile"] == "working"
    assert status["services"]["embed-large-bulk"]["configured_state"] == "on request"
    assert status["services"]["embed-large-bulk"]["state"] == "sleeping"


def test_acceptance_node_program_is_valid_python(monkeypatch):
    acceptance = module("profile_acceptance", "../tests/mac_profile_models_acceptance.py")
    programs = []
    monkeypatch.setattr(
        acceptance.subprocess, "run", lambda *args, **kwargs: programs.append(kwargs["input"])
    )
    acceptance.node_probe()
    acceptance.node_probe(extra=True)
    for program in programs:
        compile(program, "node-probe", "exec")


def test_budget_sums_all_on_and_largest_on_request_including_unknown(files):
    _, cfg, registry = migrate()
    policy = cfg["profiles"]["inference"]["services"]
    vision = registry["services"]["vision"]
    vision["vision"]["memory_mb"] = 5000
    vision["small"] = {**copy.deepcopy(vision["vision"]), "memory_mb": 2000}
    policy["vision"].update(state="on", allowed=["vision", "small"])
    embed = registry["services"]["embed-large-bulk"]
    embed["Qwen3-Embedding-4B"]["memory_mb"] = 4000
    embed["other"] = {**copy.deepcopy(embed["Qwen3-Embedding-4B"]), "memory_mb": 3000}
    embed["unknown"] = {**copy.deepcopy(embed["other"]), "memory_mb": None}
    policy["embed-large-bulk"]["allowed"] += ["other", "unknown"]
    report = m.budget(policy, registry, 10000)
    assert report == {
        "memory_budget_mb": 10000,
        "required_mb": None,
        "known_mb": 11000,
        "unknown_models": ["embed-large-bulk-unknown"],
        "over_budget": True,
        "warning": "profile exceeds its memory budget",
    }
    embed["unknown"]["memory_mb"] = 6000
    assert m.budget(policy, registry, 20000)["required_mb"] == 13000
    policy["vision"]["state"] = "off"
    assert m.budget(policy, registry, 20000)["required_mb"] == 6000


def test_budget_migration_backup_and_warning_does_not_block_save(files, capsys):
    _, cfg, registry = migrate()
    for profile in cfg["profiles"].values():
        profile.pop("memory_budget_mb")
    p.save(p.CONFIG, cfg)
    original = p.CONFIG.read_bytes()
    p.migrate()
    assert p.CONFIG.with_suffix(".json.budget.bak").read_bytes() == original
    cfg = p.config()
    assert cfg["profiles"]["working"]["memory_budget_mb"] == 16384
    assert cfg["profiles"]["inference"]["memory_budget_mb"] == 40960
    p.migrate()
    assert p.CONFIG.with_suffix(".json.budget.bak").read_bytes() == original
    registry["services"]["embed-large-bulk"]["Qwen3-Embedding-4B"]["memory_mb"] = 2000
    p.save(p.REGISTRY, registry)
    cfg["profiles"]["working"]["memory_budget_mb"] = 1
    p.write_profiles(cfg)
    assert "exceeds" in capsys.readouterr().err
    assert p.config()["profiles"]["working"]["memory_budget_mb"] == 1
    cfg["profiles"]["working"]["memory_budget_mb"] = True
    before = p.CONFIG.read_bytes()
    with pytest.raises(ValueError):
        p.write_profiles(cfg)
    assert p.CONFIG.read_bytes() == before


def test_add_and_measure_are_isolated_and_commit_under_lock(files, monkeypatch):
    from argparse import Namespace
    from contextlib import contextmanager

    _, cfg, registry = migrate()
    original_profiles = p.CONFIG.read_bytes()
    original_registry = p.REGISTRY.read_bytes()
    locked = []

    @contextmanager
    def lock():
        locked.append(True)
        try:
            yield
        finally:
            locked.pop()

    monkeypatch.setattr(p, "lock", lock)
    save = p.save

    def save_locked(path, value):
        assert locked
        save(path, value)

    monkeypatch.setattr(p, "save", save_locked)
    args = Namespace(
        action="add",
        service="embed-large-bulk",
        name="tiny",
        like="Qwen3-Embedding-4B",
        url="https://huggingface.co/example/model/resolve/main/model.gguf",
        projector_url=None,
    )
    folder = files / "embed/tiny"

    def prepare(template, name, url, projector_url):
        assert not locked
        folder.mkdir()
        (folder / "model.gguf").touch()
        return {**template, "model": str(folder / "model.gguf")}, folder

    monkeypatch.setitem(p.registration, "prepare", prepare)

    def failed(entry, router):
        assert not locked
        raise ValueError("load failed")

    monkeypatch.setitem(p.registration, "test_load", failed)
    with pytest.raises(ValueError):
        p.model_operation(args)
    assert not folder.exists()
    assert p.REGISTRY.read_bytes() == original_registry

    def measured(entry, router):
        assert not locked
        return 1024

    monkeypatch.setitem(p.registration, "test_load", measured)
    assert p.model_operation(args)["memory_mb"] == 1024
    assert folder.exists()
    saved = json.loads(p.REGISTRY.read_text())
    assert saved["services"]["embed-large-bulk"]["tiny"]["memory_mb"] == 1024
    assert all(
        "tiny" not in profile["services"]["embed-large-bulk"]["allowed"]
        for profile in cfg["profiles"].values()
    )
    assert p.CONFIG.read_bytes() == original_profiles
    assert not (r.STATE / "allowed.json").exists()
    args.action = "measure"
    args.name = "Qwen3-Embedding-4B"
    assert p.model_operation(args)["memory_mb"] == 1024
