"""Grade a commit with an overlay in the existing networkless task check shape."""

import json
import secrets
import tempfile
from pathlib import Path

from homelab.runs import delivery, launcher, runner, task_run

# Both commits arrive as Git objects, never by executing or extracting repository content
# in the service. All checkout/overlay writes happen in the launcher's task copy.
IMPORT = r"""
import os
import subprocess
import sys
import tempfile

os.environ.update(PATH='/usr/bin:/bin', GIT_CONFIG_NOSYSTEM='1', GIT_CONFIG_GLOBAL='/dev/null')
def git(*args):
    return subprocess.run(['git', '-c', 'core.hooksPath=/dev/null', '-c',
        'core.fsmonitor=false', *args], check=True, stdout=subprocess.PIPE).stdout
with tempfile.NamedTemporaryFile() as bundle:
    import shutil
    shutil.copyfileobj(sys.stdin.buffer, bundle)
    bundle.flush()
    git('init', '--template=')
    git('-c', 'protocol.file.allow=always', 'fetch', '--no-tags', '--', bundle.name,
        'refs/heads/base:refs/heads/grade-base')
git('checkout', '--detach', '--force', sys.argv[1])
"""

OVERLAY = r"""
import json
import os
import stat
import subprocess
import sys
import tempfile

spec = json.loads(sys.argv[1])
os.environ.update(PATH='/usr/bin:/bin', GIT_CONFIG_NOSYSTEM='1', GIT_CONFIG_GLOBAL='/dev/null')
def git(*args):
    return subprocess.run(['git', '-c', 'core.hooksPath=/dev/null', '-c',
        'core.fsmonitor=false', *args], check=True, stdout=subprocess.PIPE).stdout

# Import hidden objects only after dependency preparation, with no network access.
with tempfile.NamedTemporaryFile() as bundle:
    import shutil
    shutil.copyfileobj(sys.stdin.buffer, bundle)
    bundle.flush()
    git('-c', 'protocol.file.allow=always', 'fetch', '--no-tags', '--', bundle.name,
        'refs/heads/overlay:refs/heads/grade-overlay')

# Resolve every requested path independently: missing paths, links and submodules fail closed.
files = {}
for path in spec['paths']:
    rows = git('ls-tree', '-rz', '--full-tree', spec['commit'], '--', ':(literal)' + path)
    if not rows:
        raise ValueError('overlay path missing')
    for row in rows.rstrip(b'\0').split(b'\0'):
        meta, name = row.split(b'\t', 1)
        mode, kind, oid = meta.split()
        parts = os.fsdecode(name).split('/')
        if (mode not in (b'100644', b'100755') or kind != b'blob'
            or any(p in ('', '.', '..', '.git', '.venv') for p in parts)):
            raise ValueError('overlay path refused')
        files[tuple(parts)] = mode, oid
root = os.open('.', os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW)
try:
    for parts, (mode, oid) in files.items():
        parent = os.dup(root)
        temporary = '.grade-' + os.urandom(12).hex()
        try:
            for part in parts[:-1]:
                try:
                    os.mkdir(part, 0o700, dir_fd=parent)
                except FileExistsError:
                    pass
                child = os.open(part, os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW, dir_fd=parent)
                os.close(parent)
                parent = child
            fd = os.open(temporary, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600, dir_fd=parent)
            with os.fdopen(fd, 'wb') as output:
                output.write(git('cat-file', 'blob', oid.decode('ascii')))
                os.fchmod(output.fileno(), 0o700 if mode == b'100755' else 0o600)
            os.rename(temporary, parts[-1], src_dir_fd=parent, dst_dir_fd=parent)
        finally:
            try:
                os.unlink(temporary, dir_fd=parent)
            except FileNotFoundError:
                pass
            os.close(parent)
finally:
    os.close(root)
"""


def data(task, folder, script, arguments, *, source=None):
    with tempfile.TemporaryFile() as empty:
        return launcher.capture(
            {
                "op": "run",
                "profile": "task",
                "shape": "data",
                "work_id": str(task["id"]),
                "run_id": "grade-data-" + secrets.token_hex(12),
                "env": {},
                "arguments": [script, *arguments],
            },
            folder / "data.out",
            folder / "data.err",
            runner.GIT_SECONDS,
            stdin=source if source is not None else empty,
        )


def run(task):
    document = task["request"]
    runner.RUNS.mkdir(mode=0o700, parents=True, exist_ok=True)
    with tempfile.TemporaryDirectory(dir=runner.RUNS, prefix="grade-") as temporary:
        folder = Path(temporary)
        repository = document["repository"]
        token = runner.installation_token(repository)
        runner.remove_copy(task)
        launcher.request({"op": "task-create", "work_id": str(task["id"])})
        # The two exact object ids may live on different branches of this repository.
        directory = folder / "git"
        delivery.git(None, "init", "--bare", "--template=", "--", str(directory))
        overlay = document["work"]["overlay"]
        delivery.git(
            directory,
            "fetch",
            "--no-tags",
            "--no-recurse-submodules",
            "--",
            runner.clone_url(repository),
            document["base_commit"] + ":refs/heads/base",
            overlay["commit"] + ":refs/heads/overlay",
            token=token,
        )
        del token
        bundle = folder / "grade.bundle"
        delivery.git(directory, "bundle", "create", str(bundle), "refs/heads/base")
        with bundle.open("rb") as source:
            if data(task, folder, IMPORT, [document["base_commit"]], source=source) != (
                0,
                "exited",
            ):
                raise runner.Refused("check_setup_failed")
        code, stopped = launcher.capture(
            {
                "op": "run",
                "profile": "task",
                "shape": "prepare",
                "work_id": str(task["id"]),
                "run_id": "grade-prepare-" + secrets.token_hex(12),
                "env": {"HOMELAB_PI_VERSION": ""},
                "arguments": [task_run.PREPARE, "prepare", *task["pinned_paths"]],
            },
            folder / "prepare.out",
            folder / "prepare.err",
            30 * 60,
        )
        if code != 0 or stopped != "exited":
            raise runner.Refused("prepare_failed")
        overlay_bundle = folder / "overlay.bundle"
        delivery.git(directory, "bundle", "create", str(overlay_bundle), "refs/heads/overlay")
        with overlay_bundle.open("rb") as source:
            if data(task, folder, OVERLAY, [json.dumps(overlay)], source=source) != (0, "exited"):
                raise runner.Refused("check_setup_failed")
        code, stopped = launcher.capture(
            {
                "op": "run",
                "profile": "task",
                "shape": "check",
                "work_id": str(task["id"]),
                "run_id": "grade-check-" + secrets.token_hex(12),
                "env": {"UV_NO_SYNC": "1", "PYTHONPYCACHEPREFIX": "/tmp/pycache"},
                "arguments": ['exec sh -c "$1" 2>&1', "check", task["check_command"]],
            },
            folder / "check.log",
            folder / "check.err",
            30 * 60,
        )
        if stopped != "exited":
            raise runner.Refused("check_timeout" if stopped == "timeout" else "check_failed")
        return {"exit_code": code, "tail": runner.check_output_of({"run_folder": str(folder)})}
