"""The committed run-api document is what the code generates, and it covers every run route."""

import json
import subprocess
import sys
from pathlib import Path

from homelab.contracts import export

ROOT = Path(__file__).resolve().parents[1]
ROUTES = {
    ("/v1/runs", "post"),
    ("/v1/runs", "get"),
    ("/v1/runs/{run_id}", "get"),
    ("/v1/runs/{run_id}/messages", "post"),
    ("/v1/runs/{run_id}/cancel", "post"),
    ("/v1/contracts/usage", "get"),
}


def printed() -> str:
    return subprocess.run(
        [sys.executable, "-m", "homelab.contracts.export", "run-api"],
        capture_output=True,
        text=True,
        check=True,
        cwd=ROOT,
    ).stdout


def test_export_is_deterministic_and_committed():
    first = printed()
    assert first == printed()
    assert (
        first == json.dumps(export.run_api(), indent=2, sort_keys=True, ensure_ascii=False) + "\n"
    )
    assert first == (ROOT / "contracts/run-api/openapi.json").read_text()


def test_document_holds_only_run_routes_with_their_schemas():
    document = json.loads(printed())
    found = {(path, method) for path, item in document["paths"].items() for method in item}
    assert found == ROUTES
    paths = document["paths"]
    for path, method in ROUTES:
        operation = paths[path][method]
        assert any(code.startswith("2") for code in operation["responses"])
        for response in operation["responses"].values():
            assert response["content"]["application/json"]["schema"]
    for path, method in (("/v1/runs", "post"), ("/v1/runs/{run_id}/messages", "post")):
        schema = paths[path][method]["requestBody"]["content"]["application/json"]["schema"]
        assert schema["additionalProperties"] is False and "contract" in schema["properties"]
    assert "$ref" not in json.dumps(paths["/v1/runs"]["post"]["requestBody"])
    assert set(document["components"]["schemas"]) >= {"RunRecord", "RunPage", "ErrorBody"}
