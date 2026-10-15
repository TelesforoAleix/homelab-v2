import runpy
import subprocess
import sys
from pathlib import Path

SCRIPT = Path(__file__).resolve().parents[1] / "mac/homelab-embed"


def test_retired_launcher_points_to_router_without_starting():
    result = subprocess.run([sys.executable, str(SCRIPT), "start"], capture_output=True)
    assert result.returncode == 1
    assert b"mac/homelab-router" in result.stderr


def test_address_discovery_import_remains_available_to_listener():
    assert callable(runpy.run_path(str(SCRIPT))["tailscale_address"])
