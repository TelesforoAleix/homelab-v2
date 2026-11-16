"""Strict executor and bearer-file parsing, independent of a database."""

import hashlib
import json
from pathlib import Path

import pytest
import yaml

from homelab.api import run_keys
from homelab.tasks.executors import configuration_hash, load_executors


def test_initial_executor_table():
    table = load_executors()
    assert set(table) == {"ticket", "slice", "cut", "review"}
    for role in table.values():
        assert set(role) == {"light", "standard", "strong"}
    assert all(
        row[0].model_dump(exclude_none=True) == {"configuration": "pi/base"}
        for row in table["ticket"].values()
    )
    assert table["slice"]["light"][0].model == "gpt-6-luna"
    assert table["slice"]["standard"][0].effort == "medium"
    assert table["review"]["strong"][1].model == "opus"
    assert len(configuration_hash("pi/base")) == 64


@pytest.mark.parametrize("change", ["missing", "extra", "empty", "mixed", "unknown", "path"])
def test_bad_executor_table(tmp_path, change):
    table = yaml.safe_load(Path("config/executors.yaml").read_text())
    if change == "missing":
        del table["review"]
    elif change == "extra":
        table["ticket"]["other"] = table["ticket"]["light"]
    elif change == "empty":
        table["ticket"]["light"] = []
    else:
        table["ticket"]["light"] = [
            {
                "mixed": {"configuration": "pi/base", "model": "x"},
                "unknown": {"harness": "unknown", "model": "x", "effort": "high"},
                "path": {"configuration": "pi/../outside"},
            }[change]
        ]
    path = tmp_path / "executors.yaml"
    path.write_text(yaml.safe_dump(table))
    with pytest.raises(ValueError, match="executor"):
        load_executors(path)


def test_configuration_hash_matches_documented_manifest():
    folder = Path("configurations/pi/base")
    pairs = sorted(
        [str(p.relative_to(folder)), hashlib.sha256(p.read_bytes()).hexdigest()]
        for p in folder.rglob("*")
        if p.is_file()
    )
    expected = hashlib.sha256(json.dumps(pairs, separators=(",", ":")).encode()).hexdigest()
    assert configuration_hash("pi/base") == expected


@pytest.mark.parametrize(
    "document",
    [
        {},
        [{"application": "factory", "sha256": "secret", "scopes": [], "repositories": {}}],
        [{"application": "factory", "sha256": "a" * 64, "scopes": ["admin"], "repositories": {}}],
        [
            {
                "application": "factory",
                "sha256": "a" * 64,
                "scopes": [],
                "repositories": {},
                "key": "secret",
            }
        ],
        [{"application": "factory", "sha256": "a" * 64, "scopes": [], "repositories": {}}] * 2,
    ],
)
def test_invalid_key_file_redacts_content(document):
    with pytest.raises(ValueError) as exc:
        run_keys.parse_keys(json.dumps(document).encode())
    assert "secret" not in str(exc.value)


def test_key_file_example_is_dummy_and_api_only():
    keys = run_keys.parse_keys(Path("docs/run-keys.example.json").read_bytes())
    assert keys[0].sha256 == "0" * 64
    assert keys[0].repositories == {"owner/scratch-repo": "main"}
    services = yaml.safe_load(Path("compose.yaml").read_text())["services"]
    mounts = services["api"]["volumes"]
    assert {
        "type": "bind",
        "source": "/etc/homelab/run-keys.json",
        "target": "/etc/homelab/run-keys.json",
        "read_only": True,
        "bind": {"create_host_path": False},
    } in mounts
    for name, service in services.items():
        if name != "api":
            assert "run-keys" not in json.dumps(service)


def test_startup_refuses_malformed_table_before_opening_services(monkeypatch):
    from homelab.api import runs

    def invalid_table():
        raise ValueError("invalid executor table")

    monkeypatch.setattr(runs, "load_executors", invalid_table)
    monkeypatch.setattr(runs, "load_keys", lambda: pytest.fail("invalid table did not stop start"))
    with pytest.raises(ValueError, match="executor table"):
        runs.startup()


def test_untrusted_key_file_is_refused(monkeypatch, tmp_path):
    import os

    path = tmp_path / "keys.json"
    path.write_text("[]")
    monkeypatch.setattr(run_keys, "KEY_FILE", path)
    if os.getuid() != 0:
        with pytest.raises(ValueError, match="root-owned"):
            run_keys.load_keys()
    path.chmod(0o666)
    with pytest.raises(ValueError):
        run_keys.load_keys()


def test_symlink_key_file_is_refused(monkeypatch, tmp_path):
    target = tmp_path / "target.json"
    target.write_text("[]")
    link = tmp_path / "keys.json"
    link.symlink_to(target)
    monkeypatch.setattr(run_keys, "KEY_FILE", link)
    with pytest.raises(ValueError):
        run_keys.load_keys()


def test_duplicate_executor_rows_are_refused(tmp_path):
    path = tmp_path / "executors.yaml"
    path.write_text(Path("config/executors.yaml").read_text() + "\nticket: {}\n")
    with pytest.raises(ValueError, match="executor"):
        load_executors(path)


@pytest.mark.parametrize(
    "repositories",
    [
        None,
        [],
        {"invalid": "main"},
        {"owner/repo": ""},
        {"owner/repo": 1},
        {"owner/repo": {"default_branch": "main"}},
    ],
)
def test_invalid_repository_policy_is_refused(repositories):
    document = [
        {
            "application": "factory",
            "sha256": "0" * 64,
            "scopes": ["runs:write"],
            "repositories": repositories,
        }
    ]
    with pytest.raises(ValueError, match="invalid run key file"):
        run_keys.parse_keys(json.dumps(document).encode())
