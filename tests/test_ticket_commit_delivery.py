"""Successful ticket pushes carry their pre-push range through to the final finish event."""

import json
import logging
import subprocess
from pathlib import Path
from types import SimpleNamespace

import pytest

from homelab.runs import runner
from homelab.tasks import finish
from tests.test_task_runner import CODEX
from tests.test_task_runner import task as ticket_task
from tests.test_ticket_commit_range import git, history  # noqa: F401


@pytest.fixture
def delivery(history, tmp_path, monkeypatch):  # noqa: F811
    task, copy, base, first, second = history
    state = {
        "task": task,
        "copy": copy,
        "base": base,
        "first": first,
        "second": second,
        "results": ["pushed"],
        "ranges": [],
        "pushes": [],
        "tokens": [],
        "private": False,
    }
    deny = tmp_path / "deny"
    deny.write_text("private-marker-391d\n")
    monkeypatch.setattr(runner, "DENY_LIST", deny)
    monkeypatch.setattr(runner, "default_branch", lambda _: "main")
    monkeypatch.setattr(runner, "repository_is_private", lambda _: state["private"])

    def token(repository):
        state["tokens"].append(repository)
        return "FAKE-TICKET-TOKEN"

    monkeypatch.setattr(runner, "installation_token", token)
    real_run = subprocess.run

    def offline_git(command, **kwargs):
        if "push" not in command:
            result = real_run(command, **kwargs)
            if "rev-list" in command and any(".." in arg for arg in command):
                state["ranges"].append(result.stdout.decode("ascii").splitlines())
            return result
        state["pushes"].append(command)
        assert "FAKE-TICKET-TOKEN" not in repr(command)
        assert not any(arg.startswith("--force") or arg.startswith("+") for arg in command)
        result = state["results"].pop(0)
        if result == "timeout":
            raise subprocess.TimeoutExpired(command, 1)
        if result == "pushed":
            # Git may update tracking refs after delivery. The saved range must survive that.
            git(copy, "update-ref", "refs/remotes/origin/work/ticket", second)
            return SimpleNamespace(returncode=0, stdout="")
        if result == "rejected":
            git(copy, "update-ref", "refs/remotes/origin/work/ticket", first)
            return SimpleNamespace(returncode=1, stdout="!\tHEAD\t[rejected] (fetch first)\n")
        return SimpleNamespace(returncode=1, stdout="remote failure")

    monkeypatch.setattr(subprocess, "run", offline_git)
    return state


@pytest.mark.parametrize("private", [False, True])
def test_delivery_push_collects_the_pre_push_range_once_for_any_visibility(delivery, private):
    delivery["private"] = private
    collected = ["e" * 40]
    assert (
        runner.push(delivery["task"], delivery["copy"], delivery["second"], commits=collected)
        == "pushed"
    )
    assert collected == ["e" * 40, delivery["first"], delivery["second"]]
    assert delivery["ranges"] == [[delivery["first"], delivery["second"]]]
    assert len(delivery["pushes"]) == len(delivery["tokens"]) == 1


@pytest.mark.parametrize(
    "result, expected",
    [("failed", "push_failed"), ("rejected", "push_rejected"), ("timeout", "push_failed")],
)
def test_delivery_unsuccessful_push_does_not_mutate_collector(delivery, result, expected):
    delivery["results"] = [result]
    collected = ["e" * 40]
    assert (
        runner.push(delivery["task"], delivery["copy"], delivery["second"], commits=collected)
        == expected
    )
    assert collected == ["e" * 40]


@pytest.mark.parametrize("private", [False, True])
def test_delivery_unknown_range_fails_before_token_or_push(delivery, private):
    delivery["private"] = private
    git(delivery["copy"], "update-ref", "-d", "refs/remotes/origin/work/ticket")
    collected = []
    assert (
        runner.push(delivery["task"], delivery["copy"], delivery["second"], commits=collected)
        == "push_content_check_failed"
    )
    assert collected == [] and delivery["tokens"] == [] and delivery["pushes"] == []


def test_delivery_private_content_refusal_does_not_record_commits_or_mint_token(delivery):
    commit = git(delivery["copy"], "rev-parse", "HEAD")
    git(delivery["copy"], "commit", "--allow-empty", "-qm", "private-marker-391d")
    denied = git(delivery["copy"], "rev-parse", "HEAD")
    assert denied != commit
    collected = []
    assert (
        runner.push(delivery["task"], delivery["copy"], denied, commits=collected)
        == "push_private_content: commit message"
    )
    assert collected == [] and delivery["tokens"] == [] and delivery["pushes"] == []


@pytest.mark.parametrize("refusal", ["default_branch", "bad_commit"])
def test_delivery_early_refusals_leave_collector_untouched(delivery, refusal):
    task = delivery["task"].copy()
    commit = delivery["second"]
    if refusal == "default_branch":
        task["branch"] = "main"
        expected = "push_default_branch_refused"
    else:
        commit = "not-a-sha"
        expected = "push_bad_commit"
    collected = ["e" * 40]
    assert runner.push(task, delivery["copy"], commit, commits=collected) == expected
    assert collected == ["e" * 40]
    assert delivery["ranges"] == delivery["tokens"] == delivery["pushes"] == []


@pytest.fixture
def ticket_runner(delivery, monkeypatch):
    events = []
    outcomes = [{"ended": "committed", "commit": delivery["second"]}]
    monkeypatch.setattr(runner, "fresh_copy", lambda _: delivery["copy"])
    monkeypatch.setattr(runner, "run_ticket", lambda *args, **kwargs: outcomes.pop(0).copy())
    monkeypatch.setattr(runner, "check_output_of", lambda _: "Check feedback")
    monkeypatch.setattr(runner, "remove_copy", lambda _: events.append("removed"))
    monkeypatch.setattr(runner, "record", lambda event: events.append(event.copy()))
    monkeypatch.setattr(runner, "notify", lambda *args: None)
    delivery.update(events=events, outcomes=outcomes)
    return delivery


def ending(state):
    events = [event for event in state["events"] if isinstance(event, dict)]
    endings = [event for event in events if event["event"] == "end_task"]
    assert len(endings) == 1
    assert state["events"][-2:] == ["removed", endings[0]]
    assert all("commits" not in event for event in events if event["event"] != "end_task")
    # The real protocol, rather than a permissive fake record function, must accept every event.
    for event in events:
        finish.parse(json.dumps(event).encode())
    return endings[0]


@pytest.mark.parametrize("cli", [False, True])
def test_delivery_ticket_finish_contains_every_pushed_sha(ticket_runner, caplog, cli):
    caplog.set_level(logging.INFO, logger="homelab-runs")
    state = ticket_runner
    task = ticket_task(**state["task"])
    if cli:
        task["routes"] = [CODEX]
    runner.run_task(task)
    final = ending(state)
    assert (final["status"], final["reason"]) == ("done", None)
    assert final["commits"] == [state["first"], state["second"]]
    assert task["goal"] not in caplog.text and "FAKE-TICKET-TOKEN" not in caplog.text
    assert "commits" not in task  # Delivery state belongs to this invocation, not the claim record.


def test_delivery_rejected_attempt_range_is_discarded_after_branch_moves(ticket_runner):
    state = ticket_runner
    state["results"] = ["rejected", "pushed"]
    state["outcomes"] *= 2
    runner.run_task(ticket_task(**state["task"]))
    final = ending(state)
    assert final["status"] == "done" and final["commits"] == [state["second"]]
    assert state["ranges"] == [[state["first"], state["second"]], [state["second"]]]


def test_delivery_check_retry_records_only_the_eventual_successful_range(ticket_runner):
    state = ticket_runner
    state["outcomes"].insert(0, {"ended": "check_failed"})
    runner.run_task(ticket_task(**state["task"]))
    assert ending(state)["commits"] == [state["first"], state["second"]]
    assert len(state["pushes"]) == 1


@pytest.mark.parametrize("outcome", ["check_failed", "clone_failed", "push_failed", "eval_kept"])
def test_delivery_nonpushed_ticket_endings_keep_commits_empty(ticket_runner, monkeypatch, outcome):
    state = ticket_runner
    task = ticket_task(**state["task"])
    if outcome == "clone_failed":
        monkeypatch.setattr(runner, "fresh_copy", lambda _: None)
    elif outcome == "check_failed":
        state["outcomes"][:] = [{"ended": "check_failed"}] * 2
    elif outcome == "push_failed":
        state["results"] = ["failed"]
    else:
        task.update(eval_set="T", eval_case="one", eval_configuration="base")
    runner.run_task(task)
    final = ending(state)
    assert final.get("commits", []) == []
    assert final["reason"] == outcome
    if outcome == "eval_kept":
        assert final["status"] == "done" and state["pushes"] == []


def test_delivery_collector_does_not_leak_between_invocations(ticket_runner, monkeypatch):
    state = ticket_runner
    task = ticket_task(**state["task"])
    runner.run_task(task)
    assert ending(state)["commits"] == [state["first"], state["second"]]
    state["events"].clear()
    monkeypatch.setattr(runner, "fresh_copy", lambda _: None)
    runner.run_task(task)
    assert ending(state).get("commits", []) == []


def test_delivery_documentation_covers_both_run_roles_and_preserves_final_message_contract():
    guide = Path(__file__).resolve().parents[1] / "docs/run-api.md"
    text = " ".join(guide.read_text().split())
    assert "For `slice` and `ticket` runs, `commits` lists the pushed SHAs." in text
    assert "These fields remain `commits:[]`" not in text
    assert "`final_message:null`" in text
