"""Whole-copy CLI turns. Only launcher containers ever write a mounted folder."""

import json
import re
import secrets
import tempfile
import time
from dataclasses import dataclass, field
from datetime import UTC, datetime
from uuid import uuid4

from homelab.runs import launcher, usage
from homelab.runs.app import GitHubApp
from homelab.runs.delivery import (
    Refused,
    authorization,
    checked_bundle,
    push_checked,
    remote_refs,
)
from homelab.tasks.executors import Executor

SCHEMA = {
    "type": "object",
    "properties": {
        "kind": {"type": "string", "enum": ["handover", "question", "escalation", "waiting"]},
        "body": {"type": "string"},
    },
    "required": ["kind", "body"],
    "additionalProperties": False,
}
SCHEMA_PATH = "/work/.git/homelab-output-schema.json"
# Whole-run CLI profiles have no turn deadline; ticket/probe/check deadlines stay separate.
TURN_SECONDS = None
GIT_SECONDS = 600

# Fixed system Git, in an offline data container, never host code from the copy.
CHECKOUT = """import json, os, subprocess, sys
spec = json.loads(sys.argv[1])
env = {'PATH': '/usr/bin:/bin', 'HOME': '/nonexistent', 'GIT_CONFIG_NOSYSTEM': '1',
       'GIT_CONFIG_GLOBAL': '/dev/null', 'GIT_TERMINAL_PROMPT': '0'}
def git(*args):
    subprocess.run(['git', '-c', 'core.hooksPath=/dev/null', '-c', 'credential.helper=',
                    *args], env=env, check=True, stdout=subprocess.DEVNULL,
                   stderr=subprocess.DEVNULL)
git('merge-base', '--is-ancestor', spec['base'], 'HEAD')
git('checkout', '-B', spec['branch'], 'HEAD')
git('config', '--local', 'core.hooksPath', '/dev/null')
git('config', '--local', 'user.name', 'Home Lab')
git('config', '--local', 'user.email', 'home-lab@home-lab.invalid')
with open('/work/.git/homelab-output-schema.json', 'w') as stream:
    json.dump(spec['schema'], stream)
"""


def installation_token(repository):
    return GitHubApp().token(repository)


def prepare(record, folder, *, cancelled=lambda: False):
    document = record["request"]
    token = installation_token(document["repository"])
    default, refs = remote_refs(document["repository"], token)
    branch = document["branch"] or default
    if document["mode"] == "write" and branch in {"main", default}:
        raise Refused("push_refused")
    work_id = "run-" + record["id"].hex
    launcher.request({"op": "cli-create", "work_id": work_id})
    clone = {
        "op": "run",
        "profile": "task",
        "shape": "clone",
        "workspace": "cli",
        "work_id": work_id,
        "run_id": "clone-" + secrets.token_hex(12),
        "repository": document["repository"],
        "branch": branch if branch in refs else default,
        "env": {"GIT_CONFIG_VALUE_0": authorization(token)},
    }
    code, stopped = launcher.capture(
        clone, folder / "clone.out", folder / "clone.err", GIT_SECONDS, cancelled=cancelled
    )
    del clone, token
    if code != 0 or stopped != "exited":
        raise Refused("sandbox_error")
    checkout = {
        "op": "run",
        "profile": "task",
        "shape": "data",
        "workspace": "cli",
        "work_id": work_id,
        "run_id": "checkout-" + secrets.token_hex(12),
        "env": {},
        "arguments": [
            CHECKOUT,
            json.dumps({"base": document["base_commit"], "branch": branch, "schema": SCHEMA}),
        ],
    }
    with tempfile.TemporaryFile() as source:
        code, stopped = launcher.capture(
            checkout,
            folder / "checkout.out",
            folder / "checkout.err",
            GIT_SECONDS,
            stdin=source,
            cancelled=cancelled,
        )
    if code != 0 or stopped != "exited":
        raise Refused("sandbox_error")
    return work_id


def command(executor, prompt, session=None, *, stdin=False, ticket=False):
    if executor.harness == "codex":
        result = [
            "codex",
            "exec",
            *(["resume", session] if session else []),
            "-m",
            executor.model,
            "-c",
            f'model_reasoning_effort="{executor.effort}"',
        ]
        result += ["--skip-git-repo-check"] if ticket else ["--output-schema", SCHEMA_PATH]
        return [*result, "-o", "/run/factory/last.json", "--json", "--", "-" if stdin else prompt]
    result = ["claude", "-p", "--model", executor.model]
    if executor.effort:
        result += ["--effort", executor.effort]
    if session:
        result += ["--resume", session]
    return [
        *result,
        "--output-format",
        "json",
        *([] if ticket else ["--json-schema", json.dumps(SCHEMA)]),
        *([] if stdin else ["--", prompt]),
    ]


@dataclass
class Output:
    session: str | None = None
    final: dict | None = None
    limited: bool = False
    metrics: dict = field(
        default_factory=lambda: {"requests": 0, "fresh": 0, "cached": 0, "output": 0, "usd": None}
    )


LIMIT_MESSAGE = re.compile(
    r"usage[ -]limit|rate[ -]limit|quota exceeded|limit reached|you(?:'|’)ve hit your.*limit", re.I
)


def parse_output(harness, text):
    result = Output()
    for line in text.splitlines():
        try:
            event = json.loads(line)
            if not isinstance(event, dict):
                continue
            session = event.get("thread_id") if harness == "codex" else event.get("session_id")
            if isinstance(session, str) and re.fullmatch(r"[A-Za-z0-9._:-]{1,200}", session):
                result.session = session
            candidate = None
            if harness == "codex":
                item = event.get("item", {})
                if event.get("type") == "item.completed" and item.get("type") == "agent_message":
                    candidate = json.loads(item["text"])
                if event.get("type") == "turn.completed":
                    tokens = event.get("usage", {})
                    cached = tokens.get("cached_input_tokens", 0)
                    result.metrics.update(
                        requests=result.metrics["requests"] + 1,
                        fresh=max(0, tokens.get("input_tokens", 0) - cached),
                        cached=cached,
                        output=tokens.get("output_tokens", 0),
                    )
            elif event.get("type") == "result":
                candidate = event.get("structured_output")
                if candidate is None and isinstance(event.get("result"), str):
                    try:
                        candidate = json.loads(event["result"])
                    except ValueError:
                        pass
                tokens = event.get("usage", {})
                result.metrics.update(
                    requests=event.get("num_turns", 1),
                    fresh=tokens.get("input_tokens", 0)
                    + tokens.get("cache_creation_input_tokens", 0),
                    cached=tokens.get("cache_read_input_tokens", 0),
                    output=tokens.get("output_tokens", 0),
                    usd=event.get("total_cost_usd"),
                )
            if (
                isinstance(candidate, dict)
                and set(candidate) == {"kind", "body"}
                and candidate["kind"] in SCHEMA["properties"]["kind"]["enum"]
                and isinstance(candidate["body"], str)
            ):
                result.final = candidate
            if event.get("type") in {"error", "turn.failed"} or event.get("is_error") is True:
                if LIMIT_MESSAGE.search(json.dumps(event)):
                    result.limited = True
        except (ValueError, TypeError, KeyError, AttributeError):
            continue
    if result.final is not None:
        result.limited = False
    return result


def final_state(final):
    kind = final["kind"]
    return {
        "status": {
            "handover": "done",
            "question": "question",
            "escalation": "escalated",
            "waiting": "waiting",
        }[kind],
        "resumable": True,
        "status_reason": "client_step"
        if kind == "waiting"
        else "executor_gave_up"
        if kind == "escalation"
        else None,
        "final_message": final["body"],
        "resume_at": None,
    }


def waiting_state(reset):
    return {
        "status": "waiting",
        "status_reason": "usage_limit",
        "resumable": True,
        "resume_at": datetime.fromtimestamp(reset, UTC),
    }


def interrupted_state(interruptions):
    return {
        "status": "queued" if interruptions == 0 else "failed",
        "status_reason": "interrupted",
        "resumable": False,
    }


def deliver(record, folder, *, cancelled=lambda: False):
    document = record["request"]
    bundle = folder / "branch.bundle"
    code, stopped = launcher.capture(
        {
            "op": "run",
            "profile": "bundle",
            "run_id": "bundle-" + secrets.token_hex(12),
            "stream": True,
            "folders": {"work": record["work_id"], "run": record["work_id"]},
            "branch": document["branch"],
        },
        bundle,
        folder / "bundle.err",
        GIT_SECONDS,
        cancelled=cancelled,
    )
    if code != 0 or stopped != "exited":
        raise Refused("push_refused")
    with checked_bundle(bundle, document, folder) as (directory, tip):
        if cancelled():
            raise Refused("interrupted")
        return push_checked(directory, document, tip)


def execute_turn(record, prompt, folder, turn_id, *, cancelled=lambda: False):
    selected = Executor(**record["executor"])
    out, err = folder / "turn.out", folder / "turn.err"
    started = time.monotonic()
    request = {
        "op": "run",
        "profile": "cli-run",
        "run_id": turn_id,
        "folders": {"work": record["work_id"], "run": record["work_id"]},
        "tool": selected.harness,
        "exports": [],
        "stdin": True,
        "read_only": record["request"]["mode"] == "read",
        "command": command(selected, prompt, record.get("session_id"), stdin=True),
    }
    payload = {"goal": prompt, "assets": []}
    if not record.get("session_id"):
        payload.update(
            assets=record["request"].get("assets", []), work=record["request"].get("work")
        )
    with tempfile.TemporaryFile() as source:
        source.write(json.dumps(payload, ensure_ascii=False, separators=(",", ":")).encode())
        source.seek(0)
        code, stopped = launcher.capture(
            request, out, err, TURN_SECONDS, stdin=source, cancelled=cancelled
        )
    output = parse_output(selected.harness, out.read_text(errors="replace"))
    output.metrics["seconds"] = time.monotonic() - started
    session = output.session or record.get("session_id")

    def failed(reason):
        # Keep measured usage even when the turn or its subsequent delivery fails.
        return (
            {"status": "failed", "status_reason": reason, "resumable": False},
            session,
            [],
            output.metrics,
        )

    if output.limited or (
        output.final is None and code != 0 and LIMIT_MESSAGE.search(err.read_text(errors="replace"))
    ):
        if not session:
            return failed("provider_lost")
        return waiting_state(time.time() + 3600), session, [], output.metrics
    if stopped != "exited" or code != 0 or output.final is None:
        return failed("sandbox_error" if stopped != "exited" else "executor_gave_up")
    if not session:
        return failed("provider_lost")
    if cancelled():
        raise Refused("interrupted")
    try:
        commits = (
            deliver(record, folder, cancelled=cancelled)
            if record["request"]["mode"] == "write"
            else []
        )
    except Exception as error:
        return failed(str(error) if isinstance(error, Refused) else "sandbox_error")
    return final_state(output.final), session, commits, output.metrics


def codex_usage(folder, *, cancelled=lambda: False):
    out, err = folder / "codex-usage.out", folder / "codex-usage.err"
    code, stopped = launcher.capture(
        {"op": "run", "profile": "usage", "run_id": "usage-" + secrets.token_hex(12)},
        out,
        err,
        60,
        limit=1024 * 1024,
        cancelled=cancelled,
    )
    return (
        usage.codex_reading(out.read_text(), time.time())
        if code == 0 and stopped == "exited"
        else None
    )


def claude_probe(folder, *, cancelled=lambda: False):
    work_id = "run-" + uuid4().hex
    launcher.request({"op": "cli-create", "work_id": work_id})
    try:
        out, err = folder / "claude-usage.out", folder / "claude-usage.err"
        launcher.capture(
            {
                "op": "run",
                "profile": "cli-run",
                "run_id": "probe-" + secrets.token_hex(12),
                "folders": {"work": work_id, "run": work_id},
                "tool": "claude",
                "exports": [],
                "command": [
                    "claude",
                    "-p",
                    "--model",
                    "haiku",
                    "--output-format",
                    "stream-json",
                    "--verbose",
                    "Reply with: ok",
                ],
            },
            out,
            err,
            60,
            cancelled=cancelled,
        )
        return usage.claude_reading(out.read_text(errors="replace"), time.time())
    finally:
        launcher.request({"op": "cli-remove", "work_id": work_id})
