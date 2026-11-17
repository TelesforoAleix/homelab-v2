"""Real bundles are checked outside the harness copy before any push."""

import subprocess
from types import SimpleNamespace

import httpx
import pytest

from homelab.runs import delivery


def git(repo, *args):
    return subprocess.run(
        ["git", "-C", str(repo), *args], check=True, capture_output=True, text=True
    ).stdout.strip()


@pytest.fixture
def bundle(tmp_path):
    copy = tmp_path / "copy"
    copy.mkdir()
    git(copy, "init", "-q", "-b", "slice/test")
    git(copy, "config", "user.name", "Fixture")
    git(copy, "config", "user.email", "fixture@example.invalid")
    (copy / "src").mkdir()
    (copy / "src/app").write_text("base")
    (copy / "src/pinned").write_text("pinned")
    git(copy, "add", ".")
    git(copy, "commit", "-qm", "Base")
    base = git(copy, "rev-parse", "HEAD")
    (copy / "src/app").write_text("changed")
    git(copy, "commit", "-qam", "Change")
    tip = git(copy, "rev-parse", "HEAD")
    path = tmp_path / "branch.bundle"
    git(copy, "bundle", "create", str(path), "HEAD", "refs/heads/slice/test")
    document = {
        "branch": "slice/test",
        "base_commit": base,
        "repository": "owner/repo",
        "work": {"allowed_files": ["src"], "pinned_paths": ["src/pinned"]},
    }
    return copy, path, document, tip


def test_bundle_accepts_exact_heads_and_ignores_harness_config(bundle, tmp_path):
    copy, path, document, tip = bundle
    git(copy, "config", "core.fsmonitor", "/must-not-execute")
    git(copy, "config", "include.path", "/must-not-read")
    with delivery.checked_bundle(path, document, tmp_path) as (directory, checked_tip):
        assert checked_tip == tip
        assert str(directory).startswith(str(tmp_path))
        assert "/must-not" not in (directory / "config").read_text()


@pytest.mark.parametrize("kind", ["extra_head", "different_head", "wrong_base", "corrupt"])
def test_bundle_refuses_invalid_export(bundle, tmp_path, kind):
    copy, path, document, tip = bundle
    if kind == "extra_head":
        git(copy, "branch", "extra")
        git(copy, "bundle", "create", str(path), "HEAD", "--branches")
    elif kind == "different_head":
        git(copy, "checkout", "--detach", document["base_commit"])
        git(copy, "bundle", "create", str(path), "HEAD", "refs/heads/slice/test")
    elif kind == "wrong_base":
        document["base_commit"] = "f" * 40
    else:
        path.write_bytes(b"not a bundle")
    with pytest.raises(delivery.Refused, match="push_refused"):
        with delivery.checked_bundle(path, document, tmp_path):
            pytest.fail("must not accept invalid export")


@pytest.mark.parametrize(
    "allowed,pinned,accepted",
    [
        (["src"], ["src/pinned"], True),
        (["src/app"], [], True),
        (["src/ap"], [], False),
        ([], [], False),
        (None, [], True),
        (None, ["src"], False),
        (["docs"], [], False),
    ],
)
def test_bundle_diff_enforces_allowed_and_pinned(bundle, tmp_path, allowed, pinned, accepted):
    _, path, document, _ = bundle
    document["work"].update(allowed_files=allowed, pinned_paths=pinned)
    if accepted:
        with delivery.checked_bundle(path, document, tmp_path):
            pass
    else:
        with pytest.raises(delivery.Refused, match="outside_allowed_files"):
            with delivery.checked_bundle(path, document, tmp_path):
                pytest.fail("must not deliver restricted paths")


def test_deleted_and_renamed_pins_are_checked(bundle, tmp_path):
    copy, path, document, _ = bundle
    git(copy, "mv", "src/pinned", "src/renamed")
    git(copy, "commit", "-qm", "Rename pin")
    git(copy, "bundle", "create", str(path), "HEAD", "refs/heads/slice/test")
    with pytest.raises(delivery.Refused, match="outside_allowed_files"):
        with delivery.checked_bundle(path, document, tmp_path):
            pytest.fail("must not deliver renamed pin")


def github(monkeypatch, *, private=False):
    real_client = httpx.Client

    def respond(request):
        assert request.headers["Authorization"] == "Bearer FAKE-APP-TOKEN"
        return httpx.Response(200, json={"private": private})

    monkeypatch.setattr(
        delivery, "GitHubApp", lambda: SimpleNamespace(token=lambda repository: "FAKE-APP-TOKEN")
    )
    monkeypatch.setattr(
        delivery.httpx,
        "Client",
        lambda **kw: real_client(**kw, transport=httpx.MockTransport(respond)),
    )
    monkeypatch.setattr(delivery, "remote_refs", lambda r, t: ("trunk", {}))


@pytest.mark.parametrize("code", [0, 1])
def test_delivery_push_is_nonforce_and_authentication_is_environment_only(
    bundle, tmp_path, monkeypatch, code
):
    _, path, document, _ = bundle
    github(monkeypatch, private=True)
    with delivery.checked_bundle(path, document, tmp_path) as (directory, tip):
        calls = []

        def run(command, **kwargs):
            calls.append((command, kwargs))
            return SimpleNamespace(returncode=code, stdout=b"PRIVATE GIT RESPONSE")

        monkeypatch.setattr(delivery.subprocess, "run", run)
        if code:
            with pytest.raises(delivery.Refused, match="push_refused"):
                delivery.push_checked(directory, document, tip)
        else:
            delivery.push_checked(directory, document, tip)
        command, options = calls[0]
        assert command[-2:] == ["https://github.com/owner/repo.git", tip + ":refs/heads/slice/test"]
        assert "--force" not in command and "FAKE-APP-TOKEN" not in repr(command)
        assert options["env"]["GIT_CONFIG_VALUE_0"].startswith("Authorization: Basic ")
        assert "credential.helper=" in command and "core.hooksPath=/dev/null" in command


def test_remote_default_is_rechecked_before_delivery(bundle, tmp_path, monkeypatch):
    _, path, document, _ = bundle
    github(monkeypatch)
    monkeypatch.setattr(delivery, "remote_refs", lambda r, t: ("slice/test", {}))
    with delivery.checked_bundle(path, document, tmp_path) as (directory, tip):
        with pytest.raises(delivery.Refused, match="push_refused"):
            delivery.push_checked(directory, document, tip)


def test_public_scan_checks_intermediate_commits_even_when_tip_is_clean(
    bundle, tmp_path, monkeypatch
):
    from homelab.runs import runner

    copy, path, document, _ = bundle
    (copy / "src/leak").write_text(runner.DENY_CANARY)
    git(copy, "add", ".")
    git(copy, "commit", "-qm", "Intermediate change")
    (copy / "src/leak").unlink()
    git(copy, "commit", "-qam", "Remove intermediate file")
    git(copy, "bundle", "create", str(path), "HEAD", "refs/heads/slice/test")
    deny = tmp_path / "deny-list"
    deny.write_text("unrelated denied term\n")
    monkeypatch.setattr(runner, "DENY_LIST", deny)
    github(monkeypatch)
    with delivery.checked_bundle(path, document, tmp_path) as (directory, tip):
        original = delivery.git

        def refuse_push(directory, *args, **kwargs):
            if "push" in args:
                pytest.fail("an intermediate private-content hit must prevent push")
            return original(directory, *args, **kwargs)

        monkeypatch.setattr(delivery, "git", refuse_push)
        with pytest.raises(delivery.Refused, match="push_refused"):
            delivery.push_checked(directory, document, tip)


@pytest.mark.parametrize("reason", ["push_private_content: fixture:1", "push_deny_list_unreadable"])
def test_deny_failures_keep_contract_reason_without_pushing(bundle, tmp_path, monkeypatch, reason):
    _, path, document, _ = bundle
    github(monkeypatch)
    monkeypatch.setattr(delivery, "private_content_check", lambda *a, **kw: reason)
    with delivery.checked_bundle(path, document, tmp_path) as (directory, tip):
        original = delivery.git

        def refuse_push(directory, *args, **kwargs):
            if "push" in args:
                pytest.fail("deny failures must prevent push")
            return original(directory, *args, **kwargs)

        monkeypatch.setattr(delivery, "git", refuse_push)
        with pytest.raises(delivery.Refused, match="^push_refused$"):
            delivery.push_checked(directory, document, tip)
