"""The existing sealed CLI turn, with file transfers confined to launcher containers."""

import base64
import hashlib
import inspect
import json
import secrets
import tempfile
from pathlib import Path

from homelab.runs import launcher, slices, task_run

# Reuse the old no-follow regular-file copier inside the image, as the original runner did.
DATA = (
    """import base64, errno, hashlib, json, os, secrets, shutil, stat, sys
"""
    + "\n".join(
        inspect.getsource(getattr(task_run, name))
        for name in ("tree_files", "data_file", "fingerprint", "covers")
    )
    + """
spec = json.loads(sys.argv[1])
root = os.open('/work', os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW)
try:
    if spec['op'] == 'export':
        for path, source, info in tree_files(root):
            if source is None:
                print(json.dumps([path, None, stat.S_IFMT(info.st_mode)]))
            else:
                with os.fdopen(os.dup(source), 'rb') as stream:
                    stream.seek(0)
                    print(json.dumps([path, base64.b64encode(stream.read()).decode(),
                                      bool(info.st_mode & 0o111)]))
    else:
        imported = []
        for line in sys.stdin.buffer:
            path, value, executable = json.loads(line)
            if (not path or path.startswith('/') or
                any(part in ('', '.', '..', '.git', '.venv') for part in path.split('/'))):
                continue
            if spec.get('allowed') is not None and not (
                path == '.task-commit-message' or any(covers(p, path) for p in spec['allowed'])):
                continue
            if any(covers(p, path) for p in spec.get('pinned', [])) or value is None:
                continue
            import tempfile
            with tempfile.TemporaryFile() as stream:
                stream.write(base64.b64decode(value, validate=True)); stream.seek(0)
                info = os.stat_result((0o100700 if executable else 0o100600, 0, 0, 1,
                                       os.getuid(), os.getgid(), 0, 0, 0, 0))
                try:
                    data_file(root, path, stream.fileno(), info, (os.getuid(), os.getgid()))
                    imported.append(path)
                except OSError as error:
                    if error.errno not in (errno.ELOOP, errno.ENOTDIR, errno.EISDIR):
                        raise
        print(json.dumps(imported))
finally:
    os.close(root)
"""
)


def transfer(context, work_id, workspace, operation, *, source=None, allowed=None, pinned=()):
    label = "data-" + secrets.token_hex(8)
    out, err = context.folder / (label + ".out"), context.folder / (label + ".log")
    request = {
        "op": "run",
        "profile": "task",
        "shape": "data",
        "run_id": label,
        "work_id": work_id,
        "workspace": workspace,
        "env": {},
        "arguments": [
            DATA,
            json.dumps({"op": operation, "allowed": allowed, "pinned": list(pinned)}),
        ],
    }
    with open(source, "rb") if source is not None else tempfile.TemporaryFile() as stream:
        code, stopped = launcher.capture(request, out, err, context.seconds, stdin=stream)
    if code != 0 or stopped != "exited":
        raise launcher.Refused("cli_transfer_failed")
    return out


def snapshot(path, *, regular_only=False):
    result = {}
    for line in path.read_bytes().splitlines():
        name, value, executable = json.loads(line)
        if regular_only and value is None:
            continue
        result[name] = (
            (executable, hashlib.sha256(base64.b64decode(value)).hexdigest())
            if value is not None
            else (executable,)
        )
    return result


def worker(args, context, pins, ticket, record):
    name = args.cli_name
    created = False
    try:
        launcher.request({"op": "cli-create", "work_id": name})
        created = True
        plain = transfer(context, Path(context.owner[0]).name, "task", "export")
        before = snapshot(plain, regular_only=True)
        transfer(context, name, "cli", "import", source=plain)
        request = {
            "op": "run",
            "profile": "cli-run",
            "run_id": "cli-" + secrets.token_hex(12),
            "folders": {"work": name, "run": name},
            "tool": args.executor.harness,
            "exports": [],
            "stdin": True,
            "command": slices.command(args.executor, "", stdin=True, ticket=True),
        }
        # The rendered ticket includes policy and feedback beyond the API goal bound. Keep
        # it intact in the existing work envelope rather than putting content in argv.
        payload = {
            "goal": "Complete the ticket supplied in the work policy's ticket field.",
            "assets": [],
            "work": {"ticket": Path(ticket).read_text()},
        }
        with tempfile.TemporaryFile() as source:
            source.write(json.dumps(payload, ensure_ascii=False).encode())
            source.seek(0)
            code, stopped = launcher.capture(
                request,
                context.folder / "cli.out",
                context.folder / "cli.log",
                task_run.CLI_SECONDS,
                stdin=source,
            )
        output = slices.parse_output(
            args.executor.harness, (context.folder / "cli.out").read_text(errors="replace")
        )
        record["steps"] = output.metrics["requests"]
        record["tokens"] = {key: output.metrics[key] for key in ("fresh", "cached", "output")}
        if output.metrics["usd"] is not None:
            record["cost_usd"] = output.metrics["usd"]
        if output.limited or (
            code != 0
            and slices.LIMIT_MESSAGE.search(
                (context.folder / "cli.log").read_text(errors="replace")
            )
        ):
            return "usage_limit"
        if stopped != "exited":
            return "cli_" + stopped
        if code != 0:
            return "cli_failed"
        output = transfer(context, name, "cli", "export")
        after = snapshot(output)
        result = transfer(
            context,
            Path(context.owner[0]).name,
            "task",
            "import",
            source=output,
            allowed=args.allow,
            pinned=pins,
        )
        imported = set(json.loads(result.read_bytes()))
        ignored = sum(
            before.get(path) != after.get(path)
            for path in before.keys() | after.keys()
            if path not in imported
        )
        record["cli_ignored_changes"] = record.get("cli_ignored_changes", 0) + ignored
        return None
    except (OSError, ValueError, launcher.Refused):
        return "cli_failed"
    finally:
        if created:
            launcher.request({"op": "cli-remove", "work_id": name})
        # As before, CLI stdout/stderr and plain copies are not retained as run content.
        for filename in ("cli.out", "cli.log"):
            (context.folder / filename).unlink(missing_ok=True)
        for path in context.folder.glob("data-*"):
            path.unlink()
