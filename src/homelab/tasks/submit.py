"""Accept a planner's task file on stdin: `python -m homelab.tasks.submit`.

Prints the new task ids, in position order, as JSON. A refused file changes nothing, exits 1 and
says why on stderr, naming fields and positions but never their content.
"""

from __future__ import annotations

import json
import sys
from graphlib import CycleError, TopologicalSorter
from typing import Annotated, Any

import psycopg
from pydantic import (
    AfterValidator,
    BaseModel,
    ConfigDict,
    Field,
    StringConstraints,
    ValidationError,
    model_validator,
)

from homelab.jobs.tasks import run_coding_task
from homelab.settings import get_settings
from homelab.tasks.store import Refused, write_tasks

INT_MAX = 2**31 - 1
Text = Annotated[str, StringConstraints(strip_whitespace=True, min_length=1, pattern=r"^[^\x00]+$")]
Position = Annotated[int, Field(ge=1, le=INT_MAX)]


def repository_path(value: str) -> str:
    # The same rule homelab-task-run applies to a pin, so a bad path fails here, not at run time.
    parts = value.split("/")
    if (
        "\n" in value
        or value.startswith("-")
        or any(part in {"", ".", ".."} for part in parts)
        or parts[0] in {".git", ".venv"}
    ):
        raise ValueError("not a path inside the repository")
    return value


RepositoryPath = Annotated[Text, AfterValidator(repository_path)]


class Strict(BaseModel):
    model_config = ConfigDict(extra="forbid", strict=True)


class TaskEntry(Strict):
    position: Position
    depends_on: list[Position] = []
    title: Text
    goal: Text
    allowed_files: list[RepositoryPath] = Field(min_length=1)
    check_command: Text
    pinned_paths: list[RepositoryPath] = []
    priority: int = Field(default=0, ge=-INT_MAX, le=INT_MAX)
    severity: Text = "normal"

    @model_validator(mode="after")
    def distinct_dependencies(self) -> TaskEntry:
        if len(set(self.depends_on)) != len(self.depends_on):
            raise ValueError("a dependency is repeated")
        return self


class TaskFile(Strict):
    slice: Text
    repository: Text
    branch: Annotated[str, StringConstraints(pattern=r"^[A-Za-z0-9][A-Za-z0-9._/-]*$")]
    base_commit: Annotated[str, StringConstraints(pattern=r"^[0-9a-f]{40,64}$")]
    tasks: list[TaskEntry] = Field(min_length=1)

    @model_validator(mode="after")
    def ordered_graph(self) -> TaskFile:
        positions = [task.position for task in self.tasks]
        if len(set(positions)) != len(positions):
            raise ValueError("a position is repeated")
        try:
            TopologicalSorter({task.position: task.depends_on for task in self.tasks}).prepare()
        except CycleError:
            raise ValueError("the dependencies form a cycle") from None
        return self


def parse(raw: bytes) -> TaskFile:
    try:
        return TaskFile.model_validate_json(raw)
    except ValidationError as error:
        # Pydantic's own text quotes the input; these parts do not.
        first = error.errors(include_input=False, include_url=False, include_context=False)[0]
        where = ".".join(str(part) for part in first["loc"]) or "file"
        raise Refused(f"{where}: {first['msg']}") from None


def submit(connection: Any, plan: TaskFile) -> list[int]:
    """Write the tasks and defer their jobs in one transaction: all of it happens, or none."""
    try:
        with connection.transaction():
            ids, ready = write_tasks(connection, plan)
            for task_id, priority in ready:
                run_coding_task.configure(connection=connection, priority=priority).defer(
                    task_id=task_id
                )
    except psycopg.errors.UniqueViolation:
        raise Refused("this slice already has a task at one of these positions") from None
    return [ids[position] for position in sorted(ids)]


def main() -> int:
    try:
        plan = parse(sys.stdin.buffer.read())
        with psycopg.connect(get_settings().database_url) as connection:
            ids = submit(connection, plan)
    except Refused as error:
        print(f"homelab.tasks.submit: {error}", file=sys.stderr)
        return 1
    print(json.dumps(ids))
    return 0


if __name__ == "__main__":
    sys.exit(main())
