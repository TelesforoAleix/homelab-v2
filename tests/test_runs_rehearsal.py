"""Every rehearsal scenario has pass/fail coverage without a model or node."""

import base64
from pathlib import Path
from types import SimpleNamespace
from uuid import uuid4

import pytest

from tests import runs_rehearsal as probe

SECRET = "private-goal-final-token-session"
CHECKS = ("slice_write", "review", "question", "cut", "outside", "deny", "ticket_eval")


class FakeAPI:
    def __init__(self):
        self.settings = SimpleNamespace(timeout=30)
        self.refs = {}
        self.runs = {}
        self.deleted = []
        self.posts = []
        self.messages = set()
        self.files = {}
        self.fail = None
        self.busy = 0
        self.stopped = []
        self.copies = True

    def github(self, method, repository, path, body=None):
        if method == "DELETE":
            self.deleted.append((repository, path))
            if self.fail == "delete":
                return 500, None
            return 204, None
        if method == "POST":
            self.refs[repository, body["ref"].removeprefix("refs/heads/")] = body["sha"]
            return 201, {}
        if not path:
            return 200, {"private": self.fail != "private"}
        if path.startswith("git/ref/heads/"):
            branch = path.removeprefix("git/ref/heads/")
            return 200, {"object": {"sha": self.refs.get((repository, branch), "a" * 40)}}
        if path.startswith("compare/"):
            return 200, {"ahead_by": 2 if self.fail == "compare" else 1, "behind_by": 0}
        if path.startswith("commits/"):
            return 200, {"files": [{"filename": self.files[repository], "status": "added"}]}
        if path.startswith("contents/"):
            content = (
                "wrong"
                if self.fail == "test_contents"
                else "def test_rehearsal():\n    assert False\n"
            )
            return 200, {"content": base64.b64encode(content.encode()).decode()}
        raise AssertionError(path)

    def api(self, method, path, body=None):
        if path == "/v1/runs":
            self.posts.append(body)
            run_id = str(uuid4())
            goal = body["work"]["goal"]
            question = goal.startswith("First return")
            outside = body["work"]["allowed_files"] == []
            deny = probe.CANARY in goal
            eval = "eval" in body
            status = "question" if question else "failed" if outside or deny else "done"
            reason = "outside_allowed_files" if outside else "push_refused" if deny else None
            commits = []
            if status == "done" and body["mode"] == "write":
                self.refs[body["repository"], body["branch"]] = "b" * 40
                if body["role"] != "ticket" or eval:
                    commits = ["b" * 40]
                self.files[body["repository"]] = body["work"]["allowed_files"][0]
            result = {
                "run_id": run_id,
                "status": status,
                "status_reason": reason,
                "commits": commits,
                "attempts": [{}],
                "final_message": SECRET,
            }
            if self.fail == "status":
                result["status"] = "escalated"
            if self.fail == "reason" or (self.fail == "eval_reason" and eval):
                result["status_reason"] = "wrong"
            if self.fail == "eval_commits" and eval:
                result["commits"] = []
            self.runs[run_id] = result
            return 201, result
        run_id = path.split("/")[3]
        if method == "GET":
            return 200, self.runs[run_id]
        if path.endswith("/messages"):
            identity = run_id, body["message_id"]
            if identity not in self.messages or self.fail == "duplicate":
                self.messages.add(identity)
                self.runs[run_id]["attempts"].append({})
                self.runs[run_id]["status"] = "done"
            return 200, self.runs[run_id]
        if path.endswith("/cancel"):
            if self.busy:
                self.busy -= 1
                return 409, None
            return 200, self.runs[run_id]
        raise AssertionError(path)

    def session(self, run_id):
        if self.fail == "session" and self.messages:
            return "different"
        return SECRET

    def copies_absent(self, run_ids):
        return self.copies

    def stop_copies(self, run_ids):
        self.stopped.extend(run_ids)
        if self.fail == "stop":
            raise RuntimeError(SECRET)


@pytest.fixture
def rehearsal():
    node = FakeAPI()
    clock = [0]

    def sleep(seconds):
        clock[0] += seconds

    return probe.Rehearsal(node, clock=lambda: clock[0], sleep=sleep)


def prepare(rehearsal, check):
    if check == "review":
        rehearsal.slice_write()


@pytest.mark.parametrize("check", CHECKS)
def test_check_passes(rehearsal, check):
    prepare(rehearsal, check)
    getattr(rehearsal, check)()
    assert all(
        post["branch"].startswith("eval/" if "eval" in post else "rehearsal/")
        for post in rehearsal.node.posts
    )
    assert all(post["level"] == "light" for post in rehearsal.node.posts)


@pytest.mark.parametrize("check", CHECKS)
def test_check_fails_on_wrong_status(rehearsal, check):
    prepare(rehearsal, check)
    rehearsal.node.fail = "status"
    with pytest.raises(AssertionError):
        getattr(rehearsal, check)()


@pytest.mark.parametrize(
    "check,failure",
    [
        ("slice_write", "compare"),
        ("cut", "private"),
        ("cut", "test_contents"),
        ("question", "session"),
        ("question", "duplicate"),
        ("outside", "reason"),
        ("deny", "reason"),
        ("ticket_eval", "eval_commits"),
        ("ticket_eval", "eval_reason"),
    ],
)
def test_each_independent_invariant_is_checked(rehearsal, check, failure):
    rehearsal.node.fail = failure
    with pytest.raises(AssertionError):
        getattr(rehearsal, check)()


def test_review_detects_remote_change(rehearsal, monkeypatch):
    rehearsal.slice_write()
    original = rehearsal.node.api

    def api(*args):
        result = original(*args)
        rehearsal.node.refs[rehearsal.written[:2]] = "c" * 40
        return result

    monkeypatch.setattr(rehearsal.node, "api", api)
    with pytest.raises(AssertionError):
        rehearsal.review()


def test_all_pass_and_nothing_private_printed(rehearsal, capsys):
    assert rehearsal.run()
    assert capsys.readouterr().out.splitlines() == ["PASS: " + label for label in probe.LABELS]
    assert len(rehearsal.node.deleted) == len(rehearsal.branches) == 7


@pytest.mark.parametrize("failure_index", range(len(CHECKS)))
def test_stops_at_first_failure_but_cleans_every_branch(
    rehearsal, monkeypatch, capsys, failure_index
):
    def fail():
        rehearsal.scratch()
        raise RuntimeError(SECRET)

    monkeypatch.setattr(rehearsal, CHECKS[failure_index], fail)
    assert not rehearsal.run()
    lines = capsys.readouterr().out.splitlines()
    assert lines == ["PASS: " + label for label in probe.LABELS[:failure_index]] + [
        "FAIL: " + probe.LABELS[failure_index],
        "PASS: " + probe.LABELS[-1],
    ]
    assert len(rehearsal.node.deleted) == len(rehearsal.branches)
    assert SECRET not in "\n".join(lines)


@pytest.mark.parametrize("failure", ["delete", "copies", "stop"])
def test_cleanup_failure_still_attempts_all_branch_deletions(rehearsal, capsys, failure):
    rehearsal.slice_write()
    rehearsal.scratch()
    rehearsal.node.fail = failure
    if failure == "copies":
        rehearsal.node.copies = False
    if failure == "stop":
        rehearsal.node.busy = 1000
    with pytest.raises(AssertionError):
        rehearsal.cleanup()
    assert len(rehearsal.node.deleted) == 2
    assert not capsys.readouterr().out


def test_cleanup_retries_busy_run(rehearsal):
    rehearsal.slice_write()
    rehearsal.node.busy = 1
    rehearsal.cleanup()
    assert rehearsal.node.stopped == rehearsal.runs


def test_cleanup_never_deletes_other_branch(rehearsal):
    rehearsal.branches.append((probe.PUBLIC, "main"))
    with pytest.raises(AssertionError):
        rehearsal.cleanup()
    assert not rehearsal.node.deleted


def test_session_query_is_read_only_and_selects_only_canonical_run(monkeypatch):
    node = object.__new__(probe.Node)
    node.settings = SimpleNamespace(postgres_container="db", database_user="user", database="name")
    calls = []
    monkeypatch.setattr(probe, "command", lambda argv: calls.append(argv) or SECRET)
    run_id = str(uuid4())
    assert node.session(run_id) == SECRET
    assert (
        calls[0][-2]
        == "BEGIN READ ONLY; SELECT session_id FROM homelab_runs "
        + f"WHERE id = '{run_id}'::uuid; COMMIT;"
    )
    with pytest.raises(ValueError):
        node.session("'; DROP TABLE homelab_runs;")
    assert len(calls) == 1


def test_subprocess_output_is_captured(monkeypatch, capsys):
    def run(argv, **kwargs):
        assert kwargs == {"check": True, "capture_output": True, "text": True, "timeout": 60}
        return SimpleNamespace(stdout=SECRET + "\n")

    monkeypatch.setattr(probe.subprocess, "run", run)
    assert probe.command(["test"]) == SECRET
    assert not capsys.readouterr().out


def test_settings_failure_is_sanitized(monkeypatch, capsys):
    monkeypatch.setattr(probe.os, "geteuid", lambda: 0)

    def fail():
        raise RuntimeError(SECRET)

    monkeypatch.setattr(probe.Settings, "environment", fail)
    assert probe.main() == 1
    assert capsys.readouterr().out == "FAIL: rehearsal settings\n"


def test_copy_checks_include_ticket_work_and_never_query_content(monkeypatch, tmp_path):
    node = object.__new__(probe.Node)
    node.settings = SimpleNamespace(
        postgres_container="db",
        database_user="user",
        database="name",
        work_root=tmp_path / "work",
        run_root=tmp_path / "runs",
        task_work_root=tmp_path / "tasks",
    )
    calls = []

    def command(argv):
        calls.append(argv)
        if "psql" in argv:
            return "123"
        if argv[1:3] == ["ps", "-aq"]:
            return "container"
        if argv[1] == "inspect":
            return probe.json.dumps([{"Source": str(tmp_path / "tasks/123")}])
        raise AssertionError(argv)

    monkeypatch.setattr(probe, "command", command)
    run_id = str(uuid4())
    assert not node.copies_absent([run_id])
    query = next(argv[-2] for argv in calls if "psql" in argv)
    assert "BEGIN READ ONLY; SELECT task_id" in query and run_id in query
    assert "goal" not in query and "session_id" not in query


def test_github_repository_urls(monkeypatch):
    node = object.__new__(probe.Node)
    node.settings = SimpleNamespace(runs_container="runner")
    urls = []
    monkeypatch.setattr(probe, "command", lambda argv: SECRET)
    monkeypatch.setattr(node, "http", lambda method, url, key, body: urls.append(url))
    node.github("GET", probe.PRIVATE, "")
    node.github("GET", probe.PRIVATE, "git/refs/heads/main")
    assert urls == [
        f"https://api.github.com/repos/{probe.PRIVATE}",
        f"https://api.github.com/repos/{probe.PRIVATE}/git/refs/heads/main",
    ]


def test_token_helper_never_passes_secret_in_arguments(monkeypatch, capsys):
    node = object.__new__(probe.Node)
    node.settings = SimpleNamespace(runs_container="runner")
    calls = []
    monkeypatch.setattr(probe, "command", lambda argv: calls.append(argv) or SECRET)
    monkeypatch.setattr(node, "http", lambda method, url, key, body: (204, None))
    assert node.github("DELETE", probe.PUBLIC, "git/refs/heads/rehearsal/fixture") == (204, None)
    assert SECRET not in repr(calls)
    with pytest.raises(ValueError):
        node.github("DELETE", probe.PUBLIC, "git/refs/heads/main")
    assert not capsys.readouterr().out


def test_boot_lock_tmpfiles_rule():
    rule = Path(__file__).resolve().parents[1] / "node/etc/tmpfiles.d/homelab-task-run.conf"
    assert rule.read_text().split() == [
        "f",
        "/run/lock/homelab-task-run.lock",
        "0640",
        "root",
        "10001",
        "-",
        "-",
    ]
