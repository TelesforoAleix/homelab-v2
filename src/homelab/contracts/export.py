"""Print a contract's OpenAPI document: `python -m homelab.contracts.export run-api`."""

from __future__ import annotations

import json
import sys

from fastapi import FastAPI

from homelab.api import runs


def run_api() -> dict:
    app = FastAPI(title="Home Lab run-api", version="1")
    app.include_router(runs.router)
    document = app.openapi()
    # FastAPI adds its own 422 to routes with path parameters; these routes never send it.
    for operations in document["paths"].values():
        for operation in operations.values():
            if (
                operation["responses"]
                .get("422", {})
                .get("content", {})
                .get("application/json", {})
                .get("schema", {})
                .get("$ref", "")
                .endswith("/HTTPValidationError")
            ):
                del operation["responses"]["422"]
    for name in ("HTTPValidationError", "ValidationError"):
        del document["components"]["schemas"][name]
    return document


CONTRACTS = {"run-api": run_api}


def main(argv: list[str]) -> int:
    if len(argv) != 1 or argv[0] not in CONTRACTS:
        print(
            f"usage: python -m homelab.contracts.export {{{'|'.join(CONTRACTS)}}}", file=sys.stderr
        )
        return 2
    sys.stdout.write(
        json.dumps(CONTRACTS[argv[0]](), indent=2, sort_keys=True, ensure_ascii=False) + "\n"
    )
    return 0


if __name__ == "__main__":
    raise SystemExit(main(sys.argv[1:]))
