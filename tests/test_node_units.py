import os
import re
import subprocess
from configparser import ConfigParser
from pathlib import Path

import pytest

UNITS = Path(__file__).resolve().parents[1] / "node/etc/systemd/system"
NOTIFIER = UNITS.parents[2] / "usr/local/sbin/homelab-notify.sh"
ALERT_UNITS = {
    "bot": "homelab-telegram-bot.service",
    "watchdog": "homelab-watchdog.service",
    "homelab": "homelab.service",
}


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
