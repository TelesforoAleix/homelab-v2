"""Paths shared by LaunchAgent writers; never derive live paths from a checkout."""

import json
from pathlib import Path

ROOT = Path.home() / "Library/Application Support/homelab/mac"


def command(name, *arguments):
    manifest = json.loads((ROOT / "install.json").read_text())
    return [manifest["python"], str(ROOT / name), *arguments]
