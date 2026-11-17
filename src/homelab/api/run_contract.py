"""Version-one wire input. Validation errors never echo caller content."""

from __future__ import annotations

import hashlib
import json
from typing import Annotated, Literal

from pydantic import Field, StringConstraints, field_validator, model_validator

from homelab.api.run_keys import RunError
from homelab.tasks.executors import configuration_hash
from homelab.tasks.runs import enabled_roles
from homelab.tasks.submit import INT_MAX, EvalName, RepositoryPath, Strict

RequestId = Annotated[str, StringConstraints(pattern=r"^[A-Za-z0-9._:-]{1,64}$")]
Text = Annotated[str, StringConstraints(pattern=r"^[^\x00]*$")]


class Work(Strict):
    goal: Text = Field(min_length=1, max_length=200_000)
    allowed_files: list[RepositoryPath] | None = None
    pinned_paths: list[RepositoryPath] = []
    check_command: Text | None = None
    history: Literal["shallow", "full"] = "shallow"


class Asset(Strict):
    name: Text = Field(min_length=1)
    type: Text = Field(min_length=1)
    version: Text = Field(min_length=1)
    sha256: str = Field(pattern=r"^[0-9a-f]{64}$")
    text: Text


class Limits(Strict):
    requests: int | None = Field(default=None, ge=0)
    minutes: float | None = Field(default=None, ge=0, allow_inf_nan=False)
    usd: float | None = Field(default=None, ge=0, allow_inf_nan=False)


class RunEval(Strict):
    set: EvalName
    case: EvalName
    configuration: str

    @field_validator("configuration")
    @classmethod
    def known_configuration(cls, value: str) -> str:
        configuration_hash(value if value.startswith("pi/") else "pi/" + value)
        return value


class RunRequest(Strict):
    contract: Literal[1]
    request_id: RequestId
    attribution: dict[Text, Annotated[Text, Field(max_length=120)]] = Field(
        default_factory=dict, max_length=8
    )
    repository: str = Field(pattern=r"^[A-Za-z0-9][A-Za-z0-9_-]*/[A-Za-z0-9][A-Za-z0-9._-]*$")
    base_commit: str = Field(pattern=r"^[0-9a-f]{40,64}$")
    branch: str | None = Field(default=None, pattern=r"^[A-Za-z0-9][A-Za-z0-9._/-]*$")
    mode: Literal["write", "read"]
    role: Literal["slice", "ticket", "cut", "review"]
    level: Literal["light", "standard", "strong"]
    work: Work
    assets: list[Asset] = Field(default_factory=list, max_length=32)
    after: list[RequestId] = []
    priority: int = Field(default=0, ge=-INT_MAX, le=INT_MAX)
    limits: Limits = Field(default_factory=Limits)
    eval: RunEval | None = None

    @field_validator("contract", mode="before")
    @classmethod
    def integer_version(cls, value):
        if type(value) is not int:
            raise ValueError("invalid contract version")
        return value

    @model_validator(mode="before")
    @classmethod
    def supported_execution(cls, value):
        if not isinstance(value, dict):
            return value
        role = value.get("role")
        read_available = (
            isinstance(role, str) and role in enabled_roles() and value.get("eval") is None
        )
        if "limits" in value or (value.get("mode") == "read" and not read_available):
            raise RunError(
                409,
                "role_not_available",
                status_reason="role_not_available",
                message="Read mode and per-run limits arrive with the execution service.",
            )
        return value

    @model_validator(mode="after")
    def constraints(self) -> RunRequest:
        if self.mode == "write" and self.branch is None:
            raise ValueError("write mode requires branch")
        if self.branch and (
            ".." in self.branch or "//" in self.branch or self.branch.endswith(("/", ".", ".lock"))
        ):
            raise ValueError("invalid branch")
        if ".." in self.repository:
            raise ValueError("invalid repository")
        if self.request_id in self.after or len(set(self.after)) != len(self.after):
            raise ValueError("invalid predecessors")
        encoded = json.dumps([a.model_dump() for a in self.assets], ensure_ascii=False)
        if len(encoded.encode("utf-8")) > 200_000:
            raise ValueError("assets too large")
        for asset in self.assets:
            if hashlib.sha256(asset.text.encode("utf-8")).hexdigest() != asset.sha256:
                raise RunError(400, "asset_hash_mismatch")
        return self


class Message(Strict):
    contract: Literal[1]
    message_id: RequestId
    text: Text = Field(min_length=1, max_length=200_000)

    @field_validator("contract", mode="before")
    @classmethod
    def integer_version(cls, value):
        return RunRequest.integer_version(value)
