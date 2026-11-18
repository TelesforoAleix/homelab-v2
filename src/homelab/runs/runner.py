#!/usr/bin/python3 -I
"""Consume the existing task queue as uid 21020 through the fixed launcher.

Attempt ordering, retries, feedback, cost traces and eval retention follow the host runner.
Only the transport and the push credential change; no work-copy content is executed here.
"""

import argparse
import base64
import contextlib
import io
import json
import logging
import os
import re
import shutil
import signal
import subprocess
import tarfile
import tempfile
import threading
import time
import urllib.error
import urllib.request
from pathlib import Path

import psycopg

from homelab.runs import launcher, locking, task_run, usage
from homelab.runs.app import GitHubApp
from homelab.runs.app import Refused as GitHubTokenRefused
from homelab.settings import get_settings
from homelab.tasks import claim, finish
from homelab.tasks.executors import Executor, load_executors

VOLUME = Path("/srv/homelab")
WORK = VOLUME / "task-work"
RUNS = VOLUME / "runs"

CONFIG_REPOSITORY = "https://github.com/TelesforoAleix/homelab-v2.git"


DENY_LIST = Path("/run/secrets/deny-list")
DENY_CANARY = "homelab-deny-canary-7f3e"

POLL_SECONDS = 30
GIT_SECONDS = 600
CHECK_OUTPUT_BYTES = 16 * 1024
COMMIT_MESSAGE = ".task-commit-message"
REPOSITORY = re.compile(r"[A-Za-z0-9][A-Za-z0-9_-]*/[A-Za-z0-9][A-Za-z0-9._-]*\Z")
COMMIT = re.compile(r"[0-9a-f]{40,64}\Z")
# Failed checks get one retry in a fresh clone, carrying only the check output.
RETRIES = {"check_failed", "check_timeout"}
CHECK_LOG = re.compile(r"check(-[0-9]+)?\.log\Z")

logger = logging.getLogger("homelab-runs")
connection = None
database_mutex = threading.RLock()


class Refused(Exception):
    """A task that will not run; the message is the task's reason and carries no content."""


class LostLock(BaseException):
    """The session is gone; running without its lock is forbidden."""


class StoreUnavailable(Exception):
    """The task store did not answer."""


def store(module, *args, event=None):
    """Use the existing store and Procrastinate transactions on our locked DB session."""
    with database_mutex:
        if connection is None or connection.closed or connection.broken:
            raise LostLock("runner_database_lost")
        try:
            if module == "claim":
                return (
                    claim.recover(connection)
                    if args
                    else claim.claim(connection, legacy_roles=False)
                )
            return finish.record(connection, finish.parse(json.dumps(event).encode()))
        except psycopg.OperationalError:
            raise LostLock("runner_database_lost") from None
        except psycopg.Error:
            raise StoreUnavailable("database_error") from None


def record(event):
    """A result must land, or the task would run again: keep trying while the service lives."""
    while True:
        try:
            return store("finish", event=event)
        except StoreUnavailable as error:
            logger.warning("store unavailable event=%s error=%s", event["event"], error)
            time.sleep(POLL_SECONDS)


def git_env(**extra):
    return {
        "PATH": "/usr/bin:/bin",
        "HOME": "/nonexistent",
        "LC_ALL": "C",
        "GIT_CONFIG_NOSYSTEM": "1",
        "GIT_CONFIG_GLOBAL": os.devnull,
        "GIT_TERMINAL_PROMPT": "0",
        **extra,
    }


def clone_url(repository):
    return f"https://github.com/{repository}.git"


def push_url(repository):
    return clone_url(repository)


def installation_token(repository):
    return GitHubApp().token(repository)


def default_branch(repository):
    """Ask the remote, outside any working copy. None when it will not say."""
    try:
        result = subprocess.run(
            ["git", "ls-remote", "--symref", "--", clone_url(repository), "HEAD"],
            env=git_env(),
            cwd="/",
            capture_output=True,
            text=True,
            timeout=GIT_SECONDS,
            check=False,
        )
    except subprocess.TimeoutExpired:
        return None
    found = re.search(r"^ref: refs/heads/(\S+)\tHEAD$", result.stdout, re.M)
    return found[1] if result.returncode == 0 and found else None


def refuse_branch(task):
    """`main` and the default branch are never written, and an unknown default counts as one."""
    default = default_branch(task["repository"])
    if default is None:
        raise Refused("default_branch_unknown")
    if task["branch"] in {"main", default}:
        raise Refused("default_branch_refused")


def copy_path(task):
    return WORK / str(task["id"])


def remove_copy(task):
    launcher.request({"op": "task-remove", "work_id": str(task["id"])})


def fresh_copy(task):
    remove_copy(task)
    work_id = str(task["id"])
    launcher.request({"op": "task-create", "work_id": work_id})
    RUNS.mkdir(mode=0o700, parents=True, exist_ok=True)
    with tempfile.TemporaryDirectory(dir=RUNS, prefix="clone-") as temporary:
        request = {
            "op": "run",
            "profile": "task",
            "shape": "clone",
            "work_id": work_id,
            "run_id": "clone-" + os.urandom(12).hex(),
            "repository": task["repository"],
            "branch": task["branch"],
        }
        folder = Path(temporary)
        code, stopped = launcher.capture(request, folder / "out", folder / "err", GIT_SECONDS)
    return copy_path(task) if code == 0 and stopped == "exited" else None


def ticket(task, feedback):
    """The ticket. `feedback` is None, or ("check", output) of a lost try."""
    paths = "\n".join(f"- {path}" for path in task["allowed_files"])
    text = (
        f"# {task['title']}\n\n{task['goal']}\n\n"
        f"Change only these paths, or files below them:\n{paths}\n\n"
        f"The work is judged by this command: {task['check_command']}\n\n"
        f"When you are done, write a commit message for your change to `{COMMIT_MESSAGE}` at the "
        "top of the repository: a short subject line, then a body if it needs one. That file is "
        "not committed, and without it the work is thrown away.\n"
    )
    if feedback is not None and feedback[0] == "check":
        text += (
            "\nAn earlier try at this task failed its check, and was discarded. "
            f"The end of the check's output:\n\n```\n{feedback[1]}\n```\n"
        )
    return text


def configuration_snapshot(folder, configuration):
    """Fetch main independently of both the stack and task working copies."""
    clone = folder / "clone"
    env = git_env()
    commands = [
        [
            "git",
            "clone",
            "--quiet",
            "--no-checkout",
            "--template=",
            "--no-tags",
            "--single-branch",
            "--branch",
            "main",
            "--",
            CONFIG_REPOSITORY,
            str(clone),
        ],
        ["git", "-C", str(clone), "rev-parse", "--verify", "refs/remotes/origin/main^{commit}"],
    ]
    try:
        for command in commands:
            result = subprocess.run(
                command,
                env=env,
                cwd="/",
                stdout=subprocess.PIPE,
                stderr=subprocess.DEVNULL,
                timeout=GIT_SECONDS,
                check=True,
            )
        commit = result.stdout.decode("ascii").strip()
        if not COMMIT.fullmatch(commit):
            raise ValueError()
        result = subprocess.run(
            ["git", "-C", str(clone), "archive", commit, "configurations/"],
            env=env,
            cwd="/",
            stdout=subprocess.PIPE,
            stderr=subprocess.DEVNULL,
            timeout=GIT_SECONDS,
            check=True,
        )
        snapshot = folder / "snapshot"
        snapshot.mkdir()
        with tarfile.open(fileobj=io.BytesIO(result.stdout), mode="r:") as archive:
            archive.extractall(snapshot, filter="data")
        return snapshot / "configurations" / configuration, commit
    except (OSError, ValueError, UnicodeError, tarfile.TarError, subprocess.SubprocessError):
        raise Refused("configuration_snapshot_failed") from None


def run_ticket(task, copy, executor, feedback, *, number):
    command = [
        "--working-copy",
        str(copy),
        "--check",
        task["check_command"],
        "--author-name",
        os.environ["HOMELAB_TASK_AUTHOR_NAME"],
        "--author-email",
        os.environ["HOMELAB_TASK_AUTHOR_EMAIL"],
    ]
    if executor.harness:
        command += [
            "--executor",
            json.dumps(executor.selection()),
            "--cli-name",
            f"ticket-{task['id']}-{number}",
        ]
    for path in task["pinned_paths"]:
        command += ["--pin", path]
    for path in task["allowed_files"]:
        command += ["--allow", path]
    with tempfile.TemporaryDirectory(prefix="homelab-config-") as temporary:
        configuration_name = executor.configuration or "pi/base"
        if task.get("eval_set") is not None:
            name = task.get("eval_configuration")
            if not isinstance(name, str) or not re.fullmatch(
                r"[A-Za-z0-9][A-Za-z0-9._-]{0,63}", name
            ):
                raise Refused("eval_configuration_refused")
            configuration_name = "pi/" + name
            command += [
                "--eval-mark",
                json.dumps(
                    {"set": task["eval_set"], "case": task["eval_case"], "configuration": name}
                ),
            ]
        configuration, revision = configuration_snapshot(Path(temporary), configuration_name)
        command += ["--configuration", str(configuration), "--configuration-commit", revision]
        return invoke_ticket(task, feedback, command, executor.harness, number)


def invoke_ticket(task, feedback, command, cli, number):
    with tempfile.NamedTemporaryFile("w", encoding="utf-8") as stream:
        stream.write(ticket(task, feedback))
        stream.flush()
        try:
            run = task_run.run(task_run.parse([*command, "--ticket", stream.name]))
        except task_run.Refused:
            return {"ended": "run_refused"}
    if cli:
        logger.info(
            "cli task=%d number=%d ignored_changes=%d",
            task["id"],
            number,
            run.get("cli_ignored_changes", 0),
        )
    return run


def check_output_of(run):
    # The record names the last check's log: a fix round has a log of its own.
    log = run.get("check_log")
    if not CHECK_LOG.fullmatch(str(log)):
        log = "check.log"
    with open(Path(run["run_folder"]) / log, "rb") as stream:
        stream.seek(0, os.SEEK_END)
        stream.seek(max(stream.tell() - CHECK_OUTPUT_BYTES, 0))
        return stream.read().decode(errors="replace")


def repository_is_private(repository):
    """Only an explicit GitHub private response permits skipping the check."""
    try:
        request = urllib.request.Request(
            f"https://api.github.com/repos/{repository}",
            headers={"Accept": "application/vnd.github+json", "User-Agent": "homelab-task-runner"},
        )
        with urllib.request.urlopen(request, timeout=30) as response:
            data = json.load(response)
        return isinstance(data, dict) and data.get("private") is True
    except (OSError, ValueError, TypeError):
        return False


def private_content_check(task, copy, commit, *, private=None):
    """Inspect each outgoing commit without emitting the inspected content."""
    if private is True or (private is None and repository_is_private(task["repository"])):
        return None
    try:
        lines = DENY_LIST.read_text(encoding="utf-8").splitlines()
    except (OSError, UnicodeError):
        return "push_deny_list_unreadable"
    denied = [
        line.casefold() for line in lines if line.strip() and not line.lstrip().startswith("#")
    ]
    denied.append(DENY_CANARY)

    def hit(value):
        return any(item in value.casefold() for item in denied)

    if hit(task["branch"]):
        return "push_private_content: branch name"

    def git(*args):
        result = subprocess.run(
            ["git", "--git-dir", str(copy), "-c", "safe.directory=*", *args],
            cwd="/",
            env=git_env(),
            stdout=subprocess.PIPE,
            stderr=subprocess.DEVNULL,
            timeout=GIT_SECONDS,
            check=True,
        )
        return result.stdout

    try:
        base = f"refs/remotes/origin/{task['branch']}"
        git("merge-base", "--is-ancestor", base, commit)
        commits = git("rev-list", f"{base}..{commit}").decode("ascii").splitlines()
        for revision in commits:
            message = git("show", "-s", "--format=%B", revision).decode("utf-8", "replace")
            if hit(message):
                return "push_private_content: commit message"
            parents = git("rev-list", "--parents", "-n", "1", revision).decode("ascii").split()[1:]
            for parent in parents:
                names = git(
                    "diff",
                    "--no-ext-diff",
                    "--no-textconv",
                    "--no-renames",
                    "--name-only",
                    "-z",
                    parent,
                    revision,
                    "--",
                ).split(b"\0")
                for name in filter(None, names):
                    path = os.fsdecode(name)
                    patch = git(
                        "diff",
                        "--no-ext-diff",
                        "--no-textconv",
                        "--no-renames",
                        "--text",
                        "--unified=0",
                        parent,
                        revision,
                        "--",
                        ":(literal)" + path,
                    ).decode("utf-8", "replace")
                    number = None
                    for line in patch.split("\n"):
                        match = re.match(r"^@@ -\d+(?:,\d+)? \+(\d+)(?:,\d+)? @@", line)
                        if match:
                            number = int(match[1])
                        elif number is not None and line.startswith("+"):
                            if hit(line[1:]):
                                safe_path = "[redacted]" if hit(path) else path
                                safe_path = safe_path.encode("unicode_escape").decode("ascii")
                                return f"push_private_content: {safe_path}:{number}"
                            number += 1
                        elif number is not None and line.startswith(" "):
                            number += 1
    except (OSError, subprocess.SubprocessError, UnicodeError, ValueError):
        return "push_content_check_failed"
    return None


def log_reason(reason):
    return (
        "push_private_content" if reason and reason.startswith("push_private_content:") else reason
    )


@contextlib.contextmanager
def sealed_git_dir(copy):
    with tempfile.TemporaryDirectory(prefix="runs-git-") as temporary:
        directory = Path(temporary)
        source = Path(copy) / ".git"
        (directory / "HEAD").write_text("ref: refs/heads/unused\n")
        for name in ("objects", "refs"):
            (directory / name).symlink_to(source / name, target_is_directory=True)
        if (source / "packed-refs").is_file():
            (directory / "packed-refs").symlink_to(source / "packed-refs")
        yield directory


def push(task, copy, commit):
    """Fresh repo-scoped token, explicit URL/refspec, no copy configuration or hooks."""
    try:
        refuse_branch(task)
    except Refused as refusal:
        return f"push_{refusal}"
    if not COMMIT.fullmatch(commit):
        return "push_bad_commit"
    try:
        with sealed_git_dir(copy) as git_dir:
            refusal = private_content_check(task, git_dir, commit)
            if refusal:
                return refusal
            token = installation_token(task["repository"])
            authorization = base64.b64encode(("x-access-token:" + token).encode()).decode()
            result = subprocess.run(
                [
                    "git",
                    "--git-dir",
                    str(git_dir),
                    "-c",
                    "core.hooksPath=/dev/null",
                    "-c",
                    "credential.helper=",
                    "-c",
                    "safe.directory=*",
                    "-c",
                    "http.followRedirects=false",
                    "-c",
                    "protocol.allow=never",
                    "-c",
                    "protocol.https.allow=always",
                    "push",
                    "--porcelain",
                    "--no-verify",
                    "--no-follow-tags",
                    "--",
                    push_url(task["repository"]),
                    f"{commit}:refs/heads/{task['branch']}",
                ],
                env=git_env(
                    GIT_CONFIG_COUNT="1",
                    GIT_CONFIG_KEY_0="http.extraHeader",
                    GIT_CONFIG_VALUE_0="Authorization: Basic " + authorization,
                ),
                cwd="/",
                capture_output=True,
                text=True,
                timeout=GIT_SECONDS,
                check=False,
            )
    except (OSError, subprocess.SubprocessError, GitHubTokenRefused):
        return "push_failed"
    if result.returncode == 0:
        return "pushed"
    moved = re.search(r"^!\t.*\[rejected\]", result.stdout, re.M)
    return "push_rejected" if moved else "push_failed"


def attempt(task, executor, number, feedback):
    """One attempt on one route, from a fresh clone; returns (its end, feedback for a retry)."""
    route = executor.selection()
    started = time.monotonic()
    record(
        {
            "event": "begin_attempt",
            "task_id": task["id"],
            "number": number,
            "route": route,
            "executor": route,
        }
    )
    copy = fresh_copy(task)
    if copy is None:
        run = {"ended": "clone_failed"}
    else:
        run = run_ticket(task, copy, executor, feedback, number=number)
    ended = run["ended"]
    if ended == "committed":
        ended = "eval_kept" if task.get("eval_set") is not None else push(task, copy, run["commit"])
    keep_eval_run(task, run)
    tokens = run.get("tokens") or {}
    record(
        {
            "event": "end_attempt",
            "task_id": task["id"],
            "number": number,
            "route": route,
            "status": "done" if ended in {"pushed", "eval_kept"} else "failed",
            "reason": None if ended == "pushed" else ended,
            "run_id": run.get("run_id"),
            "check_exit_code": run.get("check_exit_code"),
            "configuration_hash": run.get("configuration_hash"),
            "steps": run.get("steps", 0),
            "fresh_tokens": tokens.get("fresh", 0),
            "cached_tokens": tokens.get("cached", 0),
            "output_tokens": tokens.get("output", 0),
            "seconds": round(time.monotonic() - started, 1),
            **known_cost(run),
        }
    )
    logger.info(
        "attempt task=%d number=%d purpose=%s ended=%s",
        task["id"],
        number,
        executor.harness or executor.configuration,
        log_reason(ended),
    )
    return ended, (("check", check_output_of(run)) if ended in RETRIES else None)


def keep_eval_run(task, run):
    """Move content within the private service-owned run store, including failed attempts."""
    if task.get("eval_set") is None or not run.get("run_folder"):
        return
    names = [task.get("eval_set"), task.get("eval_case"), run.get("run_id")]
    if any(
        not isinstance(name, str) or not re.fullmatch(r"[A-Za-z0-9][A-Za-z0-9._-]{0,63}", name)
        for name in names
    ) or not re.fullmatch(r"[0-9a-f]{64}", str(run.get("configuration_hash"))):
        raise Refused("eval_run_refused")
    source = RUNS / run["run_id"]
    if Path(run["run_folder"]) != source or source.is_symlink():
        raise Refused("eval_run_refused")
    parent = RUNS
    for name in ("eval", task["eval_set"], task["eval_case"], run["configuration_hash"]):
        parent = parent / name
        parent.mkdir(mode=0o700, exist_ok=True)
        if parent.is_symlink():
            raise Refused("eval_run_refused")
        parent.chmod(0o700)
    source.chmod(0o700)
    destination = parent / run["run_id"]
    source.rename(destination)
    run["run_folder"] = str(destination)
    export_eval_diff(destination, run["run_id"])


def export_eval_diff(folder, run_id):
    """Publish only the final round's patch for the API, by atomic replacement."""
    patches = []
    for path in folder.iterdir():
        match = re.fullmatch(r"diff(?:-([1-9][0-9]*))?\.patch", path.name)
        if match:
            patches.append((int(match[1] or 1), path))
    if not patches:
        return
    patch = max(patches)[1]
    if patch.is_symlink() or not patch.is_file():
        raise Refused("eval_run_refused")
    exports = RUNS / "eval-diffs"
    exports.mkdir(mode=0o750, exist_ok=True)
    target = exports / f"{run_id}.patch"
    if exports.is_symlink() or target.is_symlink():
        raise Refused("eval_run_refused")

    fd, name = tempfile.mkstemp(prefix=f".{run_id}.", dir=exports)
    temporary = Path(name)
    try:
        with os.fdopen(fd, "wb") as output, patch.open("rb") as source:
            shutil.copyfileobj(source, output)
            os.fchown(output.fileno(), -1, 10001)
            os.fchmod(output.fileno(), 0o640)
        temporary.replace(target)
    finally:
        temporary.unlink(missing_ok=True)


def known_cost(item):
    """The cost key only when the run measured it; an empty cost stays empty in the store."""
    return {} if item.get("cost_usd") is None else {"cost_usd": item["cost_usd"]}


def work(task):
    """Select only by table order and usage; checks get one fresh-copy retry."""
    from homelab.runs import slices

    choices = (
        [Executor(configuration="pi/" + task["eval_configuration"])]
        if task.get("eval_set") is not None
        else load_executors()["ticket"][task["level"]]
    )
    RUNS.mkdir(mode=0o700, parents=True, exist_ok=True)
    with tempfile.TemporaryDirectory(dir=RUNS, prefix="usage-") as temporary:
        folder = Path(temporary)
        cache = usage.ClaudeCache(RUNS / "claude-usage.json", lambda: slices.claude_probe(folder))
        readings = {}

        def read(harness):
            if harness not in readings:
                try:
                    readings[harness] = (
                        slices.codex_usage(folder) if harness == "codex" else cache.read()
                    )
                except launcher.Refused:
                    readings[harness] = None
            return readings[harness]

        number, feedback = task["next_attempt"], None
        check_retried = push_retried = False
        for reason, run_id, config_hash in task.get("attempt_history", []):
            if reason == "push_rejected":
                push_retried, check_retried, feedback = True, False, None
            elif reason in RETRIES:
                check_retried = True
                if not isinstance(run_id, str) or not re.fullmatch(r"[A-Za-z0-9._-]+", run_id):
                    raise Refused("check_feedback_unavailable")
                parent = RUNS
                if task.get("eval_set") is not None:
                    parent = parent / "eval" / task["eval_set"] / task["eval_case"] / config_hash
                feedback = ("check", check_output_of({"run_folder": str(parent / run_id)}))
        while True:
            executor, reset = usage.choose(choices, read, time.time())
            if executor is None:
                return "waiting", reset
            ended, check_feedback = attempt(task, executor, number, feedback)
            number += 1
            if ended == "usage_limit":
                # A limit reported during execution may be newer than the preflight reading.
                now = time.time()
                readings.pop(executor.harness, None)
                current = read(executor.harness)
                reset = current.reset if current and current.reset > now else now + 3600
                readings[executor.harness] = usage.Reading(100, reset, now)
                continue
            if ended in RETRIES and not check_retried:
                check_retried, feedback = True, check_feedback
                continue
            if ended == "push_rejected" and not push_retried:
                push_retried, feedback, check_retried = True, None, False
                continue
            if ended == "eval_kept":
                return "done", "eval_kept"
            return ("done", None) if ended == "pushed" else ("escalated", ended)


def run_task(task):
    logger.info("task started id=%d", task["id"])
    try:
        if not REPOSITORY.fullmatch(task["repository"]) or ".." in task["repository"]:
            raise Refused("repository_refused")
        refuse_branch(task)
        status, reason = work(task)
    except Refused as refusal:
        status, reason = "failed", str(refusal)
    except Exception as error:  # A bad task must not stop the queue; systemd sees only real faults.
        status, reason = "failed", f"runner_error_{type(error).__name__}"
    finally:
        remove_copy(task)
    if status == "waiting":
        record(
            {
                "event": "defer_usage",
                "task_id": task["id"],
                "job_id": task["job_id"],
                "reset": reason,
            }
        )
        logger.info("task waiting id=%d reason=usage_limit", task["id"])
        return
    record(
        {
            "event": "end_task",
            "task_id": task["id"],
            "job_id": task["job_id"],
            "status": status,
            "reason": reason,
        }
    )
    logger.info("task ended id=%d status=%s reason=%s", task["id"], status, log_reason(reason))
    if status in {"escalated", "failed"}:
        notify(task, status, log_reason(reason))


def notify(task, status, reason):
    from homelab.runs.notices import notify as send

    if not send(f"{task['id']} {task['slice']} {status} {reason}"):
        logger.warning("notice failed task=%d error=send_failed", task["id"])


def run_lock_free():
    locking.legacy_inactive()
    return True


def recover():
    launcher.request({"op": "task-recover"})
    from homelab.tasks import slices

    with database() as session:
        recovered = slices.recover(session)
    logger.info("recovered whole-runs=%s", recovered)
    for path in WORK.iterdir():
        launcher.request({"op": "task-remove", "work_id": path.name})
    logger.info("recovered jobs=%s", store("claim", "--recover"))


@contextlib.contextmanager
def database():
    with database_mutex:
        if connection is None or connection.closed or connection.broken:
            raise LostLock("runner_database_lost")
        try:
            yield connection
        except psycopg.Error:
            raise LostLock("runner_database_lost") from None


def turn(state):
    """One pass of the loop; True if it ran a task, so the next pass need not wait."""
    if not run_lock_free():
        return False
    if not state["recovered"]:
        recover()
        state["recovered"] = True
        if state.get("slices") is not None:
            state["slices"].start()
    try:
        task = store("claim")
    except StoreUnavailable:
        if state.get("slices") is not None:
            # An ambiguous ticket claim cannot trigger recovery beside live CLI workers.
            # Stop the service; the next lock holder recovers all queues together.
            raise LostLock("runner_database_lost") from None
        # A lost answer may have claimed a task no one holds. Nothing runs now: recover again.
        state["recovered"] = False
        raise
    if task is None:
        return False
    run_task(task)
    return True


def terminate(signum, frame):
    try:
        launcher.stop_all()
    finally:
        raise SystemExit(128 + signum)


def keep_lock(stop):
    # Loss of a session lock stops the live container promptly, before another claim.
    while not stop.wait(5):
        try:
            with database_mutex:
                connection.execute("SELECT 1")
        except psycopg.Error:
            if not stop.is_set():
                os.kill(os.getpid(), signal.SIGTERM)
            return


def main(argv=None):
    global connection
    parser = argparse.ArgumentParser()
    parser.parse_args(argv)
    if os.geteuid() != 21020:
        logger.error("runner refused reason=invalid_uid")
        return 2
    os.umask(0o077)
    logging.basicConfig(level=logging.INFO, format="%(message)s")
    logging.getLogger("httpx").setLevel(logging.WARNING)
    logging.getLogger("httpcore").setLevel(logging.WARNING)
    for signum in (signal.SIGTERM, signal.SIGHUP, signal.SIGINT):
        signal.signal(signum, terminate)
    stop = threading.Event()
    service = None
    try:
        with psycopg.connect(
            get_settings().database_url,
            autocommit=True,
            connect_timeout=10,
            keepalives=1,
            keepalives_idle=10,
            keepalives_interval=5,
            keepalives_count=2,
        ) as session:
            connection = session
            with locking.runner_lock(session), locking.host_run_lock():
                try:
                    keeper = threading.Thread(target=keep_lock, args=(stop,), daemon=True)
                    keeper.start()
                    state = {"recovered": False}
                    from homelab.tasks.runs import enabled_roles

                    if enabled_roles():
                        from homelab.runs.slice_loop import Service

                        service = Service(database, RUNS, locking.legacy_inactive)
                        state["slices"] = service
                    while True:
                        try:
                            worked = turn(state)
                        except StoreUnavailable as error:
                            logger.warning("store unavailable error=%s", error)
                            worked = False
                        if not worked:
                            time.sleep(POLL_SECONDS)
                finally:
                    # Keep both locks until all live launches have stopped.
                    stop.set()
                    if service is not None:
                        service.close()
                    launcher.stop_all()
    except (locking.Refused, LostLock, psycopg.Error) as error:
        logger.error(
            "runner refused reason=%s",
            str(error) if isinstance(error, locking.Refused) else "runner_database_lost",
        )
        return 2
    finally:
        stop.set()
        launcher.stop_all()
    return 0
