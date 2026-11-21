"""Run-api usage: daily shape counters and unknown field paths. Values are never stored."""

from __future__ import annotations

import functools
import hashlib
import json
import logging
import re
import types
from typing import Annotated, Union, get_args, get_origin

from pydantic import BaseModel

CONTRACT = "run-api"
MAX_FIELDS = 32
NAME = re.compile(r"^[A-Za-z0-9_-]{1,64}$")
WINDOW_DAYS = 30
logger = logging.getLogger(__name__)


@functools.cache
def schema_hash() -> str:
    """SHA-256 of the exact text `python -m homelab.contracts.export run-api` prints."""
    from homelab.contracts import export

    text = json.dumps(export.run_api(), indent=2, sort_keys=True, ensure_ascii=False) + "\n"
    return hashlib.sha256(text.encode("utf-8")).hexdigest()


def shape(operation: str, body: BaseModel | None) -> str:
    """The request shape counted for usage. Every request has the base shape today; an
    expand step adds its second shape here."""
    return "base"


def models(annotation) -> list[type[BaseModel]]:
    """The models a field holds directly; free-form dictionaries and scalars hold none."""
    origin = get_origin(annotation)
    if origin is Annotated:
        return models(get_args(annotation)[0])
    if origin in (Union, types.UnionType):
        return [model for argument in get_args(annotation) for model in models(argument)]
    if isinstance(annotation, type) and issubclass(annotation, BaseModel):
        return [annotation]
    return []


def element(annotation):
    """The item annotation of a list field, or None."""
    origin = get_origin(annotation)
    if origin is Annotated:
        return element(get_args(annotation)[0])
    if origin in (Union, types.UnionType):
        for argument in get_args(annotation):
            if (item := element(argument)) is not None:
                return item
        return None
    if origin is list:
        return get_args(annotation)[0]
    return None


def name(key: str) -> str:
    return key if NAME.fullmatch(key) else "?"


def walk(model: type[BaseModel], value, path: str, found: list[str]) -> None:
    if not isinstance(value, dict):
        return
    for key, item in value.items():
        field = model.model_fields.get(key)
        if field is None:
            found.append(f"{path}.{name(key)}")
            continue
        descend(field.annotation, item, f"{path}.{key}", found)


def descend(annotation, value, path: str, found: list[str]) -> None:
    item = element(annotation)
    if item is not None and isinstance(value, list):
        for entry in value:
            descend(item, entry, f"{path}[]", found)
        return
    for model in models(annotation):
        walk(model, value, path, found)


def unknown_fields(model: type[BaseModel], raw: bytes) -> list[str]:
    """Paths of fields the model does not name, in schema terms, deduplicated and capped."""
    try:
        document = json.loads(raw)
    except ValueError:
        return []
    found: list[str] = []
    walk(model, document, "$", found)
    return list(dict.fromkeys(found))[:MAX_FIELDS]


def error_field(model: type[BaseModel], location: tuple) -> str:
    """A validation error's path from schema field names; anything else becomes `*`."""
    path = "$"
    current: list[type[BaseModel]] = [model]
    for part in location:
        field = next(
            (
                m.model_fields[part]
                for m in current
                if isinstance(part, str) and part in m.model_fields
            ),
            None,
        )
        if field is None:
            path += ".*"
            if not isinstance(part, int):
                current = []
            continue
        path += f".{part}"
        annotation = field.annotation
        current = models(element(annotation) or annotation)
    return path


def record(
    connect, application: str, operation: str, body: BaseModel | None, fields: list[str]
) -> None:
    """Count one accepted request and its unknown field paths; failures only warn."""
    try:
        with connect() as connection:
            with connection.transaction():
                connection.execute(
                    """INSERT INTO homelab_contract_usage
                           (contract, application, operation, shape, day, count)
                       VALUES (%s, %s, %s, %s, current_date, 1)
                       ON CONFLICT (contract, application, operation, shape, day)
                       DO UPDATE SET count = homelab_contract_usage.count + 1""",
                    (CONTRACT, application, operation, shape(operation, body)),
                )
                for field in fields:
                    connection.execute(
                        """INSERT INTO homelab_contract_unknown_fields
                               (contract, application, operation, field, first_seen, last_seen,
                                count)
                           VALUES (%s, %s, %s, %s, now(), now(), 1)
                           ON CONFLICT (contract, application, operation, field)
                           DO UPDATE SET last_seen = now(),
                               count = homelab_contract_unknown_fields.count + 1""",
                        (CONTRACT, application, operation, field),
                    )
    except Exception as error:  # noqa: BLE001 - recording never fails a request
        logger.warning("contract usage not recorded (%s)", type(error).__name__)


def usage(connect) -> dict:
    """Every application's shapes and unknown fields over the last 30 days."""
    with connect() as connection:
        since = connection.execute("SELECT current_date - %s::int", (WINDOW_DAYS - 1,)).fetchone()[
            0
        ]
        shapes = connection.execute(
            """SELECT application, operation, shape, day, count FROM homelab_contract_usage
               WHERE contract = %s AND day >= %s
               ORDER BY day, application, operation, shape""",
            (CONTRACT, since),
        ).fetchall()
        fields = connection.execute(
            """SELECT application, operation, field, first_seen, last_seen, count
               FROM homelab_contract_unknown_fields
               WHERE contract = %s AND last_seen >= %s
               ORDER BY application, operation, field""",
            (CONTRACT, since),
        ).fetchall()
    return {
        "contract": CONTRACT,
        "schema": schema_hash(),
        "since": since,
        "shapes": [
            dict(zip(("application", "operation", "shape", "day", "count"), row, strict=True))
            for row in shapes
        ],
        "unknown_fields": [
            dict(
                zip(
                    ("application", "operation", "field", "first_seen", "last_seen", "count"),
                    row,
                    strict=True,
                )
            )
            for row in fields
        ],
    }
