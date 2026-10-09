import os
import re
import shlex
import subprocess
from configparser import ConfigParser
from pathlib import Path

import pytest

UNITS = Path(__file__).resolve().parents[1] / "node/etc/systemd/system"
ROOT = UNITS.parents[3]
NODE_FILES = sorted(
    path
    for path in (ROOT / "node").rglob("*")
    if path.is_file() and "__pycache__" not in path.parts
)
NOTIFIER = UNITS.parents[2] / "usr/local/sbin/homelab-notify.sh"
ALERT_UNITS = {
    "bot": "homelab-telegram-bot.service",
    "watchdog": "homelab-watchdog.service",
    "homelab": "homelab.service",
}


def host_modes() -> dict[str, tuple[int, int | None]]:
    rows = re.findall(
        r"^\| `(node/[^`]+/)` \| `(0[0-7]{3})` \| (`0[0-7]{3}`|—) \|$",
        (ROOT / "AGENTS.md").read_text(),
        re.M,
    )
    return {
        directory: (int(directory_mode, 8), None if mode == "—" else int(mode.strip("`"), 8))
        for directory, directory_mode, mode in rows
    }


def rebuild_section() -> str:
    return (
        (ROOT / "docs/operations.md")
        .read_text()
        .split("## Rebuilding the node\n", 1)[1]
        .split("\n## ", 1)[0]
    )


@pytest.mark.parametrize("path", NODE_FILES, ids=lambda path: str(path.relative_to(ROOT)))
def test_every_host_file_has_documented_directory_and_file_modes(path: Path):
    directory = f"{path.parent.relative_to(ROOT)}/"
    directory_mode, file_mode = host_modes()[directory]
    assert directory_mode & 0o700 == 0o700
    assert file_mode is not None
    assert file_mode & 0o022 == 0


def test_rebuild_installs_every_host_file_at_its_mirrored_path_with_documented_mode():
    installed = {}
    for line in rebuild_section().splitlines():
        args = shlex.split(line.strip()) if line.strip().startswith("sudo install -m ") else []
        if not args or not any(source.startswith("node/") for source in args[8:-1]):
            continue
        assert args[4:8] == ["-o", "root", "-g", "root"]
        mode = int(args[3], 8)
        destination = args[-1]
        for source in args[8:-1]:
            if not source.startswith("node/"):
                continue
            matches = sorted(ROOT.glob(source))
            assert matches, f"runbook source does not exist: {source}"
            for path in matches:
                relative = str(path.relative_to(ROOT))
                target = destination + path.name if destination.endswith("/") else destination
                assert target == "/" + relative.removeprefix("node/")
                expected_mode = host_modes()[f"{path.parent.relative_to(ROOT)}/"][1]
                assert mode == expected_mode
                assert relative not in installed
                installed[relative] = mode
    assert set(installed) == {str(path.relative_to(ROOT)) for path in NODE_FILES}


def test_rebuild_enables_every_installable_recorded_unit_and_no_trigger_only_unit():
    commands = re.findall(r"^\s*sudo systemctl enable (.+)$", rebuild_section(), re.M)
    enabled = {
        arg for command in commands for arg in shlex.split(command) if not arg.startswith("-")
    }
    for path in UNITS.iterdir():
        if not path.is_file():
            continue
        unit = ConfigParser(interpolation=None, strict=False)
        unit.read(path)
        if unit.get("Install", "WantedBy", fallback=""):
            assert path.name in enabled
        else:
            assert path.name not in enabled


def test_firewall_restricts_both_physical_interfaces_for_ipv4_and_ipv6():
    for name in ["after.rules", "after6.rules"]:
        rules = (ROOT / "node/etc/ufw" / name).read_text()
        assert ":DOCKER-USER - [0:0]" in rules
        assert "-A DOCKER-USER -m conntrack --ctstate RELATED,ESTABLISHED -j RETURN" in rules
        assert "-A DOCKER-USER -i tailscale0 -j RETURN" in rules
        for interface in ["wlp1s0", "eno1"]:
            assert f"-A DOCKER-USER -i {interface} -j DROP" in rules


def test_notifier_has_exactly_current_alert_aliases():
    aliases = dict(re.findall(r'^\s*(\S+)\)\s+unit="([^"]+)"\s*;;', NOTIFIER.read_text(), re.M))
    assert aliases == ALERT_UNITS


@pytest.mark.parametrize("alias", [*ALERT_UNITS, "model-helper", "workbench", "harness", "unknown"])
def test_notifier_accepts_only_current_alert_aliases(alias: str, tmp_path: Path):
    journal_unit = tmp_path / "journal-unit"
    journalctl = tmp_path / "journalctl"
    journalctl.write_text('#!/bin/sh\nprintf "%s" "$2" > "$JOURNAL_UNIT_FILE"\n')
    journalctl.chmod(0o755)
    env = {
        **os.environ,
        "PATH": f"{tmp_path}:{os.environ['PATH']}",
        "JOURNAL_UNIT_FILE": str(journal_unit),
    }
    env.pop("CREDENTIALS_DIRECTORY", None)
    result = subprocess.run(
        ["bash", str(NOTIFIER), "--alert", alias],
        env=env,
        capture_output=True,
        text=True,
        check=False,
    )

    assert result.returncode == 1
    if alias in ALERT_UNITS:
        assert journal_unit.read_text() == ALERT_UNITS[alias]
        assert "CREDENTIALS_DIRECTORY is not set" in result.stderr
        assert "unknown alert alias" not in result.stderr
    else:
        assert not journal_unit.exists()
        expected = f"unknown alert alias: '{alias}' (expected bot, watchdog or homelab)"
        assert expected in result.stderr


@pytest.mark.parametrize("path", sorted(path for path in UNITS.iterdir() if path.is_file()))
def test_units_wanted_by_data_target_follow_volume_contract(path: Path):
    unit = ConfigParser(interpolation=None, strict=False)
    unit.optionxform = str
    unit.read([path, *sorted(path.parent.glob(f"{path.name}.d/*.conf"))])
    if "homelab-data.target" not in unit.get("Install", "WantedBy", fallback="").split():
        return

    assert unit.get("Unit", "ConditionPathIsMountPoint") == "/srv/homelab"
    assert "homelab-data.target" in unit.get("Unit", "After").split()
    assert "homelab-data.target" in unit.get("Unit", "PartOf").split()
    requires = unit.get("Unit", "Requires", fallback="").split()
    assert "homelab-data.target" not in requires
    assert "srv-homelab.mount" not in requires
    for section, directive in [
        ("Unit", "RequiresMountsFor"),
        ("Service", "WorkingDirectory"),
        ("Service", "RootDirectory"),
        ("Service", "StateDirectory"),
    ]:
        for value in unit.get(section, directive, fallback="").split():
            directory = value.lstrip("-+").split(":", 1)[0].rstrip("/")
            assert directory != "/srv/homelab"
            assert not directory.startswith("/srv/homelab/")
