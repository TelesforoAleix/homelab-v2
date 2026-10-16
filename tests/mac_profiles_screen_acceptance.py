#!/usr/bin/env python3
"""Owner-side live check of the menu editor's stdin save path; restores in finally.

Run with the installed tools' recorded Python, outside a sandbox. No private content is printed.
Requires exact Working and a registered Qwen3-Embedding-0.6B (no download or registration).
"""

import copy
import json
import runpy
import subprocess
from pathlib import Path


def main():
    tools = Path.home() / "Library/Application Support/homelab/mac"
    manifest = json.loads((tools / "install.json").read_text())

    def command(action, value=None):
        result = subprocess.run(
            [manifest["python"], str(tools / "homelab-profile"), action],
            input=json.dumps(value) if value is not None else None,
            capture_output=True,
            text=True,
        )
        if result.returncode:
            raise RuntimeError(f"{action} failed; private command output withheld")
        return json.loads(result.stdout)

    original = command("profiles-show")
    status = command("status")
    if status["profile"] != "working":
        raise RuntimeError("Start in exact Working before this check")
    service, model = "embed-large-bulk", "Qwen3-Embedding-0.6B"
    if model not in status["model_registry"][service]:
        raise RuntimeError("Required small model is not registered; ask the orchestrator")
    candidate = copy.deepcopy(original)
    policy = candidate["profiles"]["working"]["services"][service]
    policy.update(state="on request", allowed=[model], default=model)
    if policy == original["profiles"]["working"]["services"][service]:
        raise RuntimeError(
            "Small model is already the sole default; this check needs a different initial policy"
        )
    router = runpy.run_path(str(tools / "homelab-router"))

    def native_models():
        saved = router["record"]()
        return router["api"](saved["address"], "/models")["data"]

    try:
        command("profiles-set", candidate)
        observed = command("status")
        assert observed["profile"] == "working", "save became custom"
        assert observed["profiles"]["working"][service] == policy
        models = native_models()
        expected = f"{service}-{model}"
        row = next(row for row in models if row["id"] == expected)
        assert service in row["aliases"], "default alias did not change"
        assert not any(
            row["id"].startswith(service + "-") and row["id"] != expected for row in models
        ), "old models still available"
        print("PASS: stdin save changed native model list and default; status remains Working")
    finally:
        command("profiles-set", original)
        assert command("profiles-show") == original, "profiles were not restored"
        assert command("status")["profile"] == "working", "restoration became custom"
        restored = native_models()
        prior = original["profiles"]["working"]["services"][service]
        expected = (
            {f"{service}-{name}" for name in prior["allowed"]} if prior["state"] != "off" else set()
        )
        assert {row["id"] for row in restored if row["id"].startswith(service + "-")} == expected
        print("PASS: original profiles and native model list restored; status remains Working")


if __name__ == "__main__":
    main()
