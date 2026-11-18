#!/usr/bin/python3 -I
"""Run one coding ticket in isolated containers and leave a record of what happened.

Everything inside a working copy is untrusted: the repository, the commands the model writes,
and whatever they leave behind. Nothing here runs git or interprets the copy's contents on the
host; that happens in the containers. The one thing from the copy that passes through the host is
the diff the check container writes, handled as bytes. Its `.git` is mounted read-only
in prepare, agent and check, and `.venv` in all but prepare. A passing check followed by
`--allow` reaches the commit step, whose rules enforce allowed paths and pins.
"""

import argparse
import hashlib
import json
import logging
import os
import re
import secrets
import shlex
import shutil
import stat
import sys
import time
from dataclasses import dataclass, field
from datetime import UTC, datetime
from pathlib import Path

from homelab.runs import launcher
from homelab.tasks.executors import Executor

VOLUME = Path("/srv/homelab")
RUNS = VOLUME / "runs"
TOKENS = VOLUME / "task-tokens"
COSTS = VOLUME / "task-costs"
IMAGE = "homelab-task:2"
DEFAULT_CONFIGURATION = Path("/app/configurations/pi/base")
TASK_NETWORK = "homelab-tasks"
PACKAGE_NETWORK = "homelab-packages"
PACKAGE_PROXY = "http://package-proxy:8888"
MODELS_URL = "http://litellm:4000/v1"
LITELLM_GID = 10001
MEMORY = "8g"
CPUS = "2"
PIDS = "512"
TMPFS = "4g"
OUTPUT_LIMIT = 256 * 1024 * 1024
TOKEN_GRACE_SECONDS = 60
MAX_REQUESTS = 100
PURPOSE = re.compile(
    r"(?:[a-z0-9][a-z0-9.-]{0,63}|eval/[a-z0-9][a-z0-9._-]{0,95}/[a-z0-9][a-z0-9._-]{0,95})\Z"
)
CHECK_STARTED = "homelab-task-run: check started"
COMMIT_MESSAGE = ".task-commit-message"
BUDGET_SPENT = "request budget is spent"
CLI_SECONDS = 20 * 60
logger = logging.getLogger("homelab-task-run")

# Reports the base commit on stdout: the only thing this step says to the host.
PREPARE = """set -eu
if [ -n "${HOMELAB_PI_VERSION:-}" ] && [ "$(pi --version)" != "$HOMELAB_PI_VERSION" ]; then
  echo "Pi version mismatch" >&2
  exit 3
fi
if [ -n "$(git --no-optional-locks status --porcelain)" ]; then
  echo 'working copy is not clean' >&2
  exit 3
fi
base=$(git rev-parse --verify 'HEAD^{commit}')
for path in "$@"; do
  git cat-file -e "$base:$path" || {
    echo "pinned path is not in the base commit: $path" >&2
    exit 3
  }
done
# A slow wheel download can time out; the files uv already fetched are not kept, so try again.
tries=0
until uv sync --locked >&2; do
  tries=$((tries + 1))
  if [ "$tries" -ge 3 ]; then
    echo 'uv sync failed 3 times' >&2
    exit 1
  fi
  sleep 5
done
printf '%s\\n' "$base"
"""

# The prompt arrives on stdin. Pi writes to its agent directory, so it lives on the tmpfs home.
PI = """set -eu
mkdir -p "$HOME/.pi/agent"
printf '%s' "$HOMELAB_PI_MODELS" > "$HOME/.pi/agent/models.json"
printf '%s' "$HOMELAB_PI_SETTINGS" > "$HOME/.pi/agent/settings.json"
unset HOMELAB_PI_MODELS HOMELAB_PI_SETTINGS
exec pi --mode json --no-session --no-approve --no-extensions --no-skills \
  --no-prompt-templates --no-themes --no-mcp --offline --tools TOOLS \
  --provider homelab --model "$HOMELAB_TASK_PURPOSE" --thinking off
"""
AGENT = PI.replace("TOOLS", "read,bash,edit,write")
# stdout carries the diff and stderr everything else, so the check's output cannot mix with it.
# The index and new objects live in /tmp because `.git` is read-only here.
CHECK = f"""set -eu
base=$1
command=$2
shift 2
judge=${{TMPDIR:-/tmp}}/judge
export GIT_INDEX_FILE="$judge/index"
export GIT_OBJECT_DIRECTORY="$judge/objects"
export GIT_ALTERNATE_OBJECT_DIRECTORIES="$PWD/.git/objects"
mkdir -p "$GIT_OBJECT_DIRECTORY"
echo /.venv/ > "$judge/exclude"
git read-tree "$base"
git -c core.excludesFile="$judge/exclude" add -A
git diff --cached --binary --no-ext-diff --no-textconv "$base"
for path in "$@"; do
  rm -rf -- "$path"
  git checkout "$base" -- "$path"
done
unset GIT_INDEX_FILE GIT_OBJECT_DIRECTORY GIT_ALTERNATE_OBJECT_DIRECTORIES
echo '{CHECK_STARTED}' >&2
exec sh -c "$command" >&2
"""

# The only step that can write `.git`, so it trusts nothing the tree or `.venv` could have supplied:
# the system Python in isolated mode, a fixed PATH, no git config but the clone's, hooks off. It
# never runs anything from the copy. commit-tree, unlike `git commit`, looks at no working tree or
# submodule. stdout is one line for the host: `commit <id>`, or `refused <reason>` with exit 3.
COMMIT = r"""
import json
import os
import stat
import subprocess
import sys
import tempfile

spec = json.loads(sys.argv[1])
base, allowed, pinned, message_file = (
    spec["base"], spec["allowed"], spec["pinned"], spec["message_file"]
)
os.environ.update(
    PATH="/usr/bin:/bin",
    GIT_CONFIG_NOSYSTEM="1",
    GIT_CONFIG_GLOBAL=os.devnull,
    GIT_COMMITTER_NAME=os.environ["GIT_AUTHOR_NAME"],
    GIT_COMMITTER_EMAIL=os.environ["GIT_AUTHOR_EMAIL"],
)


def git(*args, **kwargs):
    command = ["git", "-c", "core.hooksPath=/dev/null", "-c", "core.fsmonitor=false", *args]
    return subprocess.run(command, check=True, stdout=subprocess.PIPE, **kwargs).stdout


def refuse(reason, detail=""):
    print("refused", reason)
    print("refused:", reason, detail, file=sys.stderr)
    sys.exit(3)


def covers(entry, path):
    return path == entry or path.startswith(entry + "/")


def guarded(path):
    parts = path.split("/")
    return parts[-1] == "uv.lock" or any(part.startswith(".git") for part in parts)


def read_message():
    try:
        descriptor = os.open(message_file, os.O_RDONLY | os.O_NOFOLLOW | os.O_NONBLOCK)
    except OSError:
        refuse("no_message")
    with os.fdopen(descriptor, "rb") as stream:
        if not stat.S_ISREG(os.fstat(stream.fileno()).st_mode):
            refuse("no_message")
        message = stream.read(65536).strip()
    if not message:
        refuse("no_message")
    return message + b"\n"


message = read_message()
exclude = os.path.join(tempfile.mkdtemp(), "exclude")
with open(exclude, "w") as stream:
    stream.write("/.venv/\n/" + message_file + "\n")
git("-c", "core.excludesFile=" + exclude, "add", "-A")
raw = git("diff", "--cached", "--raw", "--no-renames", "-z", "--abbrev=40", base).split(b"\0")
if len(raw) < 2:
    refuse("no_change")
for meta, name in zip(raw[0::2], raw[1::2]):
    modes, path = meta.split()[:2], os.fsdecode(name)
    entries = [entry for entry in allowed if covers(entry, path)]
    if any(covers(pin, path) for pin in pinned):
        refuse("pinned", path)
    if not entries:
        refuse("outside_allowed", path)
    if guarded(path) and not any(guarded(entry) for entry in entries):
        refuse("guarded", path)
    if any(mode.lstrip(b":") == b"160000" for mode in modes):
        refuse("submodule", path)
tree = git("write-tree").decode().strip()
commit = git("commit-tree", tree, "-p", base, input=message).decode().strip()
git("update-ref", "HEAD", commit, base)
print("commit", commit)
"""


class LaunchSpec:
    def __init__(self, name, work_id, shape, variables):
        self.name, self.work_id, self.shape, self.variables = name, work_id, shape, variables
        self.arguments = []

    def __add__(self, arguments):
        self.arguments += arguments
        return self

    def value(self, environment):
        return {
            "op": "run",
            "profile": "task",
            "run_id": self.name,
            "work_id": self.work_id,
            "shape": self.shape,
            "env": {name: environment[name] for name in self.variables if "=" not in name},
            "arguments": self.arguments,
        }


class Refused(Exception):
    """An input the script will not run with; the message is for the caller."""


def terminate(signum, frame):
    raise SystemExit(128 + signum)


def working_copy(value):
    path = Path(value).resolve(strict=True)
    if "," in str(path) or "\n" in str(path):
        raise Refused("working copy path cannot be mounted")
    info = path.lstat()
    if not stat.S_ISDIR(info.st_mode):
        raise Refused("working copy is not a directory")
    if info.st_uid == 0:
        raise Refused("working copy must belong to an unprivileged account")
    require_real_directory(path / ".git")
    return path, info.st_uid, info.st_gid


def require_real_directory(path):
    # Docker follows a symlink in a bind source, which would mount a host path of its choosing.
    try:
        info = path.lstat()
    except FileNotFoundError:
        raise Refused(f"{path.name} is missing from the working copy") from None
    if not stat.S_ISDIR(info.st_mode):
        raise Refused(f"{path.name} must be a real directory")


def pinned_path(value):
    parts = value.split("/")
    if (
        not value
        or "\n" in value
        or value.startswith("-")
        or any(part in {"", ".", ".."} for part in parts)
        or parts[0] in {".git", ".venv"}
    ):
        raise Refused(f"invalid pinned path: {value!r}")
    return value


def container(
    name,
    copy,
    uid,
    gid,
    network,
    *,
    venv_read_only,
    git_read_only=True,
    work_read_only=False,
    env=(),
    stdin=False,
    entry=("sh", "-c"),
    entrypoint=None,
):
    shape = (
        "commit"
        if entrypoint
        else "prepare"
        if not venv_read_only
        else "agent"
        if stdin
        else "check"
    )
    return LaunchSpec(name, Path(copy).name, shape, env)


def capture(command, name, stdout_path, stderr_path, seconds, *, stdin=None, env=None):
    return launcher.capture(
        command.value(env or {}), stdout_path, stderr_path, seconds, stdin=stdin, limit=OUTPUT_LIMIT
    )


def clear_folder(folder, mode):
    folder.mkdir(parents=True, exist_ok=True)
    for path in folder.iterdir():
        path.unlink()


def clear_tokens():
    clear_folder(TOKENS, 0o750)


def clear_costs():
    # LiteLLM appends here as it runs as the group, so the folder is group-writable.
    clear_folder(COSTS, 0o770)


def read_cost(token):
    """What the proxy billed one container: the sum of its lines, or None if any is unknown.

    A partial sum is never a cost. The file is removed whatever it held.
    """
    path = COSTS / hashlib.sha256(token.encode()).hexdigest()
    try:
        lines = path.read_text().splitlines()
    except (OSError, UnicodeError):
        return None
    finally:
        path.unlink(missing_ok=True)
    total = 0
    for line in lines:
        try:
            cost = json.loads(line)["cost_usd"]
        except (ValueError, KeyError, TypeError):
            return None
        if isinstance(cost, bool) or not isinstance(cost, (int, float)) or not cost >= 0:
            return None
        total += cost
    return total if lines else None


def total_cost(costs):
    """The sum over several containers; None when there are none or any is unknown."""
    if not costs or any(cost is None for cost in costs):
        return None
    return sum(costs)


def valid_eval_mark(mark):
    if not isinstance(mark, dict) or set(mark) != {"set", "case", "executor"}:
        return False
    try:
        from homelab.api.run_contract import RunEval

        return RunEval.model_validate(mark).executor is not None
    except ValueError:
        return False


def issue_token(token, purpose, seconds, max_requests, *, eval_mark=None):
    if purpose.startswith("eval/") and not valid_eval_mark(eval_mark):
        raise Refused("eval purpose requires an eval mark")
    path = TOKENS / hashlib.sha256(token.encode()).hexdigest()
    temporary = path.with_suffix(".tmp")
    temporary.write_text(
        json.dumps(
            {
                "purposes": [purpose],
                "expires_at": int(time.time()) + seconds,
                "max_requests": max_requests,
            }
        )
    )
    os.chown(temporary, -1, LITELLM_GID)
    temporary.chmod(0o640)
    os.replace(temporary, path)
    return path


def pi_models(purpose, context_window=128000, max_tokens=16384):
    return json.dumps(
        {
            "providers": {
                "homelab": {
                    "baseUrl": MODELS_URL,
                    "api": "openai-completions",
                    "apiKey": "$HOMELAB_TASK_TOKEN",
                    # Keep to the OpenAI request fields the proxy accepts; it sets the effort.
                    "compat": {
                        "supportsStore": False,
                        "supportsDeveloperRole": False,
                        "supportsReasoningEffort": False,
                        "maxTokensField": "max_tokens",
                    },
                    "models": [
                        {
                            "id": purpose,
                            "name": purpose,
                            "reasoning": False,
                            "input": ["text"],
                            "contextWindow": context_window,
                            "maxTokens": max_tokens,
                            "cost": {"input": 0, "output": 0, "cacheRead": 0, "cacheWrite": 0},
                        }
                    ],
                }
            }
        }
    )


def pi_settings(shell=True):
    # Keeps the token out of ordinary command output. Not a boundary: same user, same container.
    settings = {"enableInstallTelemetry": False}
    if shell:
        settings["shellCommandPrefix"] = "unset HOMELAB_TASK_TOKEN"
    return json.dumps(settings)


def summarize_events(path):
    """Steps, tokens and how the last model reply ended. Pi exits 0 even when the model failed."""
    steps = 0
    last_stop = None
    budget_spent = False
    compactions = retries = 0
    end_event = None
    tokens = {"fresh": 0, "output": 0, "cached": 0, "cache_write": 0}
    with open(path, "rb") as stream:
        for line in stream:
            try:
                event = json.loads(line)
                kind = event["type"]
                message = event.get("message") or {}
            except (ValueError, KeyError, TypeError, AttributeError):
                continue
            if kind in {
                "agent_start",
                "compaction_start",
                "auto_retry_start",
                "summarization_retry_scheduled",
                "summarization_retry_attempt_start",
            }:
                end_event = None
            if kind == "agent_end":
                end_event = {"type": kind, "willRetry": event.get("willRetry")}
            elif kind == "agent_settled":
                end_event = {"type": kind, "aborted": event.get("aborted")}
            if kind == "compaction_start":
                compactions += 1
            if kind in {"auto_retry_start", "summarization_retry_attempt_start"}:
                retries += 1
            if kind == "turn_end":
                steps += 1
            usage = {}
            if kind == "message_end" and message.get("role") == "assistant":
                last_stop = message.get("stopReason")
                budget_spent = BUDGET_SPENT in str(message.get("errorMessage"))
                usage = message.get("usage") or {}
            elif kind == "compaction_end":
                usage = (event.get("result") or {}).get("usage") or {}
            tokens["fresh"] += usage.get("input", 0)
            tokens["output"] += usage.get("output", 0)
            tokens["cached"] += usage.get("cacheRead", 0)
            tokens["cache_write"] += usage.get("cacheWrite", 0)
    return {
        "steps": steps,
        "tokens": tokens,
        "last_stop": last_stop,
        "budget_spent": budget_spent,
        "compactions": compactions,
        "retries": retries,
        "end_event": end_event,
    }


def scrub(paths, token):
    for path in paths:
        if path.exists():
            content = path.read_bytes()
            if token.encode() in content:
                path.write_bytes(content.replace(token.encode(), b"[task-token]"))


@dataclass
class Context:
    """What every step of one run shares. `issued` holds each model token the run ever made."""

    name: str
    folder: Path
    owner: tuple
    seconds: int
    issued: list = field(default_factory=list)
    worker_costs: list = field(default_factory=list)
    last_cost: object = None
    configuration: object = None
    eval_mark: object = None


def suffix(number):
    """The first round keeps its plain file names; a later round's carry its number."""
    return "" if number == 1 else f"-{number}"


def ask_model(context, step, purpose, max_requests, script, prompt, files):
    """One Pi container with a token of its own, removed when the container ends."""
    token = secrets.token_urlsafe(32)
    context.issued.append(token)
    grant = issue_token(
        token,
        purpose,
        context.seconds + TOKEN_GRACE_SECONDS,
        max_requests,
        eval_mark=context.eval_mark,
    )
    name = f"{context.name}-{step}"
    config = context.configuration
    if config is not None:
        script = pi_script(config)
    try:
        with open(prompt, "rb") as stdin:
            return capture(
                container(
                    name,
                    *context.owner,
                    TASK_NETWORK,
                    venv_read_only=True,
                    env=[
                        "HOMELAB_TASK_TOKEN",
                        "HOMELAB_TASK_PURPOSE",
                        "HOMELAB_PI_MODELS",
                        "HOMELAB_PI_SETTINGS",
                    ],
                    stdin=True,
                )
                + [script],
                name,
                context.folder / files[0],
                context.folder / files[1],
                context.seconds,
                stdin=stdin,
                env={
                    "HOMELAB_TASK_TOKEN": token,
                    "HOMELAB_TASK_PURPOSE": purpose,
                    "HOMELAB_PI_MODELS": pi_models(
                        purpose,
                        *(config["context_window"], config["max_tokens"])
                        if config is not None
                        else (128000, 16384),
                    ),
                    "HOMELAB_PI_SETTINGS": pi_settings(),
                },
            )
    finally:
        grant.unlink(missing_ok=True)
        context.last_cost = read_cost(token)
        context.worker_costs.append(context.last_cost)


def add_usage(record, summary):
    """The worker's totals over all its runs; the last reply's stop is the last run's."""
    record["steps"] += summary["steps"]
    record["compactions"] += summary["compactions"]
    record["retries"] += summary["retries"]
    record["end_event"] = summary["end_event"]
    record["last_stop"] = summary["last_stop"]
    record["budget_spent"] = summary["budget_spent"]
    total = record["tokens"] or dict.fromkeys(summary["tokens"], 0)
    record["tokens"] = {key: total[key] + value for key, value in summary["tokens"].items()}


def tree_files(root, prefix=""):
    """Open regular files through directory descriptors; never traverse a link, even in a race.

    Yield other entries without a descriptor so ignored changes can still be counted. Git and
    the prepared environment are machinery, never inputs to the CLI or results from it.
    """
    for name in os.listdir(root):
        path = prefix + name
        info = os.stat(name, dir_fd=root, follow_symlinks=False)
        if name in {".git", ".venv"}:
            yield path, None, info
            continue
        if stat.S_ISDIR(info.st_mode):
            try:
                child = os.open(name, os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW, dir_fd=root)
            except OSError:
                yield path, None, info
                continue
            try:
                yield from tree_files(child, path + "/")
            finally:
                os.close(child)
        elif stat.S_ISREG(info.st_mode):
            try:
                source = os.open(name, os.O_RDONLY | os.O_NOFOLLOW | os.O_NONBLOCK, dir_fd=root)
            except OSError:
                yield path, None, info
                continue
            try:
                current = os.fstat(source)
                yield path, source if stat.S_ISREG(current.st_mode) else None, current
            finally:
                os.close(source)
        else:
            yield path, None, info


def data_file(root, path, source, info, owner):
    """Install bytes as a new regular inode, without following destination links or hardlinks."""
    parent = os.dup(root)
    temporary = ".cli-copy-" + secrets.token_hex(8)
    try:
        *folders, name = path.split("/")
        for folder in folders:
            try:
                os.mkdir(folder, 0o700, dir_fd=parent)
                os.chown(folder, *owner, dir_fd=parent, follow_symlinks=False)
            except FileExistsError:
                pass
            child = os.open(folder, os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW, dir_fd=parent)
            os.close(parent)
            parent = child
        target = os.open(temporary, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600, dir_fd=parent)
        with os.fdopen(target, "wb") as output, os.fdopen(os.dup(source), "rb") as stream:
            stream.seek(0)
            shutil.copyfileobj(stream, output)
            os.fchown(output.fileno(), *owner)
            os.fchmod(output.fileno(), 0o700 if info.st_mode & 0o111 else 0o600)
        os.rename(temporary, name, src_dir_fd=parent, dst_dir_fd=parent)
    finally:
        try:
            os.unlink(temporary, dir_fd=parent)
        except FileNotFoundError:
            pass
        os.close(parent)


def fingerprint(source, info):
    if source is None:
        return (stat.S_IFMT(info.st_mode),)
    with os.fdopen(os.dup(source), "rb") as stream:
        stream.seek(0)
        digest = hashlib.file_digest(stream, "sha256").digest()
    os.lseek(source, 0, os.SEEK_SET)
    return bool(info.st_mode & 0o111), digest


def covers(entry, path):
    return path == entry or path.startswith(entry + "/")


def cli_worker(args, context, pins, ticket, record):
    from homelab.runs.cli import worker

    return worker(args, context, pins, ticket, record)


CONFIG_FIELDS = {
    "pi_version",
    "image",
    "flags",
    "tools",
    "purpose",
    "context_window",
    "max_tokens",
    "thinking",
    "limits",
    "system_prompt",
    "append",
}
SAFE_FLAGS = {
    "--no-session",
    "--no-approve",
    "--no-extensions",
    "--no-skills",
    "--no-prompt-templates",
    "--no-themes",
    "--no-mcp",
    "--offline",
}
SAFE_TOOLS = {"read", "bash", "edit", "write", "grep", "find", "ls"}


def load_configuration(folder):
    """Read public regular files once, refusing links and unexpected entries."""
    try:
        files = {}

        def read_file(directory, name, relative):
            descriptor = os.open(
                name, os.O_RDONLY | os.O_NOFOLLOW | os.O_NONBLOCK, dir_fd=directory
            )
            with os.fdopen(descriptor, "rb") as stream:
                if not stat.S_ISREG(os.fstat(stream.fileno()).st_mode):
                    raise ValueError()
                files[relative] = stream.read()

        root = os.open(folder, os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW)
        try:
            for name in os.listdir(root):
                if name == "append":
                    directory = os.open(
                        name, os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW, dir_fd=root
                    )
                    try:
                        for child in os.listdir(directory):
                            if not re.fullmatch(r"[A-Za-z0-9_.-]+\.md", child):
                                raise ValueError()
                            read_file(directory, child, "append/" + child)
                    finally:
                        os.close(directory)
                elif name in {"configuration.json", "notes.md", "system-prompt.md"}:
                    read_file(root, name, name)
                else:
                    raise ValueError()
        finally:
            os.close(root)
        config = json.loads(files["configuration.json"])
        if not isinstance(config, dict) or set(config) != CONFIG_FIELDS:
            raise ValueError()
        flags = config["flags"]
        if (
            not isinstance(flags, list)
            or flags[:2] != ["--mode", "json"]
            or any(not isinstance(flag, str) or flag not in SAFE_FLAGS for flag in flags[2:])
            or len(set(flags[2:])) != len(flags[2:])
        ):
            raise ValueError()
        if (
            not isinstance(config["tools"], list)
            or not config["tools"]
            or any(not isinstance(tool, str) or tool not in SAFE_TOOLS for tool in config["tools"])
            or len(set(config["tools"])) != len(config["tools"])
        ):
            raise ValueError()
        if not isinstance(config["purpose"], str) or not PURPOSE.fullmatch(config["purpose"]):
            raise ValueError()
        if not isinstance(config["pi_version"], str) or not re.fullmatch(
            r"[0-9]+\.[0-9]+\.[0-9]+", config["pi_version"]
        ):
            raise ValueError()
        if not isinstance(config["image"], str) or not re.fullmatch(
            r"[a-z0-9][a-z0-9._/:@-]{0,200}", config["image"]
        ):
            raise ValueError()
        if config["thinking"] not in ("off", "minimal", "low", "medium", "high", "xhigh"):
            raise ValueError()
        if not isinstance(config["limits"], dict) or set(config["limits"]) != {
            "requests",
            "minutes",
        }:
            raise ValueError()
        for value in (config["context_window"], config["max_tokens"], *config["limits"].values()):
            if type(value) is not int or value <= 0:
                raise ValueError()
        if config["max_tokens"] > config["context_window"]:
            raise ValueError()
        system = config["system_prompt"]
        if system != "default" and (system != "system-prompt.md" or system not in files):
            raise ValueError()
        append = config["append"]
        if not isinstance(append, list) or any(
            not isinstance(x, str) or x not in files or not x.startswith("append/") for x in append
        ):
            raise ValueError()
        for content in files.values():
            if "\0" in content.decode("utf-8"):
                raise ValueError()
        manifest = {
            path: hashlib.sha256(content).hexdigest() for path, content in sorted(files.items())
        }
        digest = hashlib.sha256(
            json.dumps(list(manifest.items()), separators=(",", ":"), ensure_ascii=True).encode()
        ).hexdigest()
        config["_files"] = files
        return config, manifest, digest
    except (OSError, ValueError, KeyError, TypeError, UnicodeError):
        raise Refused("invalid configuration") from None


def pi_script(config):
    # Prompt bytes are shell-quoted literals, never evaluated as shell code.
    args = [
        *config["flags"],
        "--tools",
        ",".join(config["tools"]),
        "--provider",
        "homelab",
        "--model",
    ]
    command = "exec pi " + " ".join(shlex.quote(x) for x in args)
    command = command.replace(
        "--no-skills --no-prompt-templates", "--no-skills   --no-prompt-templates"
    )
    command = command.replace(" --provider homelab", "   --provider homelab")
    command += ' "$HOMELAB_TASK_PURPOSE" --thinking ' + shlex.quote(config["thinking"])
    if config["system_prompt"] != "default":
        command += " --system-prompt " + shlex.quote(
            config["_files"][config["system_prompt"]].decode()
        )
    if config["append"]:
        appended = "\n\n".join(config["_files"][path].decode() for path in config["append"])
        command += " --append-system-prompt " + shlex.quote(appended)
    return PI[: PI.index("exec pi")] + command + "\n"


def image_digest(image):
    if image != "homelab-task:2":
        raise Refused("configuration image unavailable")
    return launcher.request({"op": "task-image"})["image"]


def run(args):
    global IMAGE
    config, manifest, config_hash = load_configuration(
        getattr(args, "configuration", DEFAULT_CONFIGURATION)
    )
    explicit = getattr(args, "_explicit", set())
    if "--purpose" not in explicit and hasattr(args, "_explicit"):
        args.purpose = config["purpose"]
    if "--time-limit" not in explicit and hasattr(args, "_explicit"):
        args.time_limit = config["limits"]["minutes"]
    if "--max-requests" not in explicit and hasattr(args, "_explicit"):
        args.max_requests = config["limits"]["requests"]
    eval_mark = getattr(args, "eval_mark", None)
    if eval_mark is not None and not valid_eval_mark(eval_mark):
        raise Refused("invalid eval mark")
    if any(
        isinstance(purpose, str) and purpose.startswith("eval/") for purpose in (args.purpose,)
    ) and not valid_eval_mark(eval_mark):
        raise Refused("eval purpose requires an eval mark")
    IMAGE = config["image"]
    digest = image_digest(IMAGE)
    IMAGE = digest
    copy, uid, gid = working_copy(args.working_copy)
    if args.executor is not None:
        if args.executor.harness is None:
            raise Refused("a CLI worker needs a harness executor")
        if not args.cli_name or not re.fullmatch(r"ticket-[0-9]+-[0-9]+", args.cli_name):
            raise Refused("invalid CLI attempt name")
        if not args.allow:
            raise Refused("a CLI worker needs --allow")
    elif args.cli_name is not None:
        raise Refused("a CLI attempt name needs --executor")
    pins = [pinned_path(value) for value in args.pin]
    if not PURPOSE.fullmatch(args.purpose):
        raise Refused("invalid purpose")
    allowed = [pinned_path(value) for value in args.allow]
    if allowed and not (args.author_name and args.author_email):
        raise Refused("a commit needs an author")
    if args.time_limit <= 0:
        raise Refused("time limit must be positive")
    if args.max_requests <= 0:
        raise Refused("request budget must be positive")
    if not VOLUME.is_dir():
        raise Refused("data volume is unavailable")
    if not Path(args.ticket).read_bytes().strip():
        raise Refused("ticket is empty")

    if getattr(args, "configuration_commit", None) is not None and not re.fullmatch(
        r"[0-9a-f]{40,64}", args.configuration_commit
    ):
        raise Refused("invalid configuration commit")
    run_id = datetime.now(UTC).strftime("%Y%m%dT%H%M%SZ-") + secrets.token_hex(3)
    RUNS.mkdir(mode=0o700, parents=True, exist_ok=True)
    folder = RUNS / run_id
    folder.mkdir(mode=0o700)
    clear_tokens()
    clear_costs()
    started = time.monotonic()
    record = {
        "configuration_hash": config_hash,
        "configuration_manifest": {
            "files": manifest,
            "pi_version": config["pi_version"],
            "image_digest": digest,
        },
        "configuration_commit": getattr(args, "configuration_commit", None),
        "run_id": run_id,
        "working_copy": str(copy),
        "purpose": args.purpose,
        "pinned": pins,
        "check_command": args.check,
        "time_limit_minutes": args.time_limit,
        "started_at": datetime.now(UTC).isoformat(timespec="seconds"),
        "base_commit": None,
        "steps": 0,
        "tokens": None,
        "cost_usd": None,
        "last_stop": None,
        "compactions": 0,
        "retries": 0,
        "end_event": None,
        "check_exit_code": None,
        "check_log": "check.log",
        "commit": None,
        "ended": "interrupted",
    }
    context = Context(
        f"homelab-task-{run_id.lower()}", folder, (copy, uid, gid), args.time_limit * 60
    )
    context.configuration = config
    context.eval_mark = eval_mark
    if args.executor is not None:
        record["executor"] = args.executor.selection()
        record["configuration_hash"] = args.executor.fingerprint()
        record["tokens"] = dict.fromkeys(("fresh", "output", "cached", "cache_write"), 0)
        record.pop("cost_usd")
    try:
        ended = execute(args, record, context, pins)
        if ended == "check_passed" and allowed:
            ended = commit(args, record, folder, context.owner, pins, allowed)
        record["ended"] = ended
    finally:
        if args.executor is None:
            record["cost_usd"] = total_cost(context.worker_costs)
        for token in context.issued:
            scrub(sorted(folder.iterdir()), token)
        record["ended_at"] = datetime.now(UTC).isoformat(timespec="seconds")
        record["seconds"] = round(time.monotonic() - started, 1)
        (folder / "run.json").write_text(json.dumps(record, indent=2) + "\n")
    return {"run_folder": str(folder), **record}


def execute(args, record, context, pins):
    """Prepare, then the first agent run and check; returns why the run ended."""
    folder = context.folder
    code, stopped = capture(
        container(
            f"{context.name}-prepare",
            *context.owner,
            PACKAGE_NETWORK,
            venv_read_only=False,
            env=[f"HTTPS_PROXY={PACKAGE_PROXY}", "UV_HTTP_TIMEOUT=120", "HOMELAB_PI_VERSION"],
        )
        + [PREPARE, "prepare", *pins],
        f"{context.name}-prepare",
        folder / "base-commit",
        folder / "prepare.log",
        context.seconds,
        env={"HOMELAB_PI_VERSION": context.configuration["pi_version"]},
    )
    base = (folder / "base-commit").read_text().strip()
    (folder / "base-commit").unlink()
    if stopped != "exited":
        return f"prepare_{stopped}"
    if code != 0 or not re.fullmatch(r"[0-9a-f]{40,64}", base):
        return "prepare_failed"
    record["base_commit"] = base
    return work_round(args, record, context, pins, 1, args.ticket, args.max_requests)


def work_round(args, record, context, pins, number, ticket, budget):
    """One agent run and the check on what it left; returns why the round ended."""
    folder, tail = context.folder, suffix(number)
    step = "" if number == 1 else str(number)
    agent_ended = None
    if args.executor is not None:
        agent_ended = cli_worker(args, context, pins, ticket, record)
        if agent_ended is not None:
            return agent_ended
    else:
        code, stopped = ask_model(
            context,
            f"agent{step}",
            args.purpose,
            budget,
            AGENT,
            ticket,
            (f"events{tail}.jsonl", f"agent{tail}.log"),
        )
        summary = summarize_events(folder / f"events{tail}.jsonl")
        add_usage(record, summary)
        if stopped != "exited":
            agent_ended = f"agent_{stopped}"
        elif summary["last_stop"] == "error":
            agent_ended = "model_error"
        elif summary["budget_spent"]:
            agent_ended = "agent_budget_spent"
        elif (
            code != 0
            or summary["last_stop"] != "stop"
            or summary["end_event"] != {"type": "agent_settled", "aborted": False}
        ):
            agent_ended = "agent_failed"

    # This container also writes the diff, so it runs however the agent ended.
    name = f"{context.name}-check{step}"
    code, stopped = capture(
        container(
            name,
            *context.owner,
            "none",
            venv_read_only=True,
            env=["UV_NO_SYNC", "PYTHONPYCACHEPREFIX"],
        )
        + [CHECK, "check", record["base_commit"], args.check, *pins],
        name,
        folder / f"diff{tail}.patch",
        folder / f"check{tail}.log",
        context.seconds,
        env={"UV_NO_SYNC": "1", "PYTHONPYCACHEPREFIX": "/tmp/pycache"},
    )
    check_started = CHECK_STARTED.encode() in (folder / f"check{tail}.log").read_bytes()
    record["check_exit_code"] = code if check_started else None
    record["check_log"] = f"check{tail}.log"
    if agent_ended is not None:
        return agent_ended
    if stopped != "exited":
        return f"check_{stopped}"
    if not check_started:
        return "check_setup_failed"
    return "check_passed" if code == 0 else "check_failed"


def commit(args, record, folder, owner, pins, allowed):
    """The fourth step, for a passing check only; returns why the run ended."""
    name = f"homelab-task-{record['run_id'].lower()}-commit"
    require_real_directory(owner[0] / ".git")
    spec = {
        "base": record["base_commit"],
        "allowed": allowed,
        "pinned": pins,
        "message_file": COMMIT_MESSAGE,
    }
    # `.venv/bin` leads the image's PATH and prepare ran repository code, so name everything.
    code, stopped = capture(
        container(
            name,
            *owner,
            "none",
            venv_read_only=True,
            git_read_only=False,
            env=["PATH=/usr/bin:/bin", "GIT_AUTHOR_NAME", "GIT_AUTHOR_EMAIL"],
            entrypoint="/usr/bin/python3",
            entry=("-I", "-c"),
        )
        + [COMMIT, json.dumps(spec)],
        name,
        folder / "commit.out",
        folder / "commit.log",
        args.time_limit * 60,
        env={"GIT_AUTHOR_NAME": args.author_name, "GIT_AUTHOR_EMAIL": args.author_email},
    )
    result = (folder / "commit.out").read_text(errors="replace").strip()
    (folder / "commit.out").unlink()
    if stopped != "exited":
        return f"commit_{stopped}"
    if code == 0 and (found := re.fullmatch(r"commit ([0-9a-f]{40,64})", result)):
        record["commit"] = found[1]
        return "committed"
    if code == 3 and (found := re.fullmatch(r"refused ([a-z_]+)", result)):
        return f"commit_refused_{found[1]}"
    return "commit_failed"


def parse(argv):
    parser = argparse.ArgumentParser(prog="homelab-task-run", description=__doc__.splitlines()[0])
    parser.add_argument("--configuration", type=Path, default=DEFAULT_CONFIGURATION)
    parser.add_argument("--configuration-commit", help=argparse.SUPPRESS)
    parser.add_argument("--eval-mark", type=json.loads, help=argparse.SUPPRESS)
    parser.add_argument("--working-copy", required=True)
    parser.add_argument("--ticket", required=True, help="file holding the ticket")
    parser.add_argument("--check", required=True, help="done-check shell command")
    parser.add_argument("--pin", action="append", default=[], help="path restored before the check")
    parser.add_argument("--purpose", default="task-worker")
    parser.add_argument(
        "--executor",
        type=lambda value: Executor.model_validate_json(value),
        help="replace the Pi worker with a configured CLI harness",
    )
    parser.add_argument("--cli-name", help="assigned ticket-<task id>-<attempt> directory name")
    parser.add_argument("--time-limit", type=int, default=30, help="minutes per container")
    parser.add_argument(
        "--max-requests", type=int, default=MAX_REQUESTS, help="model requests for this run"
    )
    parser.add_argument(
        "--allow", action="append", default=[], help="path a commit may touch; asks for a commit"
    )
    parser.add_argument("--author-name", help="commit author, with --allow")
    parser.add_argument("--author-email", help="commit author, with --allow")
    args = parser.parse_args(argv)
    args._explicit = {item.split("=", 1)[0] for item in (sys.argv[1:] if argv is None else argv)}
    return args
