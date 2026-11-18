"""Marked work follows its normal pipeline; grading uses only the sealed check shape."""

# ruff: noqa: F811

import json
import subprocess
import sys
from contextlib import nullcontext
from pathlib import Path

import pytest

from homelab.runs import checks, delivery, launcher, runner, slice_loop, slices
from homelab.tasks import claim, finish
from homelab.tasks import slices as whole_store
from tests.test_runs import CLIENT, HEADERS, OTHER, api, body, post  # noqa: F401
from tests.test_runs_delivery import bundle, git, github  # noqa: F401
from tests.test_runs_tickets import reading, ticket_runner  # noqa: F401
from tests.test_task_store import connection  # noqa: F401

ENTRY = {"harness": "codex", "model": "explicit-eval-model", "effort": "high"}


def marked(role="ticket", **changes):
    return body(
        **{
            "role": role,
            "mode": "read" if role == "review" else "write",
            "branch": "eval/attempt",
            "eval": {"set": "T", "case": "one", "executor": ENTRY},
            **changes,
        }
    )


def grade(**changes):
    return body(
        **{
            "role": "check",
            "branch": None,
            "eval": {"set": "T", "case": "one"},
            "work": {
                "goal": "Grade",
                "check_command": "uv run pytest",
                "overlay": {"commit": "c" * 40, "paths": ["tests/hidden.py"]},
            },
            **changes,
        }
    )


@pytest.mark.parametrize("role", ["slice", "cut", "review"])
@pytest.mark.parametrize("limited", [False, True])
def test_eval_whole_run_uses_only_named_executor(api, monkeypatch, tmp_path, role, limited):
    monkeypatch.setenv("HOMELAB_RUN_ROLES", "slice,cut,review")
    monkeypatch.setattr(slices, "codex_usage", lambda *a, **kw: reading(99 if limited else 0))
    monkeypatch.setattr(slices, "claude_probe", lambda *a, **kw: pytest.fail("eval fallback"))
    monkeypatch.setattr(launcher, "request", lambda request: None)
    monkeypatch.setattr(slices, "prepare", lambda *a, **kw: "run-work")
    seen = []

    def turn(saved, prompt, folder, turn_id, **kw):
        seen.append(saved["executor"])
        assert saved["request"]["role"] == role
        return slices.final_state({"kind": "handover", "body": "Done"}), "session", [], {}

    monkeypatch.setattr(slices, "execute_turn", turn)
    run_id = post(marked(role)).json()["run_id"]
    assert api.execute("SELECT queue_name FROM procrastinate_jobs").fetchone() == ("runs",)
    assert claim.claim(api, legacy_roles=False) is None
    service = slice_loop.Service(lambda: nullcontext(api), tmp_path, lambda: None)
    try:
        # An eval must not consult even a usable table entry.
        service.table = {}
        service.run(whole_store.claim(api))
    finally:
        service.close()
    result = CLIENT.get(f"/v1/runs/{run_id}", headers=HEADERS).json()
    assert result["executor"] == ENTRY
    if limited:
        assert result["status"] == "waiting" and result["status_reason"] == "usage_limit"
        assert seen == [] and result["resume_at"] is not None
    else:
        assert seen == [ENTRY] and result["status"] == "done"
        assert result["resumable"] and result["final_message"] == "Done"
        assert result["attempts"][0]["status"] == "done"


@pytest.mark.parametrize("limited", [False, True])
def test_eval_ticket_never_uses_table_or_fallback(api, ticket_runner, monkeypatch, limited):
    monkeypatch.setattr(slices, "codex_usage", lambda *a, **kw: reading(99 if limited else 0))
    monkeypatch.setattr(slices, "claude_probe", lambda *a: pytest.fail("eval fallback"))
    monkeypatch.setattr(runner, "load_executors", lambda: pytest.fail("eval table lookup"))
    run_id = post(marked()).json()["run_id"]
    runner.run_task(runner.store("claim"))
    result = CLIENT.get(f"/v1/runs/{run_id}", headers=HEADERS).json()
    assert result["executor"] == ENTRY
    assert result["status"] == ("waiting" if limited else "done")
    assert [entry.selection() for entry, _ in ticket_runner] == ([] if limited else [ENTRY])
    assert result["commits"] == ([] if limited else ["b" * 40])


def test_eval_ticket_live_limit_waits_for_same_entry(api, ticket_runner, monkeypatch):
    monkeypatch.setattr(slices, "codex_usage", lambda *a: reading(0))
    monkeypatch.setattr(slices, "claude_probe", lambda *a: pytest.fail("eval fallback"))
    monkeypatch.setattr(runner, "run_ticket", lambda *a, **kw: {"ended": "usage_limit"})
    run_id = post(marked()).json()["run_id"]
    runner.run_task(runner.store("claim"))
    result = CLIENT.get(f"/v1/runs/{run_id}", headers=HEADERS).json()
    assert result["status_reason"] == "usage_limit" and result["executor"] == ENTRY
    assert len(result["attempts"]) == 1


@pytest.mark.parametrize("role", ["slice", "cut", "review"])
def test_pi_is_refused_on_non_ticket_role(api, monkeypatch, role):
    monkeypatch.setenv("HOMELAB_RUN_ROLES", "slice,cut,review")
    response = post(
        marked(role, eval={"set": "T", "case": "one", "executor": {"configuration": "pi/base"}})
    )
    assert response.status_code == 409 and response.json()["error"] == "role_not_available"
    for table in ("homelab_runs", "homelab_tasks", "procrastinate_jobs"):
        assert api.execute(f"SELECT count(*) FROM {table}").fetchone() == (0,)


@pytest.mark.parametrize(
    "entry",
    [
        {},
        {"harness": "codex", "model": "x"},
        {"harness": "claude"},
        {"harness": "claude", "model": "x", "extra": "secret"},
        {"configuration": "pi/base", "harness": "claude", "model": "x"},
        {"configuration": "pi/does-not-exist"},
        None,
    ],
)
def test_eval_entry_validation_matches_catalogue(api, entry):
    response = post(marked(eval={"set": "T", "case": "one", "executor": entry}))
    assert response.status_code == 422 and response.json() == {"error": "invalid_request"}


@pytest.mark.parametrize(
    "changes",
    [
        {"eval": None},
        {"eval": {"set": "T", "case": "one", "executor": ENTRY}},
        {"eval": {"set": "T", "case": "one", "executor": None}},
        {"work": {"goal": "Grade", "overlay": {"commit": "c" * 40, "paths": ["test.py"]}}},
        {"work": {"goal": "Grade", "check_command": "true"}},
    ],
)
def test_check_requires_mark_command_overlay_without_executor(api, changes):
    assert post(grade(**changes)).status_code == 422
    assert api.execute("SELECT count(*) FROM homelab_runs").fetchone() == (0,)


@pytest.mark.parametrize("role", ["ticket", "slice", "cut", "review"])
@pytest.mark.parametrize("present", [True, False])
def test_overlay_is_refused_on_other_roles(api, monkeypatch, role, present):
    monkeypatch.setenv("HOMELAB_RUN_ROLES", "slice,cut,review")
    work = grade()["work"]
    if not present:
        work["overlay"] = None
    assert post(marked(role, work=work)).status_code == 422
    assert api.execute("SELECT count(*) FROM homelab_runs").fetchone() == (0,)


def test_removed_eval_routes_are_404():
    assert CLIENT.post("/v1/tasks/eval", json={}).status_code == 404
    assert CLIENT.get("/v1/tasks/eval/123").status_code == 404


@pytest.mark.parametrize(
    "marked_run,branch,allowed",
    [
        (True, "eval/x", True),
        (True, "slice/x", False),
        (False, "eval/x", False),
        (False, "slice/x", True),
    ],
)
def test_whole_delivery_enforces_both_branch_directions(
    bundle, tmp_path, monkeypatch, marked_run, branch, allowed
):
    copy, path, document, tip = bundle
    git(copy, "branch", "-m", branch)
    git(copy, "bundle", "create", str(path), "HEAD", "refs/heads/" + branch)
    document.update(branch=branch, eval={"set": "T", "case": "one"} if marked_run else None)
    github(monkeypatch, private=False)
    scans, pushes = [], []
    monkeypatch.setattr(delivery, "private_content_check", lambda *a, **kw: scans.append(kw))
    original = delivery.git

    def push(directory, *args, **kwargs):
        if args[0] == "push":
            pushes.append(args)
            return type("Result", (), {"returncode": 0})()
        return original(directory, *args, **kwargs)

    monkeypatch.setattr(delivery, "git", push)
    with delivery.checked_bundle(path, document, tmp_path) as (directory, checked_tip):
        if allowed:
            assert delivery.push_checked(directory, document, checked_tip) == [tip]
            assert scans == [{"private": False}] and len(pushes) == 1
        else:
            with pytest.raises(delivery.Refused, match="push_refused"):
                delivery.push_checked(directory, document, checked_tip)
            assert scans == pushes == []


@pytest.mark.parametrize("marked_run,branch", [(True, "work/x"), (False, "eval/x")])
def test_ticket_push_refuses_wrong_branch_before_any_git(monkeypatch, marked_run, branch):
    monkeypatch.setattr(runner, "refuse_branch", lambda *a: pytest.fail("must refuse before Git"))
    task = {"branch": branch, "eval_set": "T" if marked_run else None}
    assert runner.push(task, Path("/unused"), "a" * 40) == ("push_refused", [])


def local_capture(copy, requests, hidden_commit):
    def capture(request, out, err, seconds, *, stdin=None, **kwargs):
        requests.append(request)
        assert request["profile"] == "task"
        shape = request["shape"]
        if shape == "prepare":
            # Do not install packages in the test: verify the exact ticket preparation.
            from homelab.runs.task_run import PREPARE

            assert request["arguments"][0] == PREPARE
            assert not (copy / "tests/hidden.py").exists()
            assert (
                subprocess.run(
                    ["git", "cat-file", "-e", hidden_commit], cwd=copy, capture_output=True
                ).returncode
                != 0
            )
            out.write_bytes(b"")
            err.write_bytes(b"")
            return 0, "exited"
        assert shape in {"data", "check"}  # No agent, commit or push shape.
        command = [sys.executable, "-I", "-c"] if shape == "data" else ["sh", "-c"]
        with out.open("wb") as output, err.open("wb") as error:
            result = subprocess.run(
                [*command, *request["arguments"]],
                cwd=copy,
                stdin=stdin,
                stdout=output,
                stderr=error,
            )
        return result.returncode, "exited"

    return capture


@pytest.mark.parametrize("exit_code", [0, 7])
def test_check_copies_overlay_runs_sealed_and_records_result(
    api, bundle, tmp_path, monkeypatch, exit_code
):
    copy, _, document, base = bundle
    # Overlay is on a different branch, with an unrelated edit that must not be copied.
    git(copy, "checkout", "-qb", "hidden", document["base_commit"])
    (copy / "tests").mkdir()
    (copy / "tests/hidden.py").write_text("print('sealed overlay')\n")
    (copy / "src/app").write_text("not the attempt")
    git(copy, "add", ".")
    git(copy, "commit", "-qm", "Hidden tests")
    overlay = git(copy, "rev-parse", "HEAD")
    # The grading base differs from the copy's initial tip.
    original_head = base
    calls = []
    grading = tmp_path / "grading"
    grading.mkdir()
    monkeypatch.setattr(runner, "RUNS", tmp_path / "runs")
    monkeypatch.setattr(runner, "connection", api)
    monkeypatch.setattr(runner, "installation_token", lambda _: "FIXTURE")
    monkeypatch.setattr(delivery, "remote_refs", lambda *a: ("main", {"main": overlay}))
    monkeypatch.setattr(launcher, "request", lambda request: None)
    monkeypatch.setattr(runner, "remove_copy", lambda _: None)
    monkeypatch.setattr(runner, "clone_url", lambda _: str(copy))
    monkeypatch.setattr(runner, "notify", lambda *a: pytest.fail("check should complete"))
    monkeypatch.setattr(runner, "work", lambda *a: pytest.fail("no executor"))
    monkeypatch.setattr(runner, "push", lambda *a: pytest.fail("no push"))
    monkeypatch.setattr(launcher, "capture", local_capture(grading, calls, overlay))
    original_git = delivery.git

    def fetch(directory, *args, **kwargs):
        if args[0] == "fetch":
            return original_git(directory, "-c", "protocol.file.allow=always", *args, **kwargs)
        return original_git(directory, *args, **kwargs)

    monkeypatch.setattr(delivery, "git", fetch)
    monkeypatch.setattr(
        runner, "record", lambda e: finish.record(api, finish.parse(json.dumps(e).encode()))
    )
    command = f"python3 tests/hidden.py; printf '%020000d' 0; echo grading-tail; exit {exit_code}"
    request = grade(
        base_commit=base,
        work={
            "goal": "Grade",
            "check_command": command,
            "overlay": {"commit": overlay, "paths": ["tests"]},
        },
    )
    assert post(request, OTHER).status_code == 403
    run_id = post(request).json()["run_id"]
    task = claim.claim(api, legacy_roles=False)
    runner.run_task(task)
    result = CLIENT.get(f"/v1/runs/{run_id}", headers=HEADERS).json()
    assert result["status"] == "done", result
    assert result["check"]["exit_code"] == exit_code
    assert result["check"]["tail"].endswith("grading-tail\n")
    assert len(result["check"]["tail"].encode()) == runner.CHECK_OUTPUT_BYTES
    assert result["commits"] == [] and result["executor"] is None
    assert result["configuration_hash"] is None and not result["resumable"]
    assert git(grading, "rev-parse", "HEAD") == original_head
    assert git(grading, "rev-list", "--count", "HEAD") == git(
        copy, "rev-list", "--count", original_head
    )
    assert (grading / "src/app").read_text() == "changed"
    assert (grading / "tests/hidden.py").read_text() == "print('sealed overlay')\n"
    assert [call["shape"] for call in calls] == ["data", "prepare", "data", "check"]
    check = calls[-1]
    assert check["env"] == {"UV_NO_SYNC": "1", "PYTHONPYCACHEPREFIX": "/tmp/pycache"}
    # Repeated completion cannot replace the saved result, and the original request deduplicates.
    assert post(request).status_code == 200
    finish.record(
        api,
        finish.EndTask(
            event="end_task",
            task_id=task["id"],
            job_id=task["job_id"],
            status="done",
            check={"exit_code": 9, "tail": "changed"},
        ),
    )
    assert CLIENT.get(f"/v1/runs/{run_id}", headers=HEADERS).json()["check"] == result["check"]


@pytest.mark.parametrize("denied", [False, True])
def test_eval_ticket_public_push_scans_content_and_records_commits(
    api, ticket_runner, bundle, tmp_path, monkeypatch, denied
):
    copy, _, document, tip = bundle
    git(copy, "branch", "-m", "eval/x")
    git(copy, "update-ref", "refs/remotes/origin/eval/x", document["base_commit"])
    monkeypatch.setattr(runner, "push", delivery_ticket_push)
    monkeypatch.setattr(runner, "fresh_copy", lambda _: copy)
    monkeypatch.setattr(runner, "installation_token", lambda _: "FIXTURE")
    monkeypatch.setattr(runner, "repository_is_private", lambda _: False)
    monkeypatch.setattr(slices, "codex_usage", lambda _: reading(0))
    monkeypatch.setattr(
        runner, "run_ticket", lambda *a, **kw: {"ended": "committed", "commit": tip}
    )
    deny_list = tmp_path / "deny-list"
    deny_list.write_text("changed" if denied else "unrelated-private-marker")
    monkeypatch.setattr(runner, "DENY_LIST", deny_list)
    pushes = []
    original = subprocess.run

    def push(command, **kwargs):
        if "push" in command:
            pushes.append(command)
            return subprocess.CompletedProcess(command, 0, "")
        return original(command, **kwargs)

    monkeypatch.setattr(subprocess, "run", push)
    run_id = post(marked(branch="eval/x")).json()["run_id"]
    runner.run_task(runner.store("claim"))
    result = CLIENT.get(f"/v1/runs/{run_id}", headers=HEADERS).json()
    assert result["commits"] == ([] if denied else [tip])
    assert result["status"] == ("escalated" if denied else "done")
    assert len(pushes) == (0 if denied else 1)
    if denied:
        assert result["status_reason"] == "push_refused"


# Save the real push before the shared ticket fixture substitutes it.
delivery_ticket_push = runner.push


@pytest.mark.parametrize("kind", ["source_link", "destination_link", "missing", "submodule"])
def test_overlay_refuses_links_submodules_and_missing_paths(bundle, tmp_path, kind):
    copy, _, _, _ = bundle
    path = "tests/hidden.py"
    (copy / "tests").mkdir()
    (copy / path).write_text("hidden")
    if kind == "source_link":
        (copy / path).unlink()
        (copy / path).symlink_to("../src/app")
    git(copy, "add", ".")
    if kind == "submodule":
        git(
            copy,
            "update-index",
            "--add",
            "--cacheinfo",
            "160000",
            git(copy, "rev-parse", "HEAD"),
            path,
        )
    git(copy, "commit", "-qm", "Overlay")
    commit = git(copy, "rev-parse", "HEAD")
    if kind == "destination_link":
        (copy / path).unlink()
        (copy / "tests").rmdir()
        outside = tmp_path / "outside"
        outside.mkdir()
        (outside / "hidden.py").write_text("untouched")
        (copy / "tests").symlink_to(outside)
    request = {"commit": commit, "paths": ["missing" if kind == "missing" else path]}
    git(copy, "update-ref", "refs/heads/overlay", commit)
    bundle_file = tmp_path / "overlay.bundle"
    git(copy, "bundle", "create", str(bundle_file), "refs/heads/overlay")
    with bundle_file.open("rb") as source:
        result = subprocess.run(
            [sys.executable, "-I", "-c", checks.OVERLAY, json.dumps(request)],
            cwd=copy,
            stdin=source,
            capture_output=True,
        )
    assert result.returncode != 0
    if kind == "destination_link":
        assert (outside / "hidden.py").read_text() == "untouched"


def test_check_shape_uses_the_existing_launcher_seal():
    from tests.test_launcher import launcher as installed

    shape = installed.TASK_SHAPES["check"]
    assert shape["network"] == "none"
    assert shape["mounts"] == {"work": "/work:rw", "git": "/work/.git:ro", "venv": "/work/.venv:ro"}
    assert shape["environment"] == ["UV_NO_SYNC", "PYTHONPYCACHEPREFIX"]


@pytest.mark.parametrize(
    "kind,status,reason",
    [
        ("question", "question", None),
        ("escalation", "escalated", "executor_gave_up"),
        ("waiting", "waiting", "client_step"),
        ("usage_limit", "waiting", "usage_limit"),
    ],
)
def test_marked_whole_run_keeps_conversation_state(
    api, monkeypatch, tmp_path, kind, status, reason
):
    monkeypatch.setenv("HOMELAB_RUN_ROLES", "slice,cut,review")
    monkeypatch.setattr(slices, "codex_usage", lambda *a, **kw: reading(0))
    monkeypatch.setattr(launcher, "request", lambda *a: None)
    monkeypatch.setattr(slices, "prepare", lambda *a, **kw: "run-work")
    result = (
        slices.waiting_state(1_800_000_500)
        if kind == "usage_limit"
        else slices.final_state({"kind": kind, "body": "Answer"})
    )
    monkeypatch.setattr(slices, "execute_turn", lambda *a, **kw: (result, "session", [], {}))
    run_id = post(marked("slice")).json()["run_id"]
    service = slice_loop.Service(lambda: nullcontext(api), tmp_path, lambda: None)
    try:
        service.run(whole_store.claim(api))
    finally:
        service.close()
    saved = CLIENT.get(f"/v1/runs/{run_id}", headers=HEADERS).json()
    assert (saved["status"], saved["status_reason"]) == (status, reason)
    assert saved["resumable"] and saved["executor"] == ENTRY
    assert saved["started_at"] is not None
    reply = CLIENT.post(
        f"/v1/runs/{run_id}/messages",
        headers=HEADERS,
        json={"contract": 1, "message_id": "answer", "text": "Continue"},
    )
    assert reply.status_code == 200
    assert reply.json()["status"] == ("waiting" if kind == "usage_limit" else "queued")
