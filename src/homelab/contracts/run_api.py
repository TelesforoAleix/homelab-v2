"""Version-one run-api wire models: requests, messages and responses. Validation errors never
echo caller content."""

from __future__ import annotations

import hashlib
import json
from datetime import date, datetime
from typing import Annotated, Literal
from uuid import UUID

from pydantic import (
    BaseModel,
    ConfigDict,
    Field,
    StringConstraints,
    ValidationError,
    field_validator,
    model_validator,
)

from homelab.api.run_keys import RunError
from homelab.tasks.executors import Executor
from homelab.tasks.runs import enabled_roles
from homelab.tasks.submit import INT_MAX, EvalName, RepositoryPath

RequestId = Annotated[str, StringConstraints(pattern=r"^[A-Za-z0-9._:-]{1,64}$")]
Text = Annotated[str, StringConstraints(pattern=r"^[^\x00]*$")]
# Readers ignore unknown fields, so a newer sender survives a provider rollback, and keep strict
# types. The published schema still forbids them: senders send none, the route records any.
TOLERANT = ConfigDict(
    extra="ignore", strict=True, json_schema_extra={"additionalProperties": False}
)


class Tolerant(BaseModel):
    model_config = TOLERANT


class TolerantExecutor(Executor):
    """A request's executor entry; the catalogue loader keeps the strict `Executor`."""

    model_config = ConfigDict(**TOLERANT, title="Executor")


class Overlay(Tolerant):
    commit: str = Field(pattern=r"^[0-9a-f]{40,64}$")
    paths: list[RepositoryPath] = Field(min_length=1)


class Work(Tolerant):
    goal: Text = Field(min_length=1, max_length=200_000)
    allowed_files: list[RepositoryPath] | None = None
    pinned_paths: list[RepositoryPath] = []
    check_command: Text | None = None
    history: Literal["shallow", "full"] = "shallow"
    overlay: Overlay | None = None


class Asset(Tolerant):
    name: Text = Field(min_length=1)
    type: Text = Field(min_length=1)
    version: Text = Field(min_length=1)
    sha256: str = Field(pattern=r"^[0-9a-f]{64}$")
    text: Text


class Limits(Tolerant):
    requests: int | None = Field(default=None, ge=0)
    minutes: float | None = Field(default=None, ge=0, allow_inf_nan=False)
    usd: float | None = Field(default=None, ge=0, allow_inf_nan=False)


class RunEval(Tolerant):
    set: EvalName
    case: EvalName
    executor: TolerantExecutor | None = None


class RunRequest(Tolerant):
    contract: Literal[1]
    request_id: RequestId
    attribution: dict[Text, Annotated[Text, Field(max_length=120)]] = Field(
        default_factory=dict, max_length=8
    )
    repository: str = Field(pattern=r"^[A-Za-z0-9][A-Za-z0-9_-]*/[A-Za-z0-9][A-Za-z0-9._-]*$")
    base_commit: str = Field(pattern=r"^[0-9a-f]{40,64}$")
    branch: str | None = Field(default=None, pattern=r"^[A-Za-z0-9][A-Za-z0-9._/-]*$")
    mode: Literal["write", "read"]
    role: Literal["slice", "ticket", "cut", "review", "check"]
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
        read_available = isinstance(role, str) and role in enabled_roles()
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
        if self.role == "check":
            if (
                self.mode != "write"
                or self.eval is None
                or "executor" in self.eval.model_fields_set
                or not self.work.check_command
                or self.work.overlay is None
            ):
                raise ValueError(
                    "check requires an eval mark, command and overlay, without executor"
                )
        elif "overlay" in self.work.model_fields_set:
            raise ValueError("overlay is only available for check; eval work needs an executor")
        elif self.eval and self.eval.executor is None:
            raise ValidationError.from_exception_data(
                "RunRequest",
                [
                    {
                        "type": "value_error",
                        "loc": ("eval", "executor"),
                        "input": None,
                        "ctx": {
                            "error": ValueError(
                                "overlay is only available for check; eval work needs an executor"
                            )
                        },
                    }
                ],
            )
        if self.mode == "write" and self.role != "check" and self.branch is None:
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


class Message(Tolerant):
    contract: Literal[1]
    message_id: RequestId
    text: Text = Field(min_length=1, max_length=200_000)

    @field_validator("contract", mode="before")
    @classmethod
    def integer_version(cls, value):
        return RunRequest.integer_version(value)


class Response(BaseModel):
    """Responses are described, not enforced: readers may meet new fields."""


class ErrorBody(Response):
    error: str
    status_reason: str | None = None
    message: str | None = None
    contract: str | None = None
    field: str | None = None
    schema_: str | None = Field(default=None, alias="schema")


class RunCreated(Response):
    run_id: UUID


class ExecutorSelection(Response):
    configuration: str | None = None
    harness: Literal["codex", "claude"] | None = None
    model: str | None = None
    effort: Literal["low", "medium", "high"] | None = None


class Tokens(Response):
    fresh: int | None
    cached: int | None
    output: int | None


class AttemptMetrics(Response):
    requests: int | None
    tokens: Tokens
    usd: float | None
    minutes: float | None


class RunMetrics(Response):
    requests: int
    tokens: Tokens
    usd: float | None
    minutes: float


class Attempt(Response):
    number: int
    status: str
    outcome: str | None
    started_at: datetime | None
    ended_at: datetime | None
    configuration_hash: str | None
    check_exit_code: int | None
    metrics: AttemptMetrics


class CheckResult(Response):
    exit_code: int
    tail: str


RunStatus = Literal[
    "queued", "running", "done", "question", "waiting", "escalated", "failed", "cancelled"
]


class RunRecord(Response):
    """What every run route returns for one run. Goals and assets are never included."""

    contract: Literal[1]
    run_id: UUID
    request_id: str
    application: str
    attribution: dict[str, str]
    repository: str
    branch: str | None
    mode: Literal["write", "read"]
    role: Literal["slice", "ticket", "cut", "review", "check"]
    level: Literal["light", "standard", "strong"]
    status: RunStatus
    status_reason: str | None
    executor: ExecutorSelection | None
    configuration_hash: str | None
    attempts: list[Attempt]
    commits: list[str]
    metrics: RunMetrics
    resumable: bool
    final_message: str | None
    resume_at: datetime | None
    created_at: datetime
    updated_at: datetime
    started_at: datetime | None
    finished_at: datetime | None
    check: CheckResult | None = None


class RunPage(Response):
    runs: list[RunRecord]
    cursor: str


class ShapeCount(Response):
    application: str
    operation: str
    shape: str
    day: date
    count: int


class UnknownField(Response):
    application: str
    operation: str
    field: str
    first_seen: datetime
    last_seen: datetime
    count: int


class ContractUsage(Response):
    """Accepted request shapes and unknown field paths; never a field value."""

    contract: Literal["run-api"]
    schema_: str = Field(alias="schema")
    since: date
    shapes: list[ShapeCount]
    unknown_fields: list[UnknownField]


def inline(schema: dict) -> dict:
    """Resolve local references so the schema stands alone inside any OpenAPI document."""
    definitions = schema.get("$defs", {})

    def walk(node):
        if isinstance(node, dict):
            if "$ref" in node and node["$ref"].startswith("#/$defs/"):
                return walk(definitions[node["$ref"].rsplit("/", 1)[1]])
            return {key: walk(value) for key, value in node.items() if key != "$defs"}
        if isinstance(node, list):
            return [walk(item) for item in node]
        return node

    return walk(schema)


def request_body(model: type[BaseModel]) -> dict:
    """The `openapi_extra` for a route that parses its own body."""
    schema = inline(model.model_json_schema())
    return {"requestBody": {"required": True, "content": {"application/json": {"schema": schema}}}}


def responses(success: dict[int, type[BaseModel]], *errors: int) -> dict:
    """The `responses=` for a run route: its success models and the error body."""
    declared = {status: {"model": model} for status, model in success.items()}
    declared.update({status: {"model": ErrorBody} for status in errors})
    return declared


def query_parameters(*names: str) -> dict:
    """The `openapi_extra` for a route that reads its optional string query by hand."""
    return {
        "parameters": [
            {"name": name, "in": "query", "required": False, "schema": {"type": "string"}}
            for name in names
        ]
    }
