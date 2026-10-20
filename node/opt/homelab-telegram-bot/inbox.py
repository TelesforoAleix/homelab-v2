"""Private, atomic inbox records and opaque attachments; no content is executed."""

import json
import os
import re
import shutil
import tempfile
import uuid
from datetime import UTC, datetime
from pathlib import Path

from router import Capability, Executor

LOCKED = "The inbox needs the node unlocked."
FILE_LIMIT = 20 * 1024 * 1024
FILES_LIMIT = 2 * 1024 * 1024 * 1024


def sync_directory(path):
    descriptor = os.open(path, os.O_RDONLY)
    try:
        os.fsync(descriptor)
    finally:
        os.close(descriptor)


def safe_name(name):
    return (
        re.sub(r"[^A-Za-z0-9._-]", "_", Path(name or "attachment").name)[:120].strip(".")
        or "attachment"
    )


class Inbox:
    def __init__(self, planning):
        self.planning = planning
        self.path = planning.directory / "inbox"

    def available(self):
        return self.planning.ready()

    def file_check(self, size):
        if not self.available():
            raise ValueError(LOCKED)
        if type(size) is not int or not 0 <= size <= FILE_LIMIT:
            raise ValueError("Files must be at most 20 MB.")
        used = sum(p.stat().st_size for p in self.path.glob("*/file/*"))
        if used >= FILES_LIMIT or used + size > FILES_LIMIT:
            raise ValueError("The inbox files have reached 2 GiB; no new files can be kept.")

    def store(self, fields, *, content=None, name=None, media_type=None):
        with self.planning.lock:
            if not self.available():
                raise ValueError(LOCKED)
            if content is not None:
                self.file_check(len(content))
            self.path.mkdir(mode=0o700, parents=True, exist_ok=True)
            item = dict(fields)
            item.update(id=uuid.uuid4().hex, created=datetime.now(UTC).isoformat(), status="new")
            stage = Path(tempfile.mkdtemp(prefix=".item-", dir=self.path))
            try:
                if content is not None:
                    name = safe_name(name)
                    (stage / "file").mkdir(mode=0o700)
                    with (stage / "file" / name).open("xb") as stream:
                        os.fchmod(stream.fileno(), 0o600)
                        stream.write(content)
                        stream.flush()
                        os.fsync(stream.fileno())
                    sync_directory(stage / "file")
                    item.update(name=name, type=media_type, size=len(content), file=f"file/{name}")
                with (stage / "item.json").open("x") as stream:
                    os.fchmod(stream.fileno(), 0o600)
                    json.dump(item, stream, ensure_ascii=False)
                    stream.flush()
                    os.fsync(stream.fileno())
                sync_directory(stage)
                os.rename(stage, self.path / item["id"])
                sync_directory(self.path)
                sync_directory(self.path.parent)
            finally:
                if stage.exists():
                    shutil.rmtree(stage)
            self.planning.log(
                f"inbox id={item['id']} kind={item['kind']} "
                f"size={item.get('size', 0)} outcome=stored"
            )
            return item

    def command(self, args):
        with self.planning.lock:
            if not self.available():
                return LOCKED
            if len(args) > 1 or (args and not re.fullmatch(r"[0-9a-f]{32}", args[0])):
                return "Usage: /inbox [id]"
            if args:
                path = self.path / args[0] / "item.json"
                if not path.exists():
                    return "Inbox item not found."
                item = json.loads(path.read_text())
                return "\n\n".join(f"{key}: {value}" for key, value in item.items())
            items = [json.loads(p.read_text()) for p in self.path.glob("[0-9a-f]*/item.json")]
            items.sort(key=lambda item: (item["created"], item["id"]), reverse=True)
            lines = []
            for item in items[:10]:
                title = item.get("title") or item["text"] or item.get("name", "File")
                title = " ".join(title.split())[:80]
                lines.append(f"{item['id']} — {item['kind']} — {title} — {item['status']}")
            return "\n".join(lines) or "The inbox is empty."

    def register(self, router):
        router.register(
            Executor(
                "/inbox",
                Capability.READ,
                self.command,
                "latest kept items",
                "/inbox [id]",
                log_args=False,
            )
        )
