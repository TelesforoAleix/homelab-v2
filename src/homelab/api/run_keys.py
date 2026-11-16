"""Application identity comes exclusively from a root-managed bearer digest file."""

from __future__ import annotations

import hashlib
import hmac
import stat
from pathlib import Path
from typing import Literal

from fastapi import Request
from pydantic import Field, TypeAdapter, ValidationError

from homelab.tasks.store import RunError
from homelab.tasks.submit import Strict

KEY_FILE = Path("/etc/homelab/run-keys.json")
Scope = Literal["runs:write", "runs:read", "runs:eval"]


class ApplicationKey(Strict):
    application: str = Field(min_length=1, max_length=64, pattern=r"^[A-Za-z0-9._:-]+$")
    sha256: str = Field(pattern=r"^[0-9a-f]{64}$")
    scopes: list[Scope]


def parse_keys(raw: bytes) -> list[ApplicationKey]:
    try:
        keys = TypeAdapter(list[ApplicationKey]).validate_json(raw, strict=True)
        if len({key.sha256 for key in keys}) != len(keys):
            raise ValueError
        return keys
    except (ValueError, ValidationError):
        raise ValueError("invalid run key file") from None


def load_keys() -> list[ApplicationKey]:
    try:
        mode = KEY_FILE.lstat()
        if not stat.S_ISREG(mode.st_mode) or mode.st_uid != 0 or mode.st_mode & 0o022:
            raise ValueError("run key file must be root-owned and not writable by others")
        return parse_keys(KEY_FILE.read_bytes())
    except OSError:
        raise ValueError("run key file unavailable") from None


def authorize(request: Request, *scopes: Scope) -> ApplicationKey:
    try:
        keys = load_keys()
    except ValueError:
        raise RunError(503, "run_keys_unavailable") from None
    scheme, _, supplied = request.headers.get("authorization", "").partition(" ")
    if scheme.lower() != "bearer" or not supplied:
        raise RunError(401, "bearer_key_required")
    digest = hashlib.sha256(supplied.encode()).hexdigest()
    identity = next((key for key in keys if hmac.compare_digest(digest, key.sha256)), None)
    if identity is None:
        raise RunError(401, "bearer_key_required")
    if not set(scopes).issubset(identity.scopes):
        raise RunError(403, "scope_required")
    return identity
