"""The host service: order of attempts, what it asks of the store, and the push, on real git."""

import importlib.machinery
import importlib.util
import json
import os
import subprocess
from configparser import ConfigParser
from pathlib import Path

import pytest
import yaml

ROOT = Path(__file__).resolve().parents[1]
loader = importlib.machinery.SourceFileLoader(
    "homelab_task_runner", str(ROOT / "node/usr/local/sbin/homelab-task-runner")
)
spec = importlib.util.spec_from_loader("homelab_task_runner", loader)
runner = importlib.util.module_from_spec(spec)
loader.exec_module(runner)

CHEAP = {"kind": "litellm", "purpose": "task-worker"}
REVIEWER = {"kind": "litellm", "purpose": "task-reviewer"}
BETTER = {"kind": "litellm", "purpose": "task-better"}
CLI = {"kind": "cli", "tool": "tool", "model": "model"}
CODEX = {"kind": "cli", "tool": "codex", "model": "example-model", "effort": "high"}
ENV = {
    "GIT_AUTHOR_NAME": "Fixture",
    "GIT_AUTHOR_EMAIL": "fixture@example.invalid",
    "GIT_COMMITTER_NAME": "Fixture",
    "GIT_COMMITTER_EMAIL": "fixture@example.invalid",
    "GIT_CONFIG_GLOBAL": os.devnull,
    "GIT_CONFIG_NOSYSTEM": "1",
}


def task(**fields):
    return {
        "id": 7,
        "job_id": 70,
        "slice": "sample",
        "repository": "example/repo",
        "branch": "slice-branch",
        "title": "Title",
        "goal": "The goal",
        "allowed_files": ["src/app.py"],
        "check_command": "pytest -q",
        "pinned_paths": ["tests"],
        "routes": [CHEAP],
        "review_routes": [REVIEWER],
        "next_attempt": 1,
        **fields,
    }


class Script:
    """Scripted runs; records the store's events, the routes used and the check output given."""

    def __init__(self, monkeypatch, tmp_path, *outcomes, pushes=()):
        self.outcomes = list(outcomes)
        self.pushes = list(pushes)
        self.events = []
        self.runs = []
        self.review_purposes = []
        self.tmp_path = tmp_path
        monkeypatch.setattr(runner, "record", self.events.append)
        monkeypatch.setattr(runner, "fresh_copy", lambda item: tmp_path)
        monkeypatch.setattr(runner, "remove_copy", lambda item: self.events.append("removed"))
        monkeypatch.setattr(runner, "refuse_branch", lambda item: None)
        monkeypatch.setattr(runner, "run_ticket", self.run_ticket)
        monkeypatch.setattr(runner, "push", self.push)

    def run_ticket(self, item, copy, purpose, review_purpose, feedback, **kwargs):
        self.runs.append((kwargs.get("cli") or purpose, feedback))
        self.review_purposes.append(review_purpose)
        ended = self.outcomes.pop(0)
        extra = {}
        if isinstance(ended, tuple):
            ended, extra = ended
        folder = self.tmp_path / f"run{len(self.runs)}"
        folder.mkdir()
        (folder / "check.log").write_text(f"output of {len(self.runs)}")
        (folder / "check-2.log").write_text(f"second output of {len(self.runs)}")
        return {
            **extra,
            "ended": ended,
            "run_folder": str(folder),
            "run_id": f"run{len(self.runs)}",
            "check_exit_code": 1 if ended == "check_failed" else 0,
            "steps": 3,
            "tokens": {"fresh": 10, "output": 2, "cached": 5, "cache_write": 0},
            "commit": "a" * 40 if ended == "committed" else None,
        }

    def push(self, item, copy, commit):
        return self.pushes.pop(0)

    def named(self, event):
        return [item for item in self.events if isinstance(item, dict) and item["event"] == event]

    def ending(self):
        (event,) = self.named("end_task")
        return event["status"], event["reason"]


def test_a_commit_that_pushes_ends_done_in_one_attempt(monkeypatch, tmp_path):
    script = Script(monkeypatch, tmp_path, "committed", pushes=["pushed"])
    runner.run_task(task())
    assert script.ending() == ("done", None)
    assert script.runs == [("task-worker", None)]
    (attempt,) = script.named("end_attempt")
    assert attempt["status"] == "done" and attempt["reason"] is None
    assert attempt["number"] == 1 and attempt["route"] == CHEAP
    assert attempt["steps"] == 3 and attempt["run_id"] == "run1"
    assert (attempt["fresh_tokens"], attempt["cached_tokens"]) == (10, 5)
    assert attempt["output_tokens"] == 2
    assert attempt["seconds"] >= 0
    assert "cost_usd" not in attempt
    assert script.events[-2:] == ["removed", script.named("end_task")[0]]


def test_a_failed_check_retries_once_on_its_route_with_its_output(monkeypatch, tmp_path):
    script = Script(monkeypatch, tmp_path, "check_failed", "committed", pushes=["pushed"])
    runner.run_task(task(routes=[CHEAP, BETTER]))
    assert script.runs == [("task-worker", None), ("task-worker", ("check", "output of 1"))]
    assert script.ending() == ("done", None)


def test_a_second_failed_check_moves_to_the_next_route_without_the_output(monkeypatch, tmp_path):
    script = Script(
        monkeypatch, tmp_path, "check_failed", "check_failed", "committed", pushes=["pushed"]
    )
    runner.run_task(task(routes=[CHEAP, BETTER]))
    assert script.runs == [
        ("task-worker", None),
        ("task-worker", ("check", "output of 1")),
        ("task-better", None),
    ]
    assert [event["number"] for event in script.named("end_attempt")] == [1, 2, 3]
    assert [event["route"] for event in script.named("begin_attempt")] == [CHEAP, CHEAP, BETTER]


def test_every_route_failing_escalates_with_the_last_reason(monkeypatch, tmp_path):
    script = Script(
        monkeypatch, tmp_path, "check_failed", "check_failed", "check_failed", "check_failed"
    )
    runner.run_task(task(routes=[CHEAP, BETTER]))
    assert len(script.runs) == 4
    assert script.ending() == ("escalated", "check_failed")


@pytest.mark.parametrize(
    "ended",
    [
        "agent_failed",
        "agent_budget_spent",
        "commit_refused_pinned",
        "prepare_failed",
        "run_refused",
    ],
)
def test_any_other_failure_goes_straight_to_the_next_route(monkeypatch, tmp_path, ended):
    script = Script(monkeypatch, tmp_path, ended, "committed", pushes=["pushed"])
    runner.run_task(task(routes=[CHEAP, BETTER]))
    assert [purpose for purpose, _ in script.runs] == ["task-worker", "task-better"]
    assert script.named("end_attempt")[0]["reason"] == ended
    assert script.named("end_attempt")[0]["status"] == "failed"


def test_a_budget_spent_on_the_last_route_escalates_with_that_reason(monkeypatch, tmp_path):
    script = Script(monkeypatch, tmp_path, "agent_budget_spent")
    runner.run_task(task())
    assert script.ending() == ("escalated", "agent_budget_spent")


def test_a_cli_route_is_skipped_with_a_logged_reason(monkeypatch, tmp_path, caplog):
    caplog.set_level("INFO", logger="homelab-task-runner")
    script = Script(monkeypatch, tmp_path, "committed", pushes=["pushed"])
    runner.run_task(task(routes=[CLI, CHEAP]))
    assert script.runs == [("task-worker", None)]
    assert "reason=unusable_route" in caplog.text


@pytest.mark.parametrize(
    "routes", [[], [CLI], [{"kind": "other"}], ["x"], [{"kind": "litellm", "purpose": "../x"}]]
)
def test_no_usable_route_escalates_without_an_attempt(monkeypatch, tmp_path, routes):
    script = Script(monkeypatch, tmp_path)
    runner.run_task(task(routes=routes))
    assert script.runs == []
    assert script.ending() == ("escalated", "no_usable_route")


def test_attempt_numbers_continue_after_a_dead_runner(monkeypatch, tmp_path):
    script = Script(monkeypatch, tmp_path, "committed", pushes=["pushed"])
    runner.run_task(task(next_attempt=4))
    assert script.named("begin_attempt")[0]["number"] == 4


def test_a_moved_branch_reruns_the_task_once(monkeypatch, tmp_path):
    script = Script(
        monkeypatch, tmp_path, "committed", "committed", pushes=["push_rejected", "pushed"]
    )
    runner.run_task(task())
    assert len(script.runs) == 2
    assert script.ending() == ("done", None)
    assert [event["number"] for event in script.named("end_attempt")] == [1, 2]
    assert script.named("end_attempt")[0]["reason"] == "push_rejected"


def test_a_branch_that_moves_twice_escalates(monkeypatch, tmp_path):
    script = Script(
        monkeypatch, tmp_path, "committed", "committed", pushes=["push_rejected", "push_rejected"]
    )
    runner.run_task(task())
    assert len(script.runs) == 2
    assert script.ending() == ("escalated", "push_rejected")


@pytest.mark.parametrize("reason", ["push_key_missing", "push_failed"])
def test_a_blocked_push_escalates_without_trying_another_route(monkeypatch, tmp_path, reason):
    script = Script(monkeypatch, tmp_path, "committed", pushes=[reason])
    runner.run_task(task(routes=[CHEAP, BETTER]))
    assert len(script.runs) == 1
    assert script.ending() == ("escalated", reason)


@pytest.mark.parametrize(
    "repository", ["repo", "a/b/c", "../x", "a/..", "a/b c", "-a/b", "a/b\n", "a/.."]
)
def test_a_bad_repository_fails_before_any_attempt(monkeypatch, tmp_path, repository):
    script = Script(monkeypatch, tmp_path)
    runner.run_task(task(repository=repository))
    assert script.runs == []
    assert script.ending() == ("failed", "repository_refused")


def test_a_runner_fault_fails_the_task_and_keeps_serving(monkeypatch, tmp_path):
    script = Script(monkeypatch, tmp_path)
    monkeypatch.setattr(runner, "work", lambda item: {}["x"])
    runner.run_task(task())
    assert script.ending() == ("failed", "runner_error_KeyError")


def test_nothing_logged_carries_task_content(monkeypatch, tmp_path, caplog):
    caplog.set_level("INFO", logger="homelab-task-runner")
    Script(monkeypatch, tmp_path, "check_failed", "committed", pushes=["pushed"])
    runner.run_task(task(goal="do-not-log", title="do-not-log", check_command="do-not-log"))
    assert "do-not-log" not in caplog.text
    assert "output of" not in caplog.text


def test_the_ticket_names_the_limits_and_the_message_file():
    text = runner.ticket(task(), None)
    assert "The goal" in text and "- src/app.py" in text and "pytest -q" in text
    assert runner.COMMIT_MESSAGE in text
    assert "failed its check" not in text
    assert "boom" in runner.ticket(task(), ("check", "boom"))
    assert "rejected by review" not in runner.ticket(task(), ("check", "boom"))
    rejected = runner.ticket(task(), ("review", "Fix the skip.\nAnd more."))
    assert "An earlier try at this task was rejected by review, and was discarded." in rejected
    assert "> Fix the skip.\n> And more.\n" in rejected
    assert "failed its check" not in rejected


# The review.


def rejected(notes="Fix the skip."):
    return "review_rejected", {"review_notes": notes, "check_log": "check.log"}


def test_every_run_is_asked_to_review_with_the_first_usable_reviewer_route(monkeypatch, tmp_path):
    script = Script(monkeypatch, tmp_path, "committed", pushes=["pushed"])
    other = {"kind": "litellm", "purpose": "task-other"}
    runner.run_task(task(review_routes=[CLI, REVIEWER, other]))
    assert script.review_purposes == ["task-reviewer"]
    (attempt,) = script.named("end_attempt")
    assert attempt["review_route"] == REVIEWER


@pytest.mark.parametrize(
    "routes", [[], [CLI], [{"kind": "other"}], [{"kind": "litellm", "purpose": "../x"}]]
)
def test_no_usable_review_route_escalates_before_any_attempt(monkeypatch, tmp_path, routes):
    script = Script(monkeypatch, tmp_path)
    runner.run_task(task(review_routes=routes))
    assert script.runs == [] and script.named("begin_attempt") == []
    assert script.ending() == ("escalated", "no_usable_review_route")


def test_a_rejection_retries_once_on_its_route_with_the_notes(monkeypatch, tmp_path):
    script = Script(monkeypatch, tmp_path, rejected(), "committed", pushes=["pushed"])
    runner.run_task(task(routes=[CHEAP, BETTER]))
    assert script.runs == [("task-worker", None), ("task-worker", ("review", "Fix the skip."))]
    assert script.review_purposes == ["task-reviewer"] * 2
    assert script.ending() == ("done", None)
    assert [event["reason"] for event in script.named("end_attempt")] == ["review_rejected", None]


def test_a_second_rejection_moves_to_the_next_route_without_the_notes(monkeypatch, tmp_path):
    script = Script(
        monkeypatch, tmp_path, rejected(), rejected("again"), "committed", pushes=["pushed"]
    )
    runner.run_task(task(routes=[CHEAP, BETTER]))
    assert script.runs == [
        ("task-worker", None),
        ("task-worker", ("review", "Fix the skip.")),
        ("task-better", None),
    ]
    assert script.ending() == ("done", None)


def test_a_rejection_on_every_route_escalates_with_that_reason(monkeypatch, tmp_path):
    script = Script(monkeypatch, tmp_path, *[rejected()] * 2)
    runner.run_task(task())
    assert script.ending() == ("escalated", "review_rejected")


@pytest.mark.parametrize("ended", ["review_failed", "review_diff_too_large"])
def test_a_review_that_cannot_decide_moves_on_at_once(monkeypatch, tmp_path, ended):
    script = Script(monkeypatch, tmp_path, ended, "committed", pushes=["pushed"])
    runner.run_task(task(routes=[CHEAP, BETTER]))
    assert [purpose for purpose, _ in script.runs] == ["task-worker", "task-better"]
    assert script.named("end_attempt")[0]["reason"] == ended


def test_the_last_check_log_is_the_one_read_after_a_fix_round(monkeypatch, tmp_path):
    failed = ("check_failed", {"check_log": "check-2.log"})
    script = Script(monkeypatch, tmp_path, failed, "committed", pushes=["pushed"])
    runner.run_task(task())
    assert script.runs[1] == ("task-worker", ("check", "second output of 1"))


@pytest.mark.parametrize("log", [None, "../../etc/passwd", "check-x.log", "events.jsonl"])
def test_a_check_log_name_the_record_could_not_have_written_is_not_followed(
    monkeypatch, tmp_path, log
):
    failed = ("check_failed", {"check_log": log})
    script = Script(monkeypatch, tmp_path, failed, "committed", pushes=["pushed"])
    runner.run_task(task())
    assert script.runs[1] == ("task-worker", ("check", "output of 1"))


def test_the_reviews_ride_on_the_attempts_end(monkeypatch, tmp_path):
    reviews = [
        {
            "result": "fix",
            "steps": 3,
            "tokens": {"fresh": 30, "output": 4, "cached": 20, "cache_write": 0},
            "seconds": 8.5,
            "cost_usd": 0.004,
        },
        {"result": "failed", "steps": 1, "tokens": None, "seconds": 2, "cost_usd": None},
    ]
    script = Script(monkeypatch, tmp_path, ("review_failed", {"reviews": reviews}))
    runner.run_task(task())
    (attempt,) = script.named("end_attempt")
    assert attempt["review_route"] == REVIEWER
    assert attempt["reviews"] == [
        {
            "result": "fix",
            "steps": 3,
            "fresh_tokens": 30,
            "cached_tokens": 20,
            "output_tokens": 4,
            "seconds": 8.5,
            "cost_usd": 0.004,
        },
        {
            "result": "failed",
            "steps": 1,
            "fresh_tokens": 0,
            "cached_tokens": 0,
            "output_tokens": 0,
            "seconds": 2,
        },
    ]
    assert "reviews" in attempt and attempt["reviews"] != []


def test_an_attempt_with_no_review_sends_an_empty_list(monkeypatch, tmp_path):
    script = Script(monkeypatch, tmp_path, "check_failed", "check_failed")
    runner.run_task(task())
    assert [event["reviews"] for event in script.named("end_attempt")] == [[], []]


def test_the_events_it_sends_are_ones_the_store_accepts(monkeypatch, tmp_path):
    from homelab.tasks import finish

    reviews = [{"result": "fix", "steps": 1, "tokens": {"fresh": 1}, "seconds": 1}]
    script = Script(monkeypatch, tmp_path, ("review_failed", {"reviews": reviews}), "check_failed")
    runner.run_task(task(routes=[CHEAP, BETTER]))
    for event in script.events:
        if isinstance(event, dict):
            finish.parse(json.dumps(event).encode())


def test_notes_are_never_logged(monkeypatch, tmp_path, caplog):
    caplog.set_level("INFO", logger="homelab-task-runner")
    Script(monkeypatch, tmp_path, rejected("do-not-log"), "committed", pushes=["pushed"])
    runner.run_task(task())
    assert "do-not-log" not in caplog.text


def test_a_run_gets_the_task_and_the_configured_author(monkeypatch, tmp_path):
    seen = {}

    def fake(command, **kwargs):
        seen["command"] = command
        seen["ticket"] = Path(command[command.index("--ticket") + 1]).read_text()
        return subprocess.CompletedProcess(command, 1, b'{"ended": "check_failed"}\n')

    monkeypatch.setattr(runner.subprocess, "run", fake)
    monkeypatch.setenv("HOMELAB_TASK_AUTHOR_NAME", "Configured")
    monkeypatch.setenv("HOMELAB_TASK_AUTHOR_EMAIL", "configured@example.invalid")

    result = runner.run_ticket(task(), tmp_path, "task-worker", "task-reviewer", ("check", "boom"))

    assert result == {"ended": "check_failed"}
    command = seen["command"]
    assert command[0] == runner.RUN
    for flag, value in [
        ("--working-copy", str(tmp_path)),
        ("--check", "pytest -q"),
        ("--purpose", "task-worker"),
        ("--review-purpose", "task-reviewer"),
        ("--pin", "tests"),
        ("--allow", "src/app.py"),
        ("--author-name", "Configured"),
        ("--author-email", "configured@example.invalid"),
    ]:
        assert command[command.index(flag) + 1] == value
    assert "boom" in seen["ticket"]


def test_a_refused_run_is_an_ending_not_a_crash(monkeypatch, tmp_path):
    monkeypatch.setattr(
        runner.subprocess, "run", lambda c, **k: subprocess.CompletedProcess(c, 2, b"")
    )
    monkeypatch.setenv("HOMELAB_TASK_AUTHOR_NAME", "x")
    monkeypatch.setenv("HOMELAB_TASK_AUTHOR_EMAIL", "x")
    assert runner.run_ticket(task(), tmp_path, "p", "r", None) == {"ended": "run_refused"}


# The store.


def test_the_store_is_reached_only_through_the_worker(monkeypatch):
    calls = []

    def fake(command, **kwargs):
        calls.append((command, kwargs["input"]))
        return subprocess.CompletedProcess(command, 0, b"[]")

    monkeypatch.setattr(runner.subprocess, "run", fake)
    assert runner.store("finish", event={"event": "x"}) == []
    assert calls == [
        (
            ["docker", "exec", "-i", "homelab-worker-1", "python", "-m", "homelab.tasks.finish"],
            b'{"event": "x"}',
        )
    ]


@pytest.mark.parametrize(
    "result",
    [subprocess.CompletedProcess([], 1, b"[]"), subprocess.CompletedProcess([], 0, b"not json")],
)
def test_a_worker_that_does_not_answer_is_unavailable(monkeypatch, result):
    monkeypatch.setattr(runner.subprocess, "run", lambda c, **k: result)
    with pytest.raises(runner.StoreUnavailable):
        runner.store("claim")


def test_a_result_is_repeated_until_it_lands(monkeypatch):
    answers = [runner.StoreUnavailable("down"), runner.StoreUnavailable("down"), []]

    def fake(module, *args, event=None):
        answer = answers.pop(0)
        if isinstance(answer, Exception):
            raise answer
        return answer

    monkeypatch.setattr(runner, "store", fake)
    monkeypatch.setattr(runner.time, "sleep", lambda seconds: None)
    assert runner.record({"event": "end_task"}) == []
    assert answers == []


# The loop.


@pytest.fixture
def loop(monkeypatch, tmp_path):
    calls = []
    monkeypatch.setattr(runner, "RUN_LOCK", tmp_path / "lock")
    monkeypatch.setattr(runner.os.path, "ismount", lambda path: True)
    monkeypatch.setattr(runner, "recover", lambda: calls.append("recover"))
    monkeypatch.setattr(runner, "run_task", lambda item: calls.append(item))
    monkeypatch.setattr(runner, "store", lambda module, *args, event=None: None)
    return calls


def test_a_locked_volume_waits_without_touching_docker(monkeypatch, loop):
    monkeypatch.setattr(runner.os.path, "ismount", lambda path: False)
    monkeypatch.setattr(runner, "store", lambda *a, **k: pytest.fail("reached the store"))
    assert runner.turn({"recovered": False}) is False
    assert loop == []


def test_recovery_happens_once_before_the_first_claim(monkeypatch, loop):
    state = {"recovered": False}
    runner.turn(state)
    runner.turn(state)
    assert loop == ["recover"]


def test_a_run_by_hand_holds_the_queue_and_its_containers_stay(monkeypatch, loop):
    import fcntl

    with open(runner.RUN_LOCK, "w") as holder:
        fcntl.flock(holder, fcntl.LOCK_EX)
        assert runner.turn({"recovered": False}) is False
    assert loop == []


def test_a_claimed_task_runs(monkeypatch, loop):
    monkeypatch.setattr(runner, "store", lambda *a, **k: task())
    assert runner.turn({"recovered": True}) is True
    assert loop == [task()]


def test_a_lost_claim_answer_recovers_before_the_next_claim(monkeypatch, loop):
    def down(*args, **kwargs):
        raise runner.StoreUnavailable("down")

    monkeypatch.setattr(runner, "store", down)
    state = {"recovered": True}
    with pytest.raises(runner.StoreUnavailable):
        runner.turn(state)
    assert state["recovered"] is False


def test_recovery_clears_containers_copies_and_the_claim(monkeypatch, tmp_path):
    calls = []

    def fake(command, **kwargs):
        calls.append(command)
        return subprocess.CompletedProcess(command, 0, b"abc\ndef\n")

    monkeypatch.setattr(runner.subprocess, "run", fake)
    monkeypatch.setattr(runner, "WORK", tmp_path / "work")
    monkeypatch.setattr(runner, "store", lambda module, *a, **k: calls.append((module, a)) or 1)
    (tmp_path / "work/7/.git").mkdir(parents=True)

    runner.recover()

    assert calls[0][:3] == ["docker", "ps", "--all"]
    assert "name=^/homelab-task-" in calls[0]
    assert calls[1] == ["docker", "rm", "--force", "abc", "def"]
    assert calls[2] == ("claim", ("--recover",))
    assert list((tmp_path / "work").iterdir()) == []
    assert (tmp_path / "work").stat().st_mode & 0o777 == 0o755


# Real git: the clone and the push.


def git(repo, *args):
    return subprocess.run(
        ["git", *args],
        cwd=repo,
        env={**os.environ, **ENV},
        check=True,
        capture_output=True,
        text=True,
    ).stdout


@pytest.fixture
def remote(tmp_path, monkeypatch):
    """A bare remote on `trunk` with a slice branch, and the runner pointed at it."""
    bare = tmp_path / "remote.git"
    bare.mkdir()
    git(bare, "init", "-q", "--bare", "-b", "trunk")
    seed = tmp_path / "seed"
    seed.mkdir()
    git(seed, "init", "-q", "-b", "trunk")
    (seed / "app.py").write_text("VALUE = 1\n")
    git(seed, "add", ".")
    git(seed, "commit", "-qm", "base")
    git(seed, "push", "-q", str(bare), "trunk", "trunk:slice-branch")
    monkeypatch.setattr(runner, "clone_url", lambda repository: str(bare))
    monkeypatch.setattr(runner, "push_url", lambda repository: str(bare))
    monkeypatch.setattr(runner, "as_task_user", dict)
    monkeypatch.setattr(runner, "WORK", tmp_path / "work")
    monkeypatch.setattr(runner, "KEYS", tmp_path)
    monkeypatch.setattr(
        runner.pwd,
        "getpwnam",
        lambda name: type("Account", (), {"pw_uid": os.getuid(), "pw_gid": os.getgid()}),
    )
    (tmp_path / "work").mkdir()
    (tmp_path / "task-push-repo").write_text("key")
    return bare


def committed(copy, text="VALUE = 2\n"):
    (copy / "app.py").write_text(text)
    git(copy, "add", ".")
    git(copy, "commit", "-qm", "work")
    return git(copy, "rev-parse", "HEAD").strip()


def tip(bare, branch="slice-branch"):
    return git(bare, "rev-parse", f"refs/heads/{branch}").strip()


def test_a_copy_is_the_branch_tip_with_no_template_hooks(remote):
    copy = runner.fresh_copy(task())
    assert copy == runner.WORK / "7"
    assert git(copy, "rev-parse", "HEAD").strip() == tip(remote)
    assert git(copy, "rev-parse", "--abbrev-ref", "HEAD").strip() == "slice-branch"
    assert not (copy / ".git/hooks").exists()
    assert (copy / ".git").is_dir()


def test_a_second_copy_starts_clean(remote):
    copy = runner.fresh_copy(task())
    (copy / "left-behind").write_text("x")
    assert not (runner.fresh_copy(task()) / "left-behind").exists()


def test_a_failed_clone_is_none(remote):
    assert runner.fresh_copy(task(branch="no-such-branch")) is None


def test_removal_does_not_follow_a_link_out(remote, tmp_path):
    copy = runner.fresh_copy(task())
    outside = tmp_path / "outside"
    outside.mkdir()
    (outside / "keep").write_text("x")
    (copy / "link").symlink_to(outside)
    runner.remove_copy(task())
    assert not copy.exists()
    assert (outside / "keep").exists()


def test_the_default_branch_comes_from_the_remote(remote):
    assert runner.default_branch("example/repo") == "trunk"


@pytest.mark.parametrize(
    "branch,refused", [("main", True), ("trunk", True), ("slice-branch", False)]
)
def test_the_branch_rule(remote, branch, refused):
    if refused:
        with pytest.raises(runner.Refused, match="default_branch_refused"):
            runner.refuse_branch(task(branch=branch))
    else:
        runner.refuse_branch(task(branch=branch))


def test_an_unknown_default_branch_is_refused(monkeypatch):
    monkeypatch.setattr(runner, "default_branch", lambda repository: None)
    with pytest.raises(runner.Refused, match="default_branch_unknown"):
        runner.refuse_branch(task(branch="slice-branch"))


def test_a_push_lands_exactly_the_commit_on_the_tasks_branch(remote):
    copy = runner.fresh_copy(task())
    commit = committed(copy)
    trunk = tip(remote, "trunk")
    assert runner.push(task(), copy, commit) == "pushed"
    assert tip(remote) == commit
    assert tip(remote, "trunk") == trunk


@pytest.mark.parametrize("branch", ["main", "trunk"])
def test_a_push_to_a_default_branch_is_refused_and_sends_nothing(remote, branch):
    copy = runner.fresh_copy(task())
    commit = committed(copy)
    trunk = tip(remote, "trunk")
    assert runner.push(task(branch=branch), copy, commit) == "push_default_branch_refused"
    assert tip(remote, "trunk") == trunk
    assert "main" not in git(remote, "branch", "--list", "main")


def test_a_moved_branch_is_rejected_and_not_overwritten(remote):
    copy = runner.fresh_copy(task())
    commit = committed(copy)
    other = runner.WORK / "other"
    git(runner.WORK, "clone", "-q", "--branch", "slice-branch", str(remote), "other")
    moved = committed(other, "VALUE = 3\n")
    git(other, "push", "-q", "origin", "slice-branch")

    assert runner.push(task(), copy, commit) == "push_rejected"
    assert tip(remote) == moved


def test_a_remote_refusal_is_not_taken_for_a_moved_branch(remote):
    hook = remote / "hooks/pre-receive"
    hook.write_text("#!/bin/sh\nexit 1\n")
    hook.chmod(0o755)
    copy = runner.fresh_copy(task())
    assert runner.push(task(), copy, committed(copy)) == "push_failed"


def test_a_push_runs_no_hook_of_the_copy(remote, tmp_path):
    copy = runner.fresh_copy(task())
    commit = committed(copy)
    (copy / ".git/hooks").mkdir()
    marker = tmp_path / "ran"
    hook = copy / ".git/hooks/pre-push"
    hook.write_text(f"#!/bin/sh\ntouch {marker}\nexit 1\n")
    hook.chmod(0o755)
    assert runner.push(task(), copy, commit) == "pushed"
    assert not marker.exists()


def test_no_key_stops_before_git(remote, tmp_path):
    copy = runner.fresh_copy(task())
    commit = committed(copy)
    before = tip(remote)
    (tmp_path / "task-push-repo").unlink()
    assert runner.push(task(), copy, commit) == "push_key_missing"
    assert tip(remote) == before


def test_a_push_names_its_key_refspec_and_host_keys_and_never_forces(monkeypatch, tmp_path):
    seen = {}

    def fake(command, **kwargs):
        seen.update(command=command, env=kwargs["env"], cwd=kwargs["cwd"], kwargs=kwargs)
        return subprocess.CompletedProcess(command, 0, "", "")

    monkeypatch.setattr(runner, "refuse_branch", lambda item: None)
    monkeypatch.setattr(runner, "KEYS", tmp_path)
    (tmp_path / "task-push-repo").write_text("key")
    monkeypatch.setattr(runner.subprocess, "run", fake)

    assert runner.push(task(), Path("/work/7"), "a" * 40) == "pushed"

    command = seen["command"]
    assert command[-2:] == [
        "git@github.com:example/repo.git",
        f"{'a' * 40}:refs/heads/slice-branch",
    ]
    assert "--force" not in command and not any("+" in item for item in command)
    assert not any(item.startswith("--force") for item in command)
    assert "--no-verify" in command and "core.hooksPath=/dev/null" in command
    assert command[1:3] == ["--git-dir", "/work/7/.git"]
    assert seen["cwd"] == "/"
    assert "user" not in seen["kwargs"]
    ssh = seen["env"]["GIT_SSH_COMMAND"]
    assert str(tmp_path / "task-push-repo") in ssh
    assert "StrictHostKeyChecking=yes" in ssh
    assert runner.KNOWN_HOSTS in ssh
    assert seen["env"]["GIT_CONFIG_GLOBAL"] == os.devnull


def test_a_commit_that_is_not_an_id_is_never_a_refspec(monkeypatch, tmp_path):
    monkeypatch.setattr(runner, "refuse_branch", lambda item: None)
    monkeypatch.setattr(runner, "KEYS", tmp_path)
    (tmp_path / "task-push-repo").write_text("key")
    monkeypatch.setattr(runner.subprocess, "run", lambda *a, **k: pytest.fail("ran git"))
    assert runner.push(task(), tmp_path, "+HEAD") == "push_bad_commit"


def test_the_key_is_per_repository():
    assert runner.key_path("example/repo") == Path("/root/.ssh/task-push-repo")


# What the node runs it with.


def test_the_service_is_a_host_service_on_the_volume():
    unit = ConfigParser(interpolation=None, strict=False)
    unit.optionxform = str
    unit.read(ROOT / "node/etc/systemd/system/homelab-task-runner.service")
    assert unit.get("Service", "ExecStart") == "/usr/local/sbin/homelab-task-runner"
    assert unit.get("Service", "Restart") == "always"
    assert unit.get("Install", "WantedBy") == "homelab-data.target"
    assert "homelab.service" in unit.get("Unit", "After").split()
    assert "homelab.service" in unit.get("Unit", "PartOf").split()
    text = (ROOT / "node/etc/systemd/system/homelab-task-runner.service").read_text()
    assert "HOMELAB_TASK_AUTHOR_NAME=" in text and "HOMELAB_TASK_AUTHOR_EMAIL=" in text


def test_only_the_proxy_has_a_way_out_of_the_package_network():
    compose = yaml.safe_load((ROOT / "compose.yaml").read_text())
    networks = compose["networks"]
    assert networks["packages"] == {
        "name": "homelab-packages",
        "internal": True,
        "ipam": {"config": [{"subnet": "172.26.0.0/16"}]},
    }
    assert "internal" not in networks["package-egress"]
    members = {
        name: service.get("networks", [])
        for name, service in compose["services"].items()
        if {"packages", "package-egress"} & set(service.get("networks", []))
    }
    assert members == {"package-proxy": ["packages", "package-egress"]}
    proxy = compose["services"]["package-proxy"]
    assert "ports" not in proxy and "secrets" not in proxy and "volumes" not in proxy
    assert proxy["read_only"] is True and proxy["cap_drop"] == ["ALL"]


def test_the_proxy_allows_only_https_to_the_package_hosts():
    folder = ROOT / "node/opt/homelab-package-proxy"
    config = (folder / "tinyproxy.conf").read_text().splitlines()
    for line in ["ConnectPort 443", "FilterDefaultDeny Yes", "FilterURLs On"]:
        assert line in config
    assert (folder / "package-hosts").read_text().splitlines() == [
        r"^pypi\.org:443$",
        r"^files\.pythonhosted\.org:443$",
        r"^registry\.npmjs\.org:443$",
    ]
    dockerfile = (folder / "Dockerfile").read_text()
    assert "tinyproxy-bin" in dockerfile and "USER node" in dockerfile
    task_image = (ROOT / "node/opt/homelab-task/Dockerfile").read_text().splitlines()
    assert next(line for line in dockerfile.splitlines() if line.startswith("FROM ")) == next(
        line for line in task_image if line.startswith("FROM ")
    )


def test_no_container_is_given_docker_or_a_key():
    text = (ROOT / "compose.yaml").read_text()
    assert "docker.sock" not in text and "/root/.ssh" not in text
    run = (ROOT / "node/usr/local/sbin/homelab-task-run").read_text()
    assert "docker.sock" not in run and ".ssh" not in run and "GIT_SSH" not in run


def test_host_keys_are_githubs_only():
    lines = (ROOT / "node/etc/homelab-task/github_known_hosts").read_text().splitlines()
    assert {line.split()[0] for line in lines} == {"github.com"}
    assert json.dumps(lines).count("ssh-ed25519") == 1


def test_the_cost_of_a_run_is_passed_on_and_an_unknown_one_stays_empty(monkeypatch, tmp_path):
    script = Script(
        monkeypatch, tmp_path, ("check_failed", {"cost_usd": 0.02}), "check_failed", "check_failed"
    )
    runner.run_task(task(routes=[CHEAP, BETTER]))
    attempts = script.named("end_attempt")
    assert [event.get("cost_usd") for event in attempts] == [0.02, None, None]
    assert "cost_usd" not in attempts[1]


class Notices:
    def __init__(self, monkeypatch, script=None, result=0, error=None):
        self.calls = []
        self.recorded_before = []

        def run(command, **kwargs):
            self.calls.append((command, kwargs))
            if script:
                self.recorded_before.append(len(script.named("end_task")))
            if error:
                raise error
            return subprocess.CompletedProcess(command, result)

        monkeypatch.setattr(runner.subprocess, "run", run)


@pytest.mark.parametrize(
    "fields,outcomes,status",
    [
        ({"repository": "bad repo"}, [], "failed"),
        ({}, ["check_failed", "check_failed"], "escalated"),
    ],
)
def test_a_task_that_stops_sends_one_notice_after_it_is_recorded(
    monkeypatch, tmp_path, fields, outcomes, status
):
    script = Script(monkeypatch, tmp_path, *outcomes)
    notices = Notices(monkeypatch, script)
    runner.run_task(task(**fields))
    ((command, kwargs),) = notices.calls
    assert command == [runner.NOTICE, "--task"]
    ended, reason = script.ending()
    assert ended == status
    assert kwargs["input"] == f"7 sample {ended} {reason}".encode()
    assert notices.recorded_before == [1]


def test_a_notice_carries_no_task_content(monkeypatch, tmp_path):
    Script(monkeypatch, tmp_path, "check_failed", "check_failed")
    notices = Notices(monkeypatch)
    runner.run_task(task(title="SECRET-TITLE", goal="SECRET-GOAL"))
    assert notices.calls[0][1]["input"] == b"7 sample escalated check_failed"


def test_a_done_task_sends_no_notice(monkeypatch, tmp_path):
    Script(monkeypatch, tmp_path, "committed", pushes=["pushed"])
    notices = Notices(monkeypatch)
    runner.run_task(task())
    assert notices.calls == []


@pytest.mark.parametrize(
    "result,error",
    [(1, None), (0, OSError("nope")), (0, subprocess.TimeoutExpired("x", 1))],
)
def test_a_failed_notice_changes_nothing(monkeypatch, tmp_path, caplog, result, error):
    caplog.set_level("INFO", logger="homelab-task-runner")
    script = Script(monkeypatch, tmp_path, "check_failed", "check_failed")
    Notices(monkeypatch, None, result, error)
    runner.run_task(task())
    assert script.ending() == ("escalated", "check_failed")
    assert caplog.text.count("notice failed task=7") == 1
    assert "SECRET" not in caplog.text


@pytest.mark.parametrize("reason", ["cli_failed", "cli_timeout"])
def test_cli_turn_failures_advance_routes_without_retry(monkeypatch, tmp_path, reason):
    script = Script(monkeypatch, tmp_path, reason, "committed", pushes=["pushed"])
    runner.run_task(task(routes=[CODEX, CHEAP]))
    assert script.runs == [(CODEX, None), ("task-worker", None)]
    assert script.named("end_attempt")[0]["reason"] == reason
    assert script.ending() == ("done", None)


def test_cli_failed_check_retries_with_the_same_ticket_feedback(monkeypatch, tmp_path):
    script = Script(monkeypatch, tmp_path, "check_failed", "committed", pushes=["pushed"])
    runner.run_task(task(routes=[CODEX]))
    assert script.runs == [(CODEX, None), (CODEX, ("check", "output of 1"))]
    assert script.ending() == ("done", None)


def test_a_valid_cli_reviewer_is_refused_before_any_attempt(monkeypatch, tmp_path):
    script = Script(monkeypatch, tmp_path)
    runner.run_task(task(review_routes=[CODEX]))
    assert script.runs == [] and script.named("begin_attempt") == []
    assert script.ending() == ("escalated", "no_usable_review_route")


@pytest.mark.parametrize(
    "route",
    [
        {**CODEX, "tool": "other"},
        {**CODEX, "model": None},
        {**CODEX, "model": "--option"},
        {**CODEX, "effort": []},
        {**CODEX, "effort": "unknown"},
    ],
)
def test_invalid_cli_routes_are_refused(route):
    assert runner.usable_routes(task(routes=[route])) == []
