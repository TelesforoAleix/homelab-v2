"""Validate the executor catalogue and fingerprint its selected configuration."""

from __future__ import annotations

import hashlib
import json
from pathlib import Path
from typing import Annotated, Literal

import yaml
from pydantic import Field, StringConstraints, TypeAdapter, ValidationError, model_validator

from homelab.tasks.submit import Strict

ROOT = Path(__file__).resolve().parents[3]
TABLE = ROOT / "config" / "executors.yaml"
ROLES = {"slice", "ticket", "cut", "review"}
LEVELS = {"light", "standard", "strong"}
Configuration = Annotated[str, StringConstraints(pattern=r"^pi/[A-Za-z0-9][A-Za-z0-9._-]{0,63}$")]


class ExecutorLoader(yaml.SafeLoader):
    def construct_mapping(self, node, deep=False):
        self.flatten_mapping(node)
        mapping = {}
        for key_node, value_node in node.value:
            key = self.construct_object(key_node, deep=deep)
            if key in mapping:
                raise ValueError("duplicate executor field")
            mapping[key] = self.construct_object(value_node, deep=deep)
        return mapping


class Executor(Strict):
    configuration: Configuration | None = None
    harness: Literal["codex", "claude"] | None = None
    model: str | None = Field(default=None, min_length=1)
    effort: Literal["low", "medium", "high"] | None = None

    @model_validator(mode="after")
    def one_kind(self) -> Executor:
        if self.configuration:
            if any(value is not None for value in (self.harness, self.model, self.effort)):
                raise ValueError("mixed executor kinds")
            configuration_hash(self.configuration)
            self.limits()
        elif self.harness is None or self.model is None:
            raise ValueError("incomplete executor")
        elif self.harness == "codex" and self.effort is None:
            raise ValueError("codex executor needs effort")
        return self

    def selection(self) -> dict:
        return self.model_dump(exclude_none=True)

    def fingerprint(self) -> str:
        if self.configuration:
            return configuration_hash(self.configuration)
        return hashlib.sha256(
            json.dumps(self.selection(), sort_keys=True, separators=(",", ":")).encode()
        ).hexdigest()

    def limits(self) -> dict:
        if not self.configuration:
            return {}
        try:
            limits = json.loads(
                (ROOT / "configurations" / self.configuration / "configuration.json").read_text()
            )["limits"]
            if set(limits) != {"requests", "minutes"} or any(
                type(value) is not int or value <= 0 for value in limits.values()
            ):
                raise ValueError
            return limits
        except (OSError, ValueError, KeyError, TypeError):
            raise ValueError("invalid executor limits") from None


def configuration_hash(configuration: str) -> str:
    """Same manifest encoding as the installed Pi configuration reader."""
    import re

    if not re.fullmatch(r"pi/[A-Za-z0-9][A-Za-z0-9._-]{0,63}", configuration):
        raise ValueError("invalid executor configuration")
    folder = ROOT / "configurations" / configuration
    try:
        if folder.is_symlink() or not (folder / "configuration.json").is_file():
            raise ValueError("unknown executor configuration")
        pairs = []
        for path in sorted(folder.rglob("*")):
            if path.is_symlink():
                raise ValueError("invalid executor configuration file")
            if path.is_dir():
                continue
            if not path.is_file():
                raise ValueError("invalid executor configuration file")
            data = path.read_bytes()
            data.decode("utf-8")
            pairs.append([path.relative_to(folder).as_posix(), hashlib.sha256(data).hexdigest()])
        return hashlib.sha256(json.dumps(pairs, separators=(",", ":")).encode()).hexdigest()
    except (OSError, UnicodeError):
        raise ValueError("unreadable executor configuration") from None


def load_executors(path: Path = TABLE) -> dict[str, dict[str, list[Executor]]]:
    try:
        document = yaml.load(path.read_text(), Loader=ExecutorLoader)
        if not isinstance(document, dict) or set(document) != ROLES:
            raise ValueError
        if any(not isinstance(row, dict) or set(row) != LEVELS for row in document.values()):
            raise ValueError
        adapter = TypeAdapter(dict[str, dict[str, list[Executor]]])
        table = adapter.validate_python(document, strict=True)
        if any(not row for levels in table.values() for row in levels.values()):
            raise ValueError
        return table
    except (OSError, ValueError, TypeError, yaml.YAMLError, ValidationError):
        raise ValueError("invalid executor table") from None
