from configparser import ConfigParser
from pathlib import Path

import pytest

UNITS = Path(__file__).resolve().parents[1] / "node/etc/systemd/system"


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
