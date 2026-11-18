"""Treat exported Git data as untrusted; only an independently checked tip is delivered."""

import base64
import contextlib
import re
import subprocess
import tempfile
from pathlib import Path

import httpx

from homelab.runs.app import GitHubApp
from homelab.runs.runner import GIT_SECONDS, git_env, private_content_check


class Refused(Exception):
    """A fixed run status reason, never Git output."""


def authorization(token):
    return "Authorization: Basic " + base64.b64encode(("x-access-token:" + token).encode()).decode()


def git_environment(token):
    """Keep repository-scoped credentials in memory, outside Git URLs and argv."""
    return git_env(
        GIT_CONFIG_COUNT="1",
        GIT_CONFIG_KEY_0="http.https://github.com/.extraHeader",
        GIT_CONFIG_VALUE_0=authorization(token),
    )


def git(directory, *args, token=None, check=True):
    environment = git_env() if token is None else git_environment(token)
    try:
        result = subprocess.run(
            [
                "git",
                *(["--git-dir", str(directory)] if directory else []),
                "-c",
                "core.hooksPath=/dev/null",
                "-c",
                "credential.helper=",
                "-c",
                "core.fsmonitor=false",
                "-c",
                "http.followRedirects=false",
                "-c",
                "protocol.allow=never",
                "-c",
                "protocol.https.allow=always",
                *args,
            ],
            env=environment,
            cwd="/",
            capture_output=True,
            timeout=GIT_SECONDS,
            check=False,
        )
        if check and result.returncode:
            raise Refused("push_refused")
        return result
    except (OSError, subprocess.SubprocessError):
        raise Refused("push_refused") from None


def remote_refs(repository, token):
    output = git(
        None, "ls-remote", "--symref", "--", f"https://github.com/{repository}.git", token=token
    ).stdout.decode("ascii")
    default = re.search(r"^ref: refs/heads/(\S+)\tHEAD$", output, re.M)
    if not default:
        raise Refused("push_refused")
    refs = dict(
        (name, commit)
        for commit, name in re.findall(r"^([0-9a-f]{40,64})\trefs/heads/(\S+)$", output, re.M)
    )
    if default[1] not in refs:
        raise Refused("push_refused")
    return default[1], refs


@contextlib.contextmanager
def checked_bundle(bundle, document, parent):
    with tempfile.TemporaryDirectory(dir=parent, prefix="verify-") as temporary:
        directory = Path(temporary) / "git"
        git(None, "init", "--bare", "--template=", "--", str(directory))
        heads = (
            git(directory, "bundle", "list-heads", str(bundle)).stdout.decode("ascii").splitlines()
        )
        branch = "refs/heads/" + document["branch"]
        pairs = [line.split() for line in heads]
        if (
            len(pairs) != 2
            or any(len(p) != 2 for p in pairs)
            or {p[1] for p in pairs} != {"HEAD", branch}
            or pairs[0][0] != pairs[1][0]
            or not re.fullmatch(r"[0-9a-f]{40,64}", pairs[0][0])
        ):
            raise Refused("push_refused")
        tip = pairs[0][0]
        git(directory, "bundle", "verify", str(bundle))
        git(
            directory,
            "-c",
            "protocol.file.allow=always",
            "fetch",
            "--no-tags",
            "--no-recurse-submodules",
            "--",
            str(bundle),
            branch + ":" + branch,
        )
        base = document["base_commit"]
        git(directory, "merge-base", "--is-ancestor", base, tip)
        paths = git(
            directory,
            "diff",
            "--no-ext-diff",
            "--no-textconv",
            "--no-renames",
            "--name-only",
            "-z",
            base,
            tip,
            "--",
        ).stdout.split(b"\0")
        allowed, pinned = document["work"]["allowed_files"], document["work"]["pinned_paths"]
        for name in filter(None, paths):
            path = name.decode("utf-8", "surrogateescape")

            def covers(entry, path=path):
                return path == entry or path.startswith(entry + "/")

            if (allowed is not None and not any(map(covers, allowed))) or any(map(covers, pinned)):
                raise Refused("outside_allowed_files")
        # The shared deny-list checker scans every outgoing commit relative to this ref.
        git(directory, "update-ref", "refs/remotes/origin/" + document["branch"], base)
        yield directory, tip


def repository_is_private(repository, token):
    try:
        with httpx.Client(timeout=30, trust_env=False, follow_redirects=False) as client:
            response = client.get(
                f"https://api.github.com/repos/{repository}",
                headers={
                    "Authorization": "Bearer " + token,
                    "Accept": "application/vnd.github+json",
                },
            )
            response.raise_for_status()
            private = response.json()["private"]
            if type(private) is not bool:
                raise ValueError
    except (httpx.HTTPError, ValueError, KeyError, TypeError):
        raise Refused("push_refused") from None
    return private


def eval_branch_allowed(document):
    marked = document.get("eval") is not None or document.get("eval_set") is not None
    return document["branch"].startswith("eval/") == marked


def push_checked(directory, document, tip):
    if not eval_branch_allowed(document):
        raise Refused("push_refused")
    if tip == document["base_commit"]:
        return []
    token = GitHubApp().token(document["repository"])
    default, _ = remote_refs(document["repository"], token)
    if document["branch"] in {"main", default}:
        raise Refused("push_refused")
    private = repository_is_private(document["repository"], token)
    if private_content_check(document, directory, tip, private=private):
        raise Refused("push_refused")
    result = git(
        directory,
        "push",
        "--porcelain",
        "--no-verify",
        "--no-follow-tags",
        "--",
        f"https://github.com/{document['repository']}.git",
        f"{tip}:refs/heads/{document['branch']}",
        token=token,
        check=False,
    )
    if result.returncode:
        # A non-fast-forward rejection means rewritten history; never force or reclone/retry.
        raise Refused("push_refused")
    return [tip]
