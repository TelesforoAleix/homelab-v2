"""Root node rehearsal. Output is fixed PASS/FAIL labels, never captured content.

Settings and the reversible runner switch are documented in docs/operations.md.
Uses only the standard library on the host; App credentials stay in the runs service.
"""

import base64
import json
import os
import stat
import subprocess
import time
import urllib.error
import urllib.parse
import urllib.request
from dataclasses import dataclass
from pathlib import Path
from uuid import UUID, uuid4

PUBLIC = "TelesforoAleix/homelab-v2"
PRIVATE = "TelesforoAleix/factory"
CANARY = "homelab-deny-canary-7f3e"
LABELS = (
    "slice writes and pushes one commit",
    "review reads without changing the branch",
    "question resumes the same session once",
    "private cut pushes a failing test",
    "outside allowed files refuses delivery",
    "deny canary refuses delivery",
    "ticket and eval preserve the task runner behavior",
    "scratch copies, containers and branches cleaned",
)


@dataclass
class Settings:
    api_url: str
    key_file: Path
    postgres_container: str
    database: str
    database_user: str
    runs_container: str
    work_root: Path = Path("/srv/factory/work")
    run_root: Path = Path("/srv/factory/runs")
    task_work_root: Path = Path("/srv/homelab/task-work")
    timeout: float = 1800

    @classmethod
    def environment(cls):
        return cls(
            os.environ["HOMELAB_REHEARSAL_API_URL"],
            Path(os.environ["HOMELAB_REHEARSAL_KEY_FILE"]),
            os.environ["HOMELAB_REHEARSAL_POSTGRES_CONTAINER"],
            os.environ["HOMELAB_REHEARSAL_DATABASE"],
            os.environ["HOMELAB_REHEARSAL_DATABASE_USER"],
            os.environ["HOMELAB_REHEARSAL_RUNS_CONTAINER"],
        )


def command(argv):
    return subprocess.run(
        argv, check=True, capture_output=True, text=True, timeout=60
    ).stdout.strip()


class NoRedirect(urllib.request.HTTPRedirectHandler):
    def redirect_request(self, request, fp, code, message, headers, newurl):
        return None


class Node:
    def __init__(self, settings):
        self.settings = settings
        parsed = urllib.parse.urlsplit(settings.api_url)
        if parsed.scheme != "http" or parsed.hostname != "127.0.0.1" or parsed.path:
            raise ValueError("loopback API required")
        metadata = settings.key_file.stat()
        if metadata.st_uid != 0 or stat.S_IMODE(metadata.st_mode) & 0o077:
            raise ValueError("root-only key file required")
        self.key = settings.key_file.read_text().strip()
        if not self.key:
            raise ValueError("empty key")

    @staticmethod
    def http(method, url, key, body=None):
        request = urllib.request.Request(
            url,
            data=json.dumps(body).encode() if body is not None else None,
            headers={
                "Authorization": "Bearer " + key,
                "Content-Type": "application/json",
                "Accept": "application/vnd.github+json",
                "User-Agent": "homelab-runs-rehearsal",
            },
            method=method,
        )
        try:
            with urllib.request.build_opener(urllib.request.ProxyHandler({}), NoRedirect()).open(
                request, timeout=60
            ) as response:
                data = response.read()
                return response.status, json.loads(data) if data else None
        except urllib.error.HTTPError as error:
            # Response bodies may contain private caller content; never echo them.
            return error.code, None

    def api(self, method, path, body=None):
        return self.http(method, self.settings.api_url + path, self.key, body)

    def github(self, method, repository, path, body=None):
        if repository not in {PUBLIC, PRIVATE}:
            raise ValueError("repository refused")
        if method == "DELETE" and not path.startswith("git/refs/heads/rehearsal/"):
            raise ValueError("branch deletion refused")
        token = command(
            [
                "docker",
                "exec",
                self.settings.runs_container,
                "python",
                "-c",
                "import sys; from homelab.runs.app import GitHubApp; "
                "print(GitHubApp().token(sys.argv[1]))",
                repository,
            ]
        )
        return self.http(method, f"https://api.github.com/repos/{repository}/{path}", token, body)

    def session(self, run_id):
        # Canonical UUID interpolation only. Only this run's session column is read.
        identifier = str(UUID(run_id))
        return command(
            [
                "docker",
                "exec",
                self.settings.postgres_container,
                "psql",
                "-XAt",
                "-v",
                "ON_ERROR_STOP=1",
                "-U",
                self.settings.database_user,
                "-d",
                self.settings.database,
                "-c",
                "BEGIN READ ONLY; SELECT session_id FROM homelab_runs "
                f"WHERE id = '{identifier}'::uuid; COMMIT;",
                "--quiet",
            ]
        )

    def copy_paths(self, run_ids):
        identifiers = [str(UUID(run_id)) for run_id in run_ids]
        paths = {
            root / ("run-" + UUID(run_id).hex)
            for root in (self.settings.work_root, self.settings.run_root)
            for run_id in identifiers
        }
        if identifiers:
            # Ticket work uses the numeric task ID, unlike whole-run work. This
            # read-only metadata query scopes cleanup to these exact API runs.
            ids = ",".join("'" + run_id + "'::uuid" for run_id in identifiers)
            output = command(
                [
                    "docker",
                    "exec",
                    self.settings.postgres_container,
                    "psql",
                    "-XAt",
                    "-v",
                    "ON_ERROR_STOP=1",
                    "-U",
                    self.settings.database_user,
                    "-d",
                    self.settings.database,
                    "-c",
                    "BEGIN READ ONLY; SELECT task_id FROM homelab_runs "
                    f"WHERE id IN ({ids}) AND request->>'role' = 'ticket'; COMMIT;",
                    "--quiet",
                ]
            )
            for task_id in output.splitlines():
                assert task_id.isascii() and task_id.isdecimal()
                paths.add(self.settings.task_work_root / task_id)
        return paths

    @staticmethod
    def mounted_containers(*, running=False):
        containers = command(["docker", "ps", "-q" if running else "-aq"]).splitlines()
        for container in containers:
            try:
                mounts = json.loads(
                    command(
                        [
                            "docker",
                            "inspect",
                            "--format",
                            "{{json .Mounts}}",
                            container,
                        ]
                    )
                )
            except subprocess.CalledProcessError:
                # A --rm container may disappear between list and inspect.
                if command(["docker", "ps", "-aq", "--filter", "id=" + container]):
                    raise
                continue
            # Inspect only mounts, never container environments or credentials.
            yield container, mounts

    def copies_absent(self, run_ids):
        paths = self.copy_paths(run_ids)
        if any(path.exists() or path.is_symlink() for path in paths):
            return False
        return not any(
            Path(mount["Source"]) in paths
            for _, mounts in self.mounted_containers()
            for mount in mounts
        )

    def stop_copies(self, run_ids):
        """On failure stop only containers bound to this invocation's exact work IDs."""
        paths = {str(path) for path in self.copy_paths(run_ids)}
        for container, mounts in self.mounted_containers(running=True):
            if any(mount["Source"] in paths for mount in mounts):
                command(["docker", "stop", "--time", "10", container])


class Rehearsal:
    def __init__(self, node, *, clock=time.monotonic, sleep=time.sleep):
        self.node, self.clock, self.sleep = node, clock, sleep
        self.branches, self.runs = [], []
        self.written = None

    def tip(self, repository, branch):
        status, value = self.node.github(
            "GET", repository, "git/ref/heads/" + urllib.parse.quote(branch, safe="/")
        )
        assert status == 200
        return value["object"]["sha"]

    def scratch(self, repository=PUBLIC):
        base = self.tip(repository, "main")
        branch = "rehearsal/" + uuid4().hex
        # Record before sending: an ambiguous response must still be cleaned up.
        self.branches.append((repository, branch))
        status, _ = self.node.github(
            "POST", repository, "git/refs", {"ref": "refs/heads/" + branch, "sha": base}
        )
        assert status == 201
        return repository, branch, base

    def submit(self, target, goal, *, role="slice", mode="write", allowed=None, eval=None):
        repository, branch, base = target
        document = {
            "contract": 1,
            "request_id": "rehearsal-" + uuid4().hex,
            "attribution": {"probe": "runs-rehearsal"},
            "repository": repository,
            "branch": branch,
            "base_commit": base,
            "role": role,
            "mode": mode,
            "level": "light",
            "work": {"goal": goal, "allowed_files": allowed, "check_command": "true"},
        }
        if eval is not None:
            document["eval"] = eval
        status, value = self.node.api("POST", "/v1/runs", document)
        assert status in {200, 201}
        run_id = str(UUID(value["run_id"]))
        self.runs.append(run_id)
        return run_id

    def wait(self, run_id):
        deadline = self.clock() + self.node.settings.timeout
        while self.clock() < deadline:
            status, value = self.node.api("GET", "/v1/runs/" + run_id)
            assert status == 200
            if value["status"] not in {"queued", "running"}:
                return value
            self.sleep(2)
        raise TimeoutError()

    def pushed_one(self, target, result):
        repository, branch, base = target
        assert result["status"] == "done" and len(result["commits"]) == 1
        tip = self.tip(repository, branch)
        assert tip == result["commits"][0] and tip != base
        status, value = self.node.github("GET", repository, f"compare/{base}...{tip}")
        assert status == 200 and value["ahead_by"] == 1 and value["behind_by"] == 0
        status, change = self.node.github("GET", repository, "commits/" + tip)
        assert status == 200 and len(change["files"]) == 1
        assert change["files"][0]["status"] == "added"

    @staticmethod
    def write_goal(path, contents):
        return (
            f"Create only {path} with exactly this content: {contents!r}. "
            "Commit that one new file in exactly one commit on the current branch. "
            "Do not push; the platform delivers it. Finish with kind handover."
        )

    def slice_write(self):
        target = self.scratch()
        path = "rehearsal-" + uuid4().hex + ".txt"
        run_id = self.submit(target, self.write_goal(path, "rehearsal\n"), allowed=[path])
        self.pushed_one(target, self.wait(run_id))
        self.written = (target[0], target[1], self.tip(*target[:2]))

    def review(self):
        assert self.written is not None
        target = self.written
        result = self.wait(
            self.submit(
                target,
                "Read the latest commit. Briefly review its trivial added file. "
                "Ask no questions; finish with kind handover.",
                role="review",
                mode="read",
            )
        )
        assert result["status"] == "done" and result["commits"] == []
        assert self.tip(*target[:2]) == target[2]

    def question(self):
        target = self.scratch()
        run_id = self.submit(
            target,
            "First return kind question asking which word to report. Do nothing else. "
            "After the answer, report that word with kind handover. Never edit or commit files.",
            mode="read",
        )
        first = self.wait(run_id)
        assert first["status"] == "question" and len(first["attempts"]) == 1
        session = self.node.session(run_id)
        assert session
        message = {"contract": 1, "message_id": "answer", "text": "Report rehearsal."}
        for _ in range(2):
            status, _ = self.node.api("POST", f"/v1/runs/{run_id}/messages", message)
            assert status == 200
        result = self.wait(run_id)
        assert result["status"] == "done" and len(result["attempts"]) == 2
        assert self.node.session(run_id) == session
        # Retry after completion too: idempotency must not queue a later turn.
        status, _ = self.node.api("POST", f"/v1/runs/{run_id}/messages", message)
        assert status == 200
        status, latest = self.node.api("GET", f"/v1/runs/{run_id}")
        assert status == 200 and latest["status"] == "done" and len(latest["attempts"]) == 2

    def cut(self):
        target = self.scratch(PRIVATE)
        status, metadata = self.node.github("GET", PRIVATE, "")
        assert status == 200 and metadata["private"] is True
        path = "tests/test_rehearsal_" + uuid4().hex + ".py"
        contents = "def test_rehearsal():\n    assert False\n"
        result = self.wait(
            self.submit(
                target,
                self.write_goal(path, contents) + " This is an intentional failing test; "
                "keep its assertion failing, as required for a cut.",
                role="cut",
                allowed=[path],
            )
        )
        self.pushed_one(target, result)
        status, change = self.node.github("GET", PRIVATE, "commits/" + result["commits"][0])
        assert status == 200 and [file["filename"] for file in change["files"]] == [path]
        status, file = self.node.github(
            "GET", PRIVATE, "contents/" + path + "?ref=" + urllib.parse.quote(target[1], safe="")
        )
        assert status == 200 and base64.b64decode(file["content"]).decode() == contents

    def outside(self):
        target = self.scratch()
        path = "rehearsal-outside-" + uuid4().hex + ".txt"
        result = self.wait(self.submit(target, self.write_goal(path, "outside\n"), allowed=[]))
        assert result["status"] == "failed" and result["status_reason"] == "outside_allowed_files"
        assert result["commits"] == [] and self.tip(*target[:2]) == target[2]

    def deny(self):
        target = self.scratch()
        path = "rehearsal-canary-" + uuid4().hex + ".txt"
        result = self.wait(
            self.submit(target, self.write_goal(path, CANARY + "\n"), allowed=[path])
        )
        assert result["status"] == "failed" and result["status_reason"] == "push_private_content"
        assert result["commits"] == [] and self.tip(*target[:2]) == target[2]

    def ticket_eval(self):
        target = self.scratch()
        path = "rehearsal-ticket-" + uuid4().hex + ".txt"
        result = self.wait(
            self.submit(
                target,
                f"Create only {path} containing ticket and a newline. "
                "Let the task runner check and commit it; report completion.",
                role="ticket",
                allowed=[path],
            )
        )
        assert result["status"] == "done" and result["status_reason"] is None
        assert len(result["attempts"]) >= 1
        assert self.tip(*target[:2]) != target[2]
        target = self.scratch()
        path = "rehearsal-eval-" + uuid4().hex + ".txt"
        result = self.wait(
            self.submit(
                target,
                f"Create only {path} containing eval and a newline. "
                "Let the task runner check and commit it; report completion.",
                role="ticket",
                allowed=[path],
                eval={"set": "rehearsal", "case": uuid4().hex, "configuration": "base"},
            )
        )
        assert result["status"] == "done" and result["status_reason"] == "eval_kept"
        assert (
            result["commits"] == []
            and isinstance(result.get("diff_base64"), str)
            and bool(result["diff_base64"])
        )
        assert self.tip(*target[:2]) == target[2]

    def cleanup(self):
        # Cancellation makes ended harness copies eligible immediately. Busy copies
        # remain the service's responsibility; never delete launcher folders by hand.
        failed = False
        deadline = self.clock() + 120
        pending = set(self.runs)
        while pending and self.clock() < deadline:
            for run_id in list(pending):
                try:
                    status, _ = self.node.api("POST", f"/v1/runs/{run_id}/cancel")
                    if status == 200:
                        pending.remove(run_id)
                    elif status != 409:
                        failed = True
                        pending.remove(run_id)
                except Exception:
                    failed = True
                    pending.remove(run_id)
            if pending:
                try:
                    self.node.stop_copies(pending)
                except Exception:
                    failed = True
                self.sleep(2)
        failed |= bool(pending)
        while self.clock() < deadline:
            try:
                if self.node.copies_absent(self.runs):
                    break
            except Exception:
                failed = True
                break
            self.sleep(2)
        else:
            failed = True
        # Attempt every branch even if another cleanup operation failed.
        for repository, branch in self.branches:
            try:
                if not branch.startswith("rehearsal/"):
                    raise ValueError("branch deletion refused")
                status, _ = self.node.github(
                    "DELETE", repository, "git/refs/heads/" + urllib.parse.quote(branch, safe="/")
                )
                assert status in {204, 404}
            except Exception:
                failed = True
        assert not failed

    def run(self):
        passed = True
        try:
            for label, check in zip(
                LABELS,
                (
                    self.slice_write,
                    self.review,
                    self.question,
                    self.cut,
                    self.outside,
                    self.deny,
                    self.ticket_eval,
                ),
                strict=False,
            ):
                try:
                    check()
                except Exception:
                    print("FAIL: " + label, flush=True)
                    passed = False
                    break
                print("PASS: " + label, flush=True)
        finally:
            try:
                self.cleanup()
            except Exception:
                print("FAIL: " + LABELS[-1], flush=True)
                passed = False
            else:
                print("PASS: " + LABELS[-1], flush=True)
        return passed


def main():
    try:
        if os.geteuid() != 0 or not __debug__:
            raise ValueError("root and enabled assertions required")
        node = Node(Settings.environment())
    except Exception:
        print("FAIL: rehearsal settings", flush=True)
        return 1
    return 0 if Rehearsal(node).run() else 1


if __name__ == "__main__":
    raise SystemExit(main())
