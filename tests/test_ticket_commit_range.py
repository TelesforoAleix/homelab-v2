"""Ticket commit ranges use the sanitized Git view and the clone's remote branch tip."""

import subprocess

import pytest

from homelab.runs import runner


def git(copy, *args):
    return subprocess.run(
        ["git", "-C", str(copy), *args],
        env=runner.git_env(
            GIT_AUTHOR_NAME="Fixture",
            GIT_AUTHOR_EMAIL="fixture@example.invalid",
            GIT_COMMITTER_NAME="Fixture",
            GIT_COMMITTER_EMAIL="fixture@example.invalid",
        ),
        check=True,
        capture_output=True,
        text=True,
    ).stdout.strip()


def commit(copy, text, message="Fixture change"):
    (copy / "file.txt").write_text(text)
    git(copy, "add", "file.txt")
    git(copy, "commit", "-qm", message)
    return git(copy, "rev-parse", "HEAD")


@pytest.fixture
def history(tmp_path):
    copy = tmp_path / "copy"
    copy.mkdir()
    git(copy, "init", "-q", "-b", "work/ticket")
    base = commit(copy, "Base\n")
    git(copy, "update-ref", "refs/remotes/origin/work/ticket", base)
    first = commit(copy, "Intermediate\n")
    second = commit(copy, "Final\n")
    task = {"repository": "example/repo", "branch": "work/ticket", "base_commit": "0" * 40}
    return task, copy, base, first, second


def test_range_uses_origin_branch_excludes_base_and_orders_oldest_first(history):
    task, copy, base, first, second = history
    with runner.sealed_git_dir(copy) as directory:
        assert runner.outgoing_commits(task, directory, second) == [first, second]
        assert runner.outgoing_commits(task, directory, base) == []
    # The submitted base_commit is deliberately unrelated: the clone's origin ref owns the range.
    git(copy, "update-ref", "refs/remotes/origin/work/ticket", first)
    with runner.sealed_git_dir(copy) as directory:
        assert runner.outgoing_commits(task, directory, second) == [second]


def test_range_is_reachability_difference_including_merge_parents(history):
    task, copy, base, first, second = history
    git(copy, "checkout", "-qb", "side", base)
    (copy / "side.txt").write_text("Side branch\n")
    git(copy, "add", "side.txt")
    git(copy, "commit", "-qm", "Side change")
    side = git(copy, "rev-parse", "HEAD")
    git(copy, "checkout", "-q", "work/ticket")
    git(copy, "merge", "--no-ff", "-qm", "Merge side", "side")
    merge = git(copy, "rev-parse", "HEAD")
    with runner.sealed_git_dir(copy) as directory:
        found = runner.outgoing_commits(task, directory, merge)
    assert set(found) == {first, second, side, merge}
    assert len(found) == 4
    assert found.index(first) < found.index(second) < found.index(merge)
    assert found.index(side) < found.index(merge)


@pytest.mark.parametrize("missing", [False, True])
def test_range_refuses_missing_or_nonancestor_origin_ref(history, missing):
    task, copy, base, first, second = history
    if missing:
        git(copy, "update-ref", "-d", "refs/remotes/origin/work/ticket")
    else:
        git(copy, "update-ref", "refs/remotes/origin/work/ticket", second)
    with runner.sealed_git_dir(copy) as directory:
        with pytest.raises(subprocess.CalledProcessError):
            runner.outgoing_commits(task, directory, first)


def test_range_scan_reuses_supplied_revisions_and_finds_removed_private_content(
    history, tmp_path, monkeypatch
):
    task, copy, base, first, second = history
    secret = "private-marker-391d"
    first = commit(copy, secret + "\n")
    second = commit(copy, "Removed the private line\n")
    deny = tmp_path / "deny"
    deny.write_text(secret + "\n")
    monkeypatch.setattr(runner, "DENY_LIST", deny)
    real_run = subprocess.run

    def no_relisting(command, **kwargs):
        if "rev-list" in command:
            assert "--parents" in command, "the supplied outgoing range must not be listed again"
        assert "PRIVATE" not in repr(command)
        return real_run(command, **kwargs)

    monkeypatch.setattr(subprocess, "run", no_relisting)
    with runner.sealed_git_dir(copy) as directory:
        reason = runner.private_content_check(
            task, directory, second, private=False, commits=[first, second]
        )
    assert reason == "push_private_content: file.txt:1"
    assert secret not in reason


def test_range_scan_keeps_private_repository_bypass(history, tmp_path, monkeypatch):
    task, copy, base, first, second = history
    monkeypatch.setattr(runner, "DENY_LIST", tmp_path / "absent")
    with runner.sealed_git_dir(copy) as directory:
        assert (
            runner.private_content_check(task, directory, second, private=True, commits=[first])
            is None
        )
