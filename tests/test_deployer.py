"""Exercise the real deploy sequence with in-memory GitHub and a fake Docker engine."""

import hashlib
import importlib.machinery
import importlib.util
import io
import json
import socket
import stat
import subprocess
import tarfile
import threading
import urllib.error
from pathlib import Path
from types import SimpleNamespace

import pytest

REPO = Path(__file__).resolve().parents[1]
loader = importlib.machinery.SourceFileLoader(
    "homelab_deployer", str(REPO / "node/usr/local/sbin/homelab-deployer")
)
spec = importlib.util.spec_from_loader(loader.name, loader)
d = importlib.util.module_from_spec(spec)
loader.exec_module(d)
A, B, C, D = (c * 40 for c in "abcd")
APPLICATION = "test-app"
SOURCE_HEALTH = "node/opt/homelab-deployer/test-app/health"
SOURCE_DOCKERFILE = "node/opt/homelab-deployer/test-app/Dockerfile"
SOURCE_COMPOSE = "node/opt/homelab-deployer/test-app/compose.yaml"
CONFIG = json.loads((REPO / "node/etc/homelab-deployer/test-app.json").read_text())


def archive(files=None, members=()):
    files = files or {
        SOURCE_HEALTH: b"healthy\n",
        SOURCE_DOCKERFILE: b"untrusted Dockerfile\n",
        SOURCE_COMPOSE: b"untrusted compose\n",
        "src/app.txt": b"application content must not enter a record",
        ".github/CODEOWNERS": b"* @TelesforoAleix\n",
    }
    buffer = io.BytesIO()
    with tarfile.open(fileobj=buffer, mode="w:gz") as stream:
        for name, data in files.items():
            member = tarfile.TarInfo("github-wrapper/" + name)
            member.size = len(data)
            member.mode = 0o644
            stream.addfile(member, io.BytesIO(data))
        for member in members:
            stream.addfile(member, io.BytesIO(b"x") if member.isfile() else None)
    return buffer.getvalue()


class GitHub(d.GitHub):
    def __init__(self):
        self.sources = {sha: archive() for sha in (A, B, C, D)}
        self.reviews = [{"id": 1, "state": "APPROVED", "user": {"login": d.OWNER}, "commit_id": D}]
        self.status = "ahead"
        self.on_main = True
        self.fail = None
        self.calls = []
        self.pr_override = None

    def request(self, repository, suffix, archive=False):
        self.calls.append((repository, suffix, archive))
        if self.fail and self.fail in suffix:
            raise d.Refused("github unavailable")
        if suffix.startswith("compare/"):
            commit = suffix.split("/")[1].split("...")[0]
            return {
                "status": self.status,
                "merge_base_commit": {"sha": commit if self.on_main else D},
            }
        if suffix.startswith("tarball/"):
            return self.sources[suffix.split("/")[1]]
        if suffix.startswith("commits/"):
            commit = suffix.split("/")[1]
            return (
                self.pr_override
                if self.pr_override is not None
                else [
                    {
                        "number": 7,
                        "merge_commit_sha": commit,
                        "merged_at": "2026-11-12T00:00:00Z",
                        "base": {"ref": "main", "repo": {"full_name": repository}},
                        "head": {"sha": D},
                    }
                ]
            )
        if suffix.startswith("pulls/"):
            return self.reviews
        raise AssertionError(suffix)


class Docker:
    def __init__(self):
        self.calls = []
        self.images = {}
        self.current = None
        self.fail = set()
        self.crash = None
        self.contexts = []
        self.health_calls = 0

    def __call__(self, args, **kwargs):
        self.calls.append((args, kwargs))
        assert kwargs["stderr"] == subprocess.DEVNULL
        assert kwargs["stdin"] == subprocess.DEVNULL
        assert kwargs["cwd"] == "/"
        assert "GITHUB_TOKEN" not in kwargs["env"]
        tag = kwargs["env"].get("HOMELAB_DEPLOY_TAG")
        output = b""
        success = True
        if args[0] == "/usr/bin/systemctl":
            operation = "dump" if "homelab-dump.service" in args else "alert"
        elif args[1] == "build":
            operation = "build"
            tag = args[args.index("--tag") + 1].split(":")[1]
            self.contexts.append((Path(args[-1]) / SOURCE_HEALTH).read_bytes())
            if (operation, tag) not in self.fail:
                self.images[tag] = self.contexts[-1].strip() == b"healthy"
        elif args[1] == "rm":
            operation = "remove migration"
            assert args == ["/usr/bin/docker", "rm", "-f", "deployer-test-app-migration"]
        elif args[1:3] == ["image", "ls"]:
            operation = "list images"
            output = ("\n".join(self.images) + "\n").encode() if self.images else b""
        elif args[1:3] == ["image", "rm"]:
            operation = "remove"
            tag = args[-1].split(":")[1]
            if (operation, tag) not in self.fail:
                self.images.pop(tag, None)
        elif args[1:3] == ["image", "inspect"]:
            operation = "image inspect"
            tag = args[-1].split(":")[1]
            success = tag in self.images
            output = (self.image_id(tag) + "\n").encode()
        elif args[1] == "inspect":
            operation = "inspect"
            success = self.current is not None
            if success:
                output = (self.image_id(self.current) + " true\n").encode()
        else:
            assert args[1] == "compose"
            operation = args[args.index("-f") + 2]
            if operation == "up" and (operation, tag) not in self.fail:
                success = tag in self.images
                if success:
                    self.current = tag
            elif operation == "stop":
                self.current = None
            elif operation == "ps":
                output = ("1" * 64 + "\n").encode() if self.current else b""
            elif operation == "exec":
                self.health_calls += 1
                success = bool(self.images.get(self.current))
        if self.crash == operation:
            raise Interrupted()
        if (operation, tag) in self.fail or (operation, None) in self.fail:
            success = False
        return SimpleNamespace(returncode=0 if success else 1, stdout=output)

    @staticmethod
    def image_id(tag):
        return "sha256:" + hashlib.sha256(tag.encode()).hexdigest()


class Interrupted(BaseException):
    pass


@pytest.fixture
def env(tmp_path, monkeypatch):
    for name in ("CONFIG", "ROOT", "STATE"):
        directory = tmp_path / name
        directory.mkdir(mode=0o700)
        monkeypatch.setattr(d, name, directory)
    installed = d.ROOT / "test-app"
    installed.mkdir()
    for name in ("compose.yaml", "Dockerfile"):
        (installed / name).write_bytes(
            (REPO / "node/opt/homelab-deployer/test-app" / name).read_bytes()
        )
        (installed / name).chmod(0o644)
    (d.CONFIG / "test-app.json").write_text(json.dumps(CONFIG))
    (d.CONFIG / "callers.json").write_text('{"0":["install","rollback"]}')
    (d.CONFIG / "automatic").write_text("")
    for path in d.CONFIG.iterdir():
        path.chmod(0o644)
    # Preserve actual no-follow and mode checks; CI need not run as root.
    original = d.metadata

    def metadata(info, mode):
        original(SimpleNamespace(st_uid=0, st_gid=0, st_mode=info.st_mode), mode)

    monkeypatch.setattr(d, "metadata", metadata)
    monkeypatch.setattr(d.time, "sleep", lambda _: None)
    github, docker = GitHub(), Docker()
    monkeypatch.setattr(d.subprocess, "run", docker)
    service = d.Deployer(github)
    return SimpleNamespace(service=service, github=github, docker=docker)


def install(env, sha=A, terminal=True, uid=0):
    return env.service.execute(
        {"op": "install", "application": APPLICATION, "sha": sha}, terminal=terminal, uid=uid
    )


def rollback(env):
    return env.service.execute({"op": "rollback", "application": APPLICATION}, terminal=True)


def update_manifest(**updates):
    value = {**CONFIG, **updates}
    (d.CONFIG / "test-app.json").write_text(json.dumps(value))


def record(env):
    return env.service.read(APPLICATION)


def test_successful_steps_use_installed_files_and_variable_tag(env):
    update_manifest(migration=["/app/migrate"])
    result = install(env)
    assert result["result"] == "installed"
    assert record(env)["current"] == A
    assert not (d.STATE / "test-app.intent").exists()
    calls = env.docker.calls
    build = next(args for args, _ in calls if args[1] == "build")
    assert build[build.index("--file") + 1] == str(d.ROOT / CONFIG["dockerfile"])
    compose = [(args, kw) for args, kw in calls if args[1] == "compose"]
    assert all(args[args.index("-f") + 1] == str(d.ROOT / CONFIG["compose"]) for args, _ in compose)
    assert all(kw["env"]["HOMELAB_DEPLOY_TAG"] == A for _, kw in compose)
    assert any("--pull" in args and "run" in args for args, _ in compose)
    assert all("--no-build" not in args for args, _ in compose if "run" in args)
    assert all("--user" not in args for args, _ in compose)  # Installed service owns the user.
    assert not any("untrusted" in arg for args, _ in calls for arg in args)
    assert next(i for i, (args, _) in enumerate(calls) if "homelab-dump.service" in args) < next(
        i for i, (args, _) in enumerate(calls) if "run" in args
    )
    assert env.docker.current == A
    assert env.docker.contexts == [b"healthy\n"]


@pytest.mark.parametrize("status,on_main", [("behind", True), ("diverged", True), ("ahead", False)])
def test_verify_refuses_commit_not_on_main(env, status, on_main):
    env.github.status, env.github.on_main = status, on_main
    with pytest.raises(d.Refused, match="commit is not on main"):
        install(env)
    assert not env.docker.calls
    assert not any(archive for _, _, archive in env.github.calls)


@pytest.mark.parametrize("suffix", ["compare/", "tarball/", "/pulls", "/reviews"])
def test_github_failure_never_changes_running_services(env, suffix):
    env.github.fail = suffix
    with pytest.raises(d.Refused, match="github unavailable"):
        install(env)
    assert env.docker.current is None
    assert not env.docker.calls


@pytest.mark.parametrize(
    "reviews", [[], [{"id": 1, "state": "APPROVED", "user": {"login": "someone"}}]]
)
def test_first_install_requires_owner_review(env, reviews):
    env.github.reviews = reviews
    result = install(env)
    assert result["result"] == "owner review required"
    assert SOURCE_COMPOSE in result["paths"]
    assert not env.docker.calls


def test_guard_differences_include_added_removed_modified_and_modes(env):
    assert install(env)["ok"]
    baseline = archive(
        {
            SOURCE_HEALTH: b"healthy\n",
            SOURCE_DOCKERFILE: b"changed",
            SOURCE_COMPOSE: b"changed",
            "node/new-helper": b"new",
            "src/ordinary": b"anything",
        }
    )
    env.github.sources[B] = baseline
    env.github.reviews = []
    result = install(env, B)
    assert result["result"] == "owner review required"
    assert set(result["paths"]) == {
        SOURCE_DOCKERFILE,
        SOURCE_COMPOSE,
        "node/new-helper",
        ".github/CODEOWNERS",
    }
    assert env.docker.current == A
    env.github.reviews = [
        {"id": 2, "state": "APPROVED", "user": {"login": d.OWNER}, "commit_id": D}
    ]
    assert install(env, B)["ok"]
    assert env.docker.current == B
    assert (d.ROOT / CONFIG["dockerfile"]).read_bytes() != b"changed"


def test_non_guard_change_needs_no_review(env):
    assert install(env)["ok"]
    env.github.reviews = []
    # Same archive guards; the sha/source may differ in ordinary application files.
    assert install(env, B)["ok"]


def test_all_mandatory_guards_apply_even_with_empty_manifest_guard_list():
    files = {
        name: "sha"
        for name in [
            "node/etc/homelab-deployer/automatic",
            "node/usr/local/sbin/homelab-launcher",
            "node/opt/homelab-fence/proxy/allowed-hosts",
            "node/usr/local/sbin/homelab-check-fence",
            ".github/CODEOWNERS",
            ".github/change-classes",
            "a/compose.yaml",
            "a/Dockerfile",
            "src/app.py",
        ]
    }
    assert set(d.guard_inventory(files, [])) == set(files) - {"src/app.py"}
    assert "src/app.py" in d.guard_inventory(files, ["src"])


@pytest.mark.parametrize("state", ["CHANGES_REQUESTED", "DISMISSED"])
def test_latest_owner_decision_overrides_earlier_approval(env, state):
    env.github.reviews.append({"id": 2, "state": state, "user": {"login": d.OWNER}})
    assert install(env)["result"] == "owner review required"


@pytest.mark.parametrize(
    "updates",
    [
        {"merge_commit_sha": D},
        {"merged_at": None},
        {"base": {"ref": "feature"}},
        {"number": "7"},
        {"base": {"ref": "main", "repo": {"full_name": "other/repo"}}},
    ],
)
def test_approval_must_belong_to_this_merge_pr(env, updates):
    pr = env.github.request(CONFIG["repository"], f"commits/{A}/pulls")[0]
    env.github.pr_override = [{**pr, **updates}]
    assert install(env)["result"] == "owner review required"


def test_build_failure_does_not_write_intent_or_switch(env):
    install(env)
    env.docker.fail.add(("build", B))
    assert install(env, B)["result"] == "build failed"
    assert env.docker.current == A
    assert record(env)["current"] == A
    assert not (d.STATE / "test-app.intent").exists()


@pytest.mark.parametrize("operation", ["dump", "run"])
def test_migration_failure_leaves_old_running_and_never_undoes_schema(env, operation):
    install(env)
    update_manifest(migration=["/app/migrate"])
    env.docker.fail.add((operation, None if operation == "dump" else B))
    before = len(env.docker.calls)
    assert install(env, B)["result"] == "migration failed"
    assert env.docker.current == A
    assert record(env)["current"] == A
    assert not any("up" in args for args, _ in env.docker.calls[before:])
    assert not any("undo" in arg for args, _ in env.docker.calls for arg in args)


@pytest.mark.parametrize("operation", ["up", "exec", "ps", "inspect", "image inspect"])
def test_switch_and_health_failures_roll_back_and_health_previous(env, operation):
    install(env)
    env.docker.fail.add((operation, B if operation != "inspect" else None))
    if operation == "inspect":
        # Only the new image's inspect fails; the previous release still verifies.
        original = env.docker.__class__.__call__

        def inspect_new(self, args, **kwargs):
            result = original(self, args, **kwargs)
            if args[1] == "inspect" and self.current == A:
                return SimpleNamespace(returncode=0, stdout=(self.image_id(A) + " true\n").encode())
            return result

        env.docker.__class__.__call__ = inspect_new
        try:
            result = install(env, B)
        finally:
            env.docker.__class__.__call__ = original
    else:
        result = install(env, B)
    assert "rolled back" in result["result"]
    assert not result["ok"]
    assert env.docker.current == A
    assert env.docker.health_calls >= 2
    assert not (d.STATE / "test-app.intent").exists()


def test_failed_rollback_stops_and_alerts_and_retains_intent(env):
    install(env)
    env.docker.fail.update({("exec", B), ("up", A)})
    assert install(env, B)["result"] == "rollback failed"
    assert env.docker.current is None
    assert (d.STATE / "test-app.intent").exists()
    assert any("homelab-notify@deployer.service" in args for args, _ in env.docker.calls)
    with pytest.raises(d.Refused, match="recovery failed"):
        install(env, C)
    env.docker.fail.clear()
    with env.service.locked():
        env.service.recover()
    assert env.docker.current == A


def test_first_install_unhealthy_stops_and_alerts(env):
    env.docker.fail.add(("exec", A))
    assert install(env)["result"] == "rollback failed"
    assert env.docker.current is None
    assert any("stop" in args for args, _ in env.docker.calls)


def test_terminal_rollback_uses_previous_tag_without_build_or_migration(env):
    install(env)
    install(env, B)
    start = len(env.docker.calls)
    assert rollback(env)["result"] == "rolled back"
    assert env.docker.current == A
    assert record(env)["previous"] == B
    assert not any(args[1] == "build" or "run" in args for args, _ in env.docker.calls[start:])


def test_rollback_still_checks_guard_and_main(env):
    install(env)
    env.github.sources[B] = archive({SOURCE_HEALTH: b"healthy\n", SOURCE_DOCKERFILE: b"new"})
    install(env, B)
    env.github.reviews = []
    assert rollback(env)["result"] == "owner review required"
    assert env.docker.current == B


def test_no_previous_commit_is_refused(env):
    with pytest.raises(d.Refused, match="no previous commit"):
        rollback(env)


@pytest.mark.parametrize("crash", ["run", "up", "exec"])
def test_restart_recovers_intent_then_health_or_rollback(env, crash):
    install(env)
    update_manifest(migration=["/app/migrate"])
    env.docker.crash = crash
    with pytest.raises(Interrupted):
        install(env, B)
    intent = json.loads((d.STATE / "test-app.intent").read_text())
    assert intent["state"] == "switching"
    assert intent["old"]["current"] == A
    assert intent["new"] == B
    env.docker.crash = None
    with d.Deployer(env.github).locked():
        d.Deployer(env.github).recover()
    assert env.docker.current == (A if crash == "run" else B)
    assert not (d.STATE / "test-app.intent").exists()


def test_restart_of_unhealthy_new_image_rolls_back(env):
    install(env)
    env.docker.crash = "up"
    with pytest.raises(Interrupted):
        install(env, B)
    env.docker.crash = None
    env.docker.images[B] = False
    with env.service.locked():
        env.service.recover()
    assert env.docker.current == A
    assert record(env)["result"] == "interrupted; rolled back"


def test_intent_is_durable_before_migration_and_switch(env, monkeypatch):
    update_manifest(migration=["/app/migrate"])
    original = d.run

    def observe(args, **kwargs):
        if "run" in args or "up" in args:
            assert json.loads((d.STATE / "test-app.intent").read_text())["new"] == A
        return original(args, **kwargs)

    monkeypatch.setattr(d, "run", observe)
    assert install(env)["ok"]


def test_failed_intent_write_never_migrates_or_switches(env, monkeypatch):
    install(env)
    original = d.atomic

    def fail_intent(path, value):
        if path.suffix == ".intent":
            raise OSError("sensitive disk diagnostic")
        original(path, value)

    monkeypatch.setattr(d, "atomic", fail_intent)
    with pytest.raises(OSError):
        install(env, B)
    assert env.docker.current == A


def test_record_write_failure_keeps_intent_for_recovery(env, monkeypatch):
    install(env)
    original = d.atomic

    def fail_record(path, value):
        if path.suffix == ".json":
            raise OSError("sensitive disk diagnostic")
        original(path, value)

    monkeypatch.setattr(d, "atomic", fail_record)
    with pytest.raises(OSError):
        install(env, B)
    assert (d.STATE / "test-app.intent").exists()
    monkeypatch.setattr(d, "atomic", original)
    with env.service.locked():
        env.service.recover()
    assert record(env)["current"] == B


def test_only_two_newest_release_images_are_kept(env):
    for commit in (A, B, C, D):
        assert install(env, commit)["ok"]
    assert set(env.docker.images) == {C, D}
    assert record(env)["images"] == [D, C]
    assert record(env)["previous"] == C


def test_install_after_rollback_retains_actual_previous_image(env):
    install(env)
    install(env, B)
    rollback(env)
    install(env, C)
    assert set(env.docker.images) == {C, A}
    assert record(env)["previous"] == A


def test_failed_new_image_is_removed_after_healthy_rollback(env):
    install(env)
    env.docker.fail.add(("exec", B))
    install(env, B)
    assert set(env.docker.images) == {A}


def test_socket_refuses_nonautomatic_then_accepts_automatic(env):
    with pytest.raises(d.Refused, match="waits for the owner"):
        install(env, terminal=False)
    assert not env.github.calls
    (d.CONFIG / "automatic").write_text("test-app\n")
    assert install(env, terminal=False)["ok"]
    with pytest.raises(d.Refused, match="unknown caller"):
        install(env, terminal=False, uid=1000)


def test_socket_rollback_cannot_bypass_automatic_gate(env):
    install(env)
    install(env, B)
    with pytest.raises(d.Refused, match="waits for the owner"):
        env.service.execute({"op": "rollback", "application": APPLICATION})


def socket_request(env, data):
    client, server = socket.socketpair()
    with client, server:
        worker = threading.Thread(target=env.service.handle, args=(server,))
        worker.start()
        client.sendall(data)
        client.shutdown(socket.SHUT_WR)
        response = json.loads(client.recv(65536))
        worker.join(timeout=3)
        assert not worker.is_alive()
        return response


def test_real_socket_peer_and_response(env):
    result = socket_request(
        env, json.dumps({"op": "install", "application": APPLICATION, "sha": A}).encode() + b"\n"
    )
    assert result["result"] in ("waits for the owner", "unknown caller")
    assert not env.github.calls


@pytest.mark.parametrize("data", [b"{}", b"x\n", b"{}\n{}\n", b"x" * 4097, b'{"op":1,"op":2}\n'])
def test_socket_rejects_bad_frames(env, data):
    assert not socket_request(env, data)["ok"]
    assert not env.github.calls


@pytest.mark.parametrize(
    "name",
    ["../test-app", "/etc/passwd", "--privileged", "-f", "a/b", "a b", "a\n", "a:b", ".", "..", ""],
)
def test_identifier_rejects_paths_options_and_controls(env, name):
    with pytest.raises(d.Refused, match="invalid identifier"):
        env.service.execute({"op": "install", "application": name, "sha": A}, terminal=True)
    assert not env.github.calls


@pytest.mark.parametrize("commit", ["main", "-f", "../sha", A.upper(), "a" * 39, "a" * 41, None, 4])
def test_sha_validation(env, commit):
    with pytest.raises(d.Refused, match="invalid sha"):
        install(env, commit)


@pytest.mark.parametrize(
    "updates",
    [
        {"unknown": True},
        {"services": []},
        {"services": ["--help"]},
        {"services": ["a", "a"]},
        {"repository": "--option/repo"},
        {"repository": "owner/../repo"},
        {"compose": "../compose.yaml"},
        {"compose": "/tmp/compose.yaml"},
        {"compose": "test-app/other.yaml"},
        {"dockerfile": "test-app/other"},
        {"health": []},
        {"health": "echo healthy"},
        {"health": ["--user", "root"]},
        {"migration": [1]},
        {"migration": ["x\x00y"]},
        {"guards": ["../node"]},
        {"guards": ["node/etc/systemd/system/x@.service"]},
        {"compose": "test@app/compose.yaml"},
        {"dockerfile": "test@app/Dockerfile"},
        {"guards": ["node", "node"]},
        {"guards": "node"},
    ],
)
def test_manifest_is_strict(env, updates):
    update_manifest(**updates)
    with pytest.raises(d.Refused):
        install(env)
    assert not env.github.calls


def test_unknown_manifest_is_refused_before_github(env):
    with pytest.raises(FileNotFoundError):
        env.service.execute({"op": "install", "application": "unknown", "sha": A}, terminal=True)
    assert not env.github.calls


@pytest.mark.parametrize("path", ["test-app.json", "callers.json", "automatic"])
def test_configuration_symlinks_are_refused(env, tmp_path, path):
    source = d.CONFIG / path
    destination = tmp_path / "elsewhere"
    source.rename(destination)
    source.symlink_to(destination)
    with pytest.raises(OSError):
        install(env, terminal=path == "test-app.json")


@pytest.mark.parametrize("key", ["compose", "dockerfile"])
def test_installed_leaf_and_parent_symlinks_are_refused(env, tmp_path, key):
    path = d.ROOT / CONFIG[key]
    moved = tmp_path / "guard"
    path.rename(moved)
    path.symlink_to(moved)
    with pytest.raises(OSError):
        install(env)
    path.unlink()
    moved.rename(path)
    directory = path.parent
    directory.rename(tmp_path / "installed")
    directory.symlink_to(tmp_path / "installed")
    with pytest.raises(OSError):
        install(env)


@pytest.mark.parametrize("name", ["test-app.json", "automatic", "callers.json"])
def test_configuration_wrong_modes_are_refused(env, name):
    (d.CONFIG / name).chmod(0o666)
    with pytest.raises(d.Refused, match="unsafe ownership or mode"):
        install(env, terminal=name == "test-app.json")


def test_archive_template_units_unpack_and_remain_mandatory_guards(env, tmp_path):
    paths = [
        "node/etc/systemd/system/x@.service",
        "node/etc/systemd/system/x@.service.d/y.conf",
    ]
    directory = tarfile.TarInfo("github-wrapper/node/etc/systemd/system/x@.service.d/")
    directory.type = tarfile.DIRTYPE
    members = [directory]
    for path in paths:
        member = tarfile.TarInfo("github-wrapper/" + path)
        member.size = 1
        members.append(member)
    source = archive(members=members)
    destination = tmp_path / "source"
    destination.mkdir()
    inventory = d.unpack(source, destination)
    guards = d.guard_inventory(inventory, [])
    for path in paths:
        assert (destination / path).read_bytes() == b"x"
        assert guards[path] == {
            "sha": hashlib.sha256(b"x").hexdigest(),
            "executable": False,
        }

    update_manifest(guards=[])
    env.github.sources[A] = source
    env.github.reviews = []
    result = install(env)
    assert result["result"] == "owner review required"
    assert set(paths) <= set(result["paths"])
    assert not env.docker.calls
    env.github.reviews = [
        {"id": 1, "state": "APPROVED", "user": {"login": d.OWNER}, "commit_id": D}
    ]
    assert install(env)["result"] == "installed"
    assert all(record(env)["guards"][path] == guards[path] for path in paths)


@pytest.mark.parametrize(
    "kind",
    [tarfile.SYMTYPE, tarfile.LNKTYPE, tarfile.FIFOTYPE, tarfile.CHRTYPE, tarfile.BLKTYPE],
)
def test_archive_never_follows_links_or_extracts_special_files(env, kind):
    member = tarfile.TarInfo("github-wrapper/danger@.service")
    member.type = kind
    member.linkname = "/etc/passwd"
    env.github.sources[A] = archive(members=[member])
    with pytest.raises(d.Refused, match="invalid archive"):
        install(env)
    assert not env.docker.calls


@pytest.mark.parametrize(
    "path",
    [
        "../escape",
        "/absolute",
        "-option/file",
        "wrapper/../../escape",
        "github-wrapper/node/../escape",
        "github-wrapper/node/./file",
        "github-wrapper/node//file",
        "github-wrapper/node/x@.service/../escape",
        "github-wrapper/node/x@.service//file",
        "github-wrapper/node/file name",
        "github-wrapper/" + "x" * 512,
        "other-root/file",
        "github-wrapper/src/app.txt",
    ],
)
def test_archive_traversal_mixed_roots_and_duplicates_are_refused(env, path):
    member = tarfile.TarInfo(path)
    member.size = 1
    env.github.sources[A] = archive(members=[member])
    with pytest.raises(d.Refused):
        install(env)


def test_archive_budgets_and_corruption_are_refused(env, monkeypatch):
    monkeypatch.setattr(d, "MAX_EXPANDED", 1)
    with pytest.raises(d.Refused, match="source too large"):
        install(env)
    env.github.sources[A] = b"not a tarball"
    with pytest.raises(d.Refused, match="invalid archive"):
        install(env)


def test_state_lock_and_records_refuse_links(env, tmp_path):
    target = tmp_path / "outside"
    target.write_text("unchanged")
    (d.STATE / "lock").symlink_to(target)
    with pytest.raises(OSError):
        install(env)
    assert target.read_text() == "unchanged"
    (d.STATE / "lock").unlink()
    (d.STATE / "test-app.json").symlink_to(target)
    with pytest.raises(OSError):
        install(env)


def test_atomic_replaces_leaf_without_following_and_cleans_temporary(env, tmp_path):
    target = tmp_path / "outside"
    target.write_text("unchanged")
    destination = d.STATE / "record"
    destination.symlink_to(target)
    d.atomic(destination, {"result": "installed"})
    assert target.read_text() == "unchanged"
    assert not destination.is_symlink()
    assert stat.S_IMODE(destination.stat().st_mode) == 0o600
    assert not list(d.STATE.glob(".write-*"))


def test_content_and_command_output_do_not_enter_records_or_logs(env, caplog):
    with caplog.at_level("INFO", logger="homelab-deployer"):
        install(env)
    data = (d.STATE / "test-app.json").read_text() + caplog.text
    assert "application content" not in data
    assert "untrusted" not in data
    assert "healthy" not in data
    assert "migrate" not in data


def test_subprocess_errors_and_output_are_suppressed(monkeypatch):
    def failure(*args, **kwargs):
        assert kwargs["stderr"] == subprocess.DEVNULL
        assert kwargs["stdout"] == subprocess.DEVNULL
        raise OSError("private command output")

    monkeypatch.setattr(d.subprocess, "run", failure)
    assert d.run(["/usr/bin/docker", "build"]) == (False, b"")


def test_pagination_fetches_all_reviews_and_fails_closed(env, monkeypatch):
    calls = []

    def request(repo, suffix):
        calls.append(suffix)
        return [{}] * 100 if suffix.endswith("&page=1") else env.github.reviews

    monkeypatch.setattr(env.github, "request", request)
    assert len(list(env.github.pages("owner/repo", "pulls/7/reviews"))) == 101
    assert len(calls) == 2
    monkeypatch.setattr(env.github, "request", lambda *args: [{}] * 100)
    with pytest.raises(d.Refused, match="pagination limit"):
        list(env.github.pages("owner/repo", "pulls/7/reviews"))


def test_github_token_is_private_and_not_sent_to_archive_redirect(env, monkeypatch):
    token = d.CONFIG / "github-token"
    token.write_text("fake-read-only-credential\n")
    token.chmod(0o600)
    requests = []

    class Opener:
        def open(self, request, timeout):
            requests.append(request)
            if len(requests) == 1:
                raise urllib.error.HTTPError(
                    request.full_url,
                    302,
                    "redirect",
                    {
                        "Location": f"https://codeload.github.com/{CONFIG['repository']}/legacy.tar.gz/{A}"
                    },
                    None,
                )
            return io.BytesIO(b"archive")

    monkeypatch.setattr(d.urllib.request, "build_opener", lambda *args: Opener())
    assert d.GitHub().source(CONFIG["repository"], A) == b"archive"
    assert requests[0].get_header("Authorization") == "Bearer fake-read-only-credential"
    assert requests[1].get_header("Authorization") is None
    token.chmod(0o644)
    with pytest.raises(d.Refused, match="unsafe ownership or mode"):
        d.GitHub().source(CONFIG["repository"], A)


@pytest.mark.parametrize(
    "location",
    [
        "http://codeload.github.com/a/b",
        "https://evil.invalid/",
        "https://codeload.github.com:443/a/b",
        "https://codeload.github.com/other/repo/archive",
    ],
)
def test_github_redirects_are_restricted(env, monkeypatch, location):
    token = d.CONFIG / "github-token"
    token.write_text("fake-token")
    token.chmod(0o600)

    def open_request(request, timeout):
        raise urllib.error.HTTPError(request.full_url, 302, "private", {"Location": location}, None)

    monkeypatch.setattr(
        d.urllib.request, "build_opener", lambda *args: SimpleNamespace(open=open_request)
    )
    with pytest.raises(d.Refused, match="github unavailable"):
        d.GitHub().source(CONFIG["repository"], A)


def test_main_refuses_nonroot_and_nonterminal(env, monkeypatch, capsys):
    monkeypatch.setattr(d.os, "geteuid", lambda: 1000)
    assert d.main(["install", APPLICATION, A]) == 1
    assert "root required" in capsys.readouterr().out
    monkeypatch.setattr(d.os, "geteuid", lambda: 0)
    monkeypatch.setattr(d.sys, "stdin", SimpleNamespace(isatty=lambda: False))
    assert d.main(["install", APPLICATION, A]) == 1
    assert "terminal required" in capsys.readouterr().out


def test_main_terminal_entrypoint(env, monkeypatch, capsys):
    monkeypatch.setattr(d.os, "geteuid", lambda: 0)
    monkeypatch.setattr(d.sys, "stdin", SimpleNamespace(isatty=lambda: True))
    monkeypatch.setattr(d, "Deployer", lambda: env.service)
    assert d.main(["install", APPLICATION, A]) == 0
    assert json.loads(capsys.readouterr().out)["result"] == "installed"


def test_shipped_configuration_and_test_application():
    assert (REPO / "node/etc/homelab-deployer/automatic").read_bytes() == b""
    assert json.loads((REPO / "node/etc/homelab-deployer/callers.json").read_text()) == {
        "0": ["install", "rollback"]
    }
    compose = (REPO / "node/opt/homelab-deployer/test-app/compose.yaml").read_text()
    assert "${HOMELAB_DEPLOY_TAG:?commit required}" in compose
    assert 'user: "10001:10001"' in compose
    assert "network_mode: none" in compose
    assert "cap_drop: [ALL]" in compose
    assert "ports:" not in compose


def test_cleanup_failure_is_recorded_and_retried_on_start(env):
    install(env)
    install(env, B)
    env.docker.fail.add(("remove", A))
    result = install(env, C)
    assert result["ok"]
    assert result["image_cleanup_failed"]
    assert record(env)["image_cleanup_failed"]
    assert set(env.docker.images) == {A, B, C}
    env.docker.fail.clear()
    with env.service.locked():
        env.service.recover()
    assert set(env.docker.images) == {B, C}
    assert not record(env)["image_cleanup_failed"]


def test_abandoned_build_is_cleaned_without_removing_other_release(env):
    install(env)
    env.docker.images[D] = True
    with env.service.locked():
        env.service.recover()
    assert set(env.docker.images) == {A}


def test_health_retries_exactly_three_times_then_checks_rollback(env):
    install(env)
    before = env.docker.health_calls
    env.docker.fail.add(("exec", B))
    install(env, B)
    assert env.docker.health_calls - before == 4


def test_guard_executable_bit_change_requires_review(env):
    install(env)
    buffer = io.BytesIO()
    with tarfile.open(fileobj=io.BytesIO(archive()), mode="r:gz") as source:
        with tarfile.open(fileobj=buffer, mode="w:gz") as target:
            for member in source:
                if member.name.endswith("Dockerfile"):
                    member.mode = 0o755
                target.addfile(member, source.extractfile(member))
    env.github.sources[B] = buffer.getvalue()
    env.github.reviews = []
    assert install(env, B)["paths"] == [SOURCE_DOCKERFILE]


def test_all_services_must_be_running_on_target_image(env, monkeypatch):
    install(env)
    config = {**CONFIG, "services": ["test-app", "second"]}
    original = d.run

    def second_stopped(args, **kwargs):
        if "ps" in args and args[-1] == "second":
            return True, b""
        return original(args, **kwargs)

    monkeypatch.setattr(d, "run", second_stopped)
    assert not env.service.health(APPLICATION, config, A)


def test_effective_identity_is_checked_without_a_root_process():
    with pytest.raises(d.Refused, match="unsafe ownership or mode"):
        d.metadata(SimpleNamespace(st_uid=1000, st_gid=0, st_mode=0o100644), 0o644)
    with pytest.raises(d.Refused, match="unsafe ownership or mode"):
        d.metadata(SimpleNamespace(st_uid=0, st_gid=1000, st_mode=0o100644), 0o644)


def test_socket_hides_exception_content(env, monkeypatch):
    def failure(*args, **kwargs):
        raise OSError("credential, file contents and command output")

    monkeypatch.setattr(env.service, "execute", failure)
    result = socket_request(env, b'{"op":"install"}\n')
    assert result == {"ok": False, "result": "deployer unavailable"}


def test_socket_callers_are_uid_zero_only_and_strict(env):
    (d.CONFIG / "callers.json").write_text('{"0":["install"],"1000":["install"]}')
    with pytest.raises(d.Refused, match="invalid callers"):
        install(env, terminal=False)


def test_automatic_list_is_strict(env):
    (d.CONFIG / "automatic").write_text("../app\n")
    with pytest.raises(d.Refused, match="invalid identifier"):
        install(env, terminal=False)


def test_request_fields_are_strict(env):
    for request in (
        {"op": "install", "application": APPLICATION, "sha": A, "options": []},
        {"op": "rollback", "application": APPLICATION, "sha": A},
        {"op": "exec", "application": APPLICATION},
    ):
        with pytest.raises(d.Refused):
            env.service.execute(request, terminal=True)
    assert not env.github.calls


def test_failure_to_stop_or_alert_is_visible_without_diagnostics(env, caplog):
    install(env)
    env.docker.fail.update({("exec", A), ("exec", B), ("stop", B), ("alert", None)})
    result = install(env, B)
    assert result["result"] == "rollback failed"
    assert result["stop_failed"] and result["alert_failed"]
    assert record(env)["stop_failed"] and record(env)["alert_failed"]
    assert "deployer alert failed" in caplog.text


def test_failed_migration_cleans_fixed_transient_container(env):
    install(env)
    update_manifest(migration=["/app/migrate"])
    env.docker.fail.add(("run", B))
    install(env, B)
    migration = next(args for args, _ in env.docker.calls if "run" in args)
    assert migration[migration.index("--name") + 1] == "deployer-test-app-migration"
    assert any(args[1] == "rm" for args, _ in env.docker.calls)
    assert env.docker.current == A


def test_health_invalid_container_identifiers_lead_to_rollback(env, monkeypatch):
    install(env)
    original = d.run

    def malformed(args, **kwargs):
        if "ps" in args and kwargs.get("tag") == B:
            return True, b"\xff\n"
        return original(args, **kwargs)

    monkeypatch.setattr(d, "run", malformed)
    assert install(env, B)["result"] == "health failed; rolled back"
    assert env.docker.current == A


def test_verification_and_guard_refusals_record_identifiers_and_fixed_reason(env):
    env.github.on_main = False
    with pytest.raises(d.Refused):
        install(env)
    assert record(env)["commit"] == A
    assert record(env)["result"] == "commit is not on main"
    assert record(env)["current"] is None
    env.github.on_main = True
    env.github.reviews = []
    assert install(env)["result"] == "owner review required"
    assert record(env)["result"] == "owner review required"
    assert SOURCE_DOCKERFILE in record(env)["paths"]


def test_service_entrypoint_recovers_before_accepting_requests(env, monkeypatch):
    install(env)
    env.docker.crash = "up"
    with pytest.raises(Interrupted):
        install(env, B)
    env.docker.crash = None
    env.docker.images[B] = False
    monkeypatch.setattr(d.os, "geteuid", lambda: 0)
    monkeypatch.setenv("LISTEN_PID", str(d.os.getpid()))
    monkeypatch.setenv("LISTEN_FDS", "1")
    monkeypatch.setattr(d, "Deployer", lambda: env.service)

    class Listener:
        family = socket.AF_UNIX
        type = socket.SOCK_STREAM

        def __enter__(self):
            return self

        def __exit__(self, *args):
            return False

        def getsockname(self):
            return "/run/homelab-deployer.sock"

        def accept(self):
            assert env.docker.current == A
            assert not (d.STATE / "test-app.intent").exists()
            raise Interrupted()

    monkeypatch.setattr(d.socket, "socket", lambda fileno: Listener())
    with pytest.raises(Interrupted):
        d.main([])


def test_decompression_limit_includes_archive_metadata(env, monkeypatch):
    buffer = io.BytesIO()
    with tarfile.open(fileobj=buffer, mode="w:gz", format=tarfile.PAX_FORMAT) as stream:
        member = tarfile.TarInfo("github-wrapper/file")
        member.pax_headers = {"comment": "x" * 20000}
        member.size = 1
        stream.addfile(member, io.BytesIO(b"x"))
    monkeypatch.setattr(d, "MAX_EXPANDED", 16000)
    env.github.sources[A] = buffer.getvalue()
    with pytest.raises(d.Refused, match="source too large"):
        install(env)
    assert not env.docker.calls
