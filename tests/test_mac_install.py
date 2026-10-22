import copy
import importlib.machinery
import importlib.util
import json
import plistlib
import runpy
import subprocess
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[1]


def module(name, filename):
    loader = importlib.machinery.SourceFileLoader(name, str(ROOT / "mac" / filename))
    spec = importlib.util.spec_from_loader(name, loader)
    result = importlib.util.module_from_spec(spec)
    loader.exec_module(result)
    return result


@pytest.fixture
def installer(monkeypatch, tmp_path):
    result = module("mac_install", "install-mac-tools")
    monkeypatch.setattr(result, "ROOT", tmp_path / "Application Support/homelab/mac")
    monkeypatch.setattr(result, "AGENTS", tmp_path / "LaunchAgents")
    result.ROOT.mkdir(parents=True)
    result.AGENTS.mkdir()
    (result.ROOT / "install.json").write_text(json.dumps({"python": "/fixed/python3"}))
    # The real path resolver, with only its install root replaced.
    command = runpy.run_path(str(ROOT / "mac/installed_tools.py"))["command"]
    monkeypatch.setitem(command.__globals__, "ROOT", result.ROOT)
    monkeypatch.setattr(result, "installed_command", command)
    return result


@pytest.fixture
def repository(tmp_path, monkeypatch, installer):
    remote = tmp_path / "remote.git"
    repo = tmp_path / "repo"
    subprocess.run(["git", "init", "--bare", str(remote)], check=True, capture_output=True)
    subprocess.run(["git", "init", "-b", "main", str(repo)], check=True, capture_output=True)

    def git(*args):
        return subprocess.check_output(["git", "-C", str(repo), *args], text=True).strip()

    git("config", "user.email", "test@example.invalid")
    git("config", "user.name", "Test")
    (repo / "file").write_text("one")
    git("add", "file")
    git("commit", "-m", "initial")
    git("remote", "add", "origin", str(remote))
    git("push", "-u", "origin", "main")
    monkeypatch.setattr(installer, "SOURCE", repo / "mac")
    return repo, git


def test_install_refuses_dirty_and_untracked(installer, repository):
    repo, git = repository
    assert installer.revision() == git("rev-parse", "HEAD")
    (repo / "file").write_text("dirty")
    with pytest.raises(ValueError, match="dirty"):
        installer.revision()
    git("restore", "file")
    (repo / "untracked").touch()
    with pytest.raises(ValueError, match="dirty"):
        installer.revision()


def test_install_refuses_unmerged_commit_accepts_contained_commit(installer, repository):
    repo, git = repository
    base = git("rev-parse", "HEAD")
    git("switch", "-c", "feature")
    (repo / "file").write_text("two")
    git("commit", "-am", "feature")
    with pytest.raises(ValueError, match="not contained"):
        installer.revision()
    git("switch", "main")
    git("merge", "--ff-only", "feature")
    git("push")
    git("checkout", base)
    assert installer.revision() == base


@pytest.mark.parametrize(
    "kind,arguments,extra",
    [
        ("listener", ["serve"], {"KeepAlive": True, "WorkingDirectory": str(ROOT)}),
        ("router", ["serve"], {"KeepAlive": True}),
        ("profile", ["tick"], {"StartInterval": 30}),
        ("profile", ["job-worker", "dictation-recheck"], {"StartCalendarInterval": {"Hour": 4}}),
    ],
)
def test_rewrites_preserve_flags_and_calendar(installer, kind, arguments, extra):
    payload = {
        "ProgramArguments": [
            str(ROOT / ".venv/bin/python3"),
            str(ROOT / f"mac/homelab-{kind}"),
            *arguments,
        ],
        **extra,
    }
    original = copy.deepcopy(payload)
    rewritten = installer.rewrite(payload, f"homelab-{kind}")
    assert str(ROOT) not in plistlib.dumps(rewritten).decode()
    assert rewritten["ProgramArguments"] == [
        "/fixed/python3",
        str(installer.ROOT / f"homelab-{kind}"),
        *arguments,
    ]
    assert payload == original
    for key, value in extra.items():
        assert rewritten[key] == (str(installer.ROOT) if key == "WorkingDirectory" else value)


def test_future_profile_job_writer_uses_install(installer, monkeypatch):
    profile = module("installed_profile_writer", "homelab-profile")
    monkeypatch.setattr(profile, "AGENTS", installer.AGENTS)
    monkeypatch.setattr(profile, "installed_command", installer.installed_command)
    monkeypatch.setattr(profile, "registered", lambda label: False)
    monkeypatch.setattr(profile, "run", lambda *args: "")
    profile.install_agent("org.homelab.profile", ["tick"], StartInterval=30)
    profile.install_agent(
        "org.homelab.profile.job.example",
        ["job-worker", "example"],
        StartCalendarInterval={"Hour": 4},
    )
    for path in installer.AGENTS.glob("*.plist"):
        assert str(ROOT) not in path.read_text()
        assert plistlib.loads(path.read_bytes())["ProgramArguments"][:2] == [
            "/fixed/python3",
            str(installer.ROOT / "homelab-profile"),
        ]


def test_refresh_restarts_only_changed_agents_and_retains_jobs(installer, monkeypatch):
    for kind, args in [
        ("router", ["serve"]),
        ("listener", ["serve"]),
        ("profile", ["tick"]),
        ("profile.job.dictation-recheck", ["job-worker", "dictation-recheck"]),
    ]:
        label = "org.homelab." + kind
        command = "homelab-" + kind.split(".")[0]
        payload = {
            "Label": label,
            "ProgramArguments": [*installer.installed_command(command), *args],
            "StartCalendarInterval": {"Hour": 4},
        }
        (installer.AGENTS / f"{label}.plist").write_bytes(plistlib.dumps(payload))
    jobs = installer.ROOT.parent / "jobs.json"
    jobs.write_text('{"dictation-recheck":{"command":["/other/repo/check"]}}')
    original = jobs.read_bytes()
    calls = []
    unloaded = set()

    def run(args, **kwargs):
        calls.append(args)
        if args[1] == "bootout":
            unloaded.add(args[2])
        return subprocess.CompletedProcess(args, int(args[1] == "print" and args[2] in unloaded))

    monkeypatch.setattr(installer.subprocess, "run", run)
    installer.refresh_agents({"homelab-profile"})
    assert [args[-1] for args in calls if len(args) > 1 and args[1] == "bootout"] == [
        f"gui/{installer.os.getuid()}/org.homelab.profile.job.dictation-recheck",
        f"gui/{installer.os.getuid()}/org.homelab.profile",
    ]
    assert jobs.read_bytes() == original
    assert len(list(installer.AGENTS.glob("*.plist"))) == 4
    calls.clear()
    installer.refresh_agents(set())
    assert calls == []


def test_snapshot_contains_all_runtime_dependencies(installer):
    files = installer.contents("/fixed/python3")
    assert files["homelab-profile"].startswith(b"#!/fixed/python3\n")
    for name in (
        "homelab-copy",
        "homelab-vision",
        "homelab-router",
        "router-models.ini.in",
        "installed_tools.py",
    ):
        assert name in files
    assert "MenuBar/Package.swift" in files
    assert all(".build" not in name for name in files)
    shell = files["install-menu-bar.sh"].decode()
    assert ".venv" not in shell
    assert 'HomeLabProfileCommand "$MAC_DIR/homelab-profile"' in shell
    assert 'exec "$INSTALLED/install-menu-bar.sh"' in shell


def test_install_snapshot_noop_update_and_branch_independence(installer, monkeypatch, tmp_path):
    monkeypatch.setattr(Path, "home", lambda: tmp_path / "home")
    monkeypatch.setattr(installer, "revision", lambda: "commit-one")
    monkeypatch.setattr(installer, "interpreter", lambda: "/fixed/python3")
    files = installer.contents("/fixed/python3")
    monkeypatch.setattr(installer, "contents", lambda python: files.copy())
    calls = []
    unloaded = set()

    def run(args, **kwargs):
        calls.append(args)
        if args[-1] == "queue-install":
            (installer.AGENTS / "org.homelab.profile.queue.plist").write_bytes(
                plistlib.dumps(
                    {
                        "Label": "org.homelab.profile.queue",
                        "ProgramArguments": [
                            *installer.installed_command("homelab-profile"),
                            "queue-worker",
                        ],
                        "KeepAlive": True,
                    }
                )
            )
        if len(args) > 1 and args[1] == "bootout":
            unloaded.add(args[2])
        if len(args) > 1 and args[1] == "bootstrap":
            unloaded.discard(f"{args[2]}/{Path(args[3]).stem}")
        if args == [str(installer.ROOT / "install-menu-bar.sh")]:
            info = Path.home() / "Applications/HomeLab.app/Contents/Info.plist"
            info.parent.mkdir(parents=True, exist_ok=True)
            info.write_bytes(
                plistlib.dumps(
                    {
                        "HomeLabPython": "/fixed/python3",
                        "HomeLabProfileCommand": str(installer.ROOT / "homelab-profile"),
                    }
                )
            )
        absent = len(args) > 1 and args[1] == "print" and args[2] in unloaded
        return subprocess.CompletedProcess(args, int(absent), b"state = waiting")

    monkeypatch.setattr(installer.subprocess, "run", run)
    for kind in ("router", "listener", "profile"):
        label = f"org.homelab.{kind}"
        (installer.AGENTS / f"{label}.plist").write_bytes(
            plistlib.dumps(
                {"Label": label, "ProgramArguments": [str(ROOT / f"mac/homelab-{kind}"), "serve"]}
            )
        )
    installer.install()
    manifest = json.loads((installer.ROOT / "install.json").read_text())
    assert manifest["commit"] == "commit-one"
    assert not manifest["pending_changes"] and not manifest["pending_agents"]
    assert installer.ROOT.stat().st_mode & 0o777 == 0o700
    assert (installer.ROOT / "install.json").stat().st_mode & 0o777 == 0o600
    assert len([args for args in calls if len(args) > 1 and args[1] == "bootout"]) == 3
    calls.clear()
    # A documentation-only revision changes the stamp without restarting code.
    monkeypatch.setattr(installer, "revision", lambda: "commit-two")
    installer.install()
    assert calls == []
    assert json.loads((installer.ROOT / "install.json").read_text())["commit"] == "commit-two"
    # An arbitrary checkout change cannot alter the installed bytes or rendered paths.
    before = (installer.ROOT / "homelab-profile").read_bytes()
    monkeypatch.setattr(installer, "SOURCE", tmp_path / "another-branch/mac")
    assert (installer.ROOT / "homelab-profile").read_bytes() == before
    for path in installer.AGENTS.glob("*.plist"):
        assert str(ROOT) not in path.read_text()
    # A profile-only update refreshes only the profile agent.
    files["homelab-profile"] += b"\n# changed\n"
    installer.install()
    assert [args[-1] for args in calls if len(args) > 1 and args[1] == "bootout"] == [
        f"gui/{installer.os.getuid()}/org.homelab.profile",
        f"gui/{installer.os.getuid()}/org.homelab.profile.queue",
    ]


def test_failed_bootstrap_remains_pending_for_retry(installer, monkeypatch):
    label = "org.homelab.profile"
    path = installer.AGENTS / f"{label}.plist"
    path.write_bytes(
        plistlib.dumps(
            {
                "Label": label,
                "ProgramArguments": [*installer.installed_command("homelab-profile"), "tick"],
            }
        )
    )
    calls = []
    unloaded = False

    def fail(args, **kwargs):
        nonlocal unloaded
        calls.append(args)
        if args[1] == "bootout":
            unloaded = True
        if args[1] == "bootstrap":
            raise subprocess.CalledProcessError(1, args)
        return subprocess.CompletedProcess(
            args, int(args[1] == "print" and unloaded), b"state = waiting"
        )

    monkeypatch.setattr(installer.subprocess, "run", fail)
    with pytest.raises(subprocess.CalledProcessError):
        installer.refresh_agents({"homelab-profile"})
    assert json.loads((installer.ROOT / "install.json").read_text())["pending_agents"] == [label]
    calls.clear()

    def retry(args, **kwargs):
        calls.append(args)
        return subprocess.CompletedProcess(args, 1 if args[1] == "print" else 0)

    monkeypatch.setattr(installer.subprocess, "run", retry)
    installer.refresh_agents(set())
    assert [args[1] for args in calls] == ["print", "bootstrap"]
    assert json.loads((installer.ROOT / "install.json").read_text())["pending_agents"] == []


def test_active_job_is_never_unloaded(installer, monkeypatch):
    path = installer.AGENTS / "org.homelab.profile.job.dictation-recheck.plist"
    path.write_bytes(
        plistlib.dumps(
            {
                "Label": path.stem,
                "ProgramArguments": [
                    *installer.installed_command("homelab-profile"),
                    "job-worker",
                    "dictation-recheck",
                ],
            }
        )
    )
    original = path.read_bytes()
    calls = []

    def run(args, **kwargs):
        calls.append(args)
        return subprocess.CompletedProcess(args, 0, b"state = running")

    monkeypatch.setattr(installer.subprocess, "run", run)
    with pytest.raises(ValueError, match="wait for completion"):
        installer.refresh_agents({"homelab-profile"})
    assert [args[1] for args in calls] == ["print"]
    assert path.read_bytes() == original


@pytest.fixture
def fake_clock(installer, monkeypatch):
    clock = {"now": 0.0, "sleeps": []}

    def sleep(seconds):
        clock["now"] += seconds
        clock["sleeps"].append(seconds)

    monkeypatch.setattr(installer.time, "monotonic", lambda: clock["now"])
    monkeypatch.setattr(installer.time, "sleep", sleep)
    return clock


def test_refresh_waits_for_removal_then_retries_bootstrap(installer, monkeypatch, fake_clock):
    label = "org.homelab.listener"
    path = installer.AGENTS / f"{label}.plist"
    original = plistlib.dumps(
        {"Label": label, "ProgramArguments": [str(ROOT / "mac/homelab-listener"), "serve"]}
    )
    path.write_bytes(original)
    prints = iter([0, 0, 0, 1])
    bootstraps = 0
    calls = []

    def run(args, **kwargs):
        nonlocal bootstraps
        operation = args[1]
        calls.append(operation)
        if operation == "print":
            # Do not replace the plist or load it while launchd still sees the old service.
            assert path.read_bytes() == original
            return subprocess.CompletedProcess(args, next(prints))
        if operation == "bootstrap":
            assert plistlib.loads(path.read_bytes())["ProgramArguments"][:2] == [
                "/fixed/python3",
                str(installer.ROOT / "homelab-listener"),
            ]
            bootstraps += 1
            if bootstraps < 3:
                raise subprocess.CalledProcessError(5, args)
        return subprocess.CompletedProcess(args, 0)

    monkeypatch.setattr(installer.subprocess, "run", run)
    installer.refresh_agents({"homelab-listener"})
    assert calls == [
        "print",
        "bootout",
        "print",
        "print",
        "print",
        "bootstrap",
        "bootstrap",
        "bootstrap",
    ]
    assert fake_clock["sleeps"] == [installer.POLL_SECONDS] * 4
    assert json.loads((installer.ROOT / "install.json").read_text())["pending_agents"] == []


def test_bootout_timeout_keeps_agent_pending_without_bootstrap(installer, monkeypatch, fake_clock):
    monkeypatch.setattr(installer, "BOOTOUT_TIMEOUT", 0.4)
    label = "org.homelab.listener"
    path = installer.AGENTS / f"{label}.plist"
    original = plistlib.dumps(
        {"Label": label, "ProgramArguments": [str(ROOT / "mac/homelab-listener"), "serve"]}
    )
    path.write_bytes(original)
    calls = []

    def run(args, **kwargs):
        calls.append(args[1])
        return subprocess.CompletedProcess(args, 0)

    monkeypatch.setattr(installer.subprocess, "run", run)
    with pytest.raises(TimeoutError, match="has not unloaded"):
        installer.refresh_agents({"homelab-listener"})
    assert "bootstrap" not in calls
    assert path.read_bytes() == original
    assert fake_clock["now"] == installer.BOOTOUT_TIMEOUT
    assert json.loads((installer.ROOT / "install.json").read_text())["pending_agents"] == [label]


@pytest.mark.parametrize("code", [1, 5])
def test_bootstrap_retries_only_exit_five_with_a_deadline(installer, monkeypatch, fake_clock, code):
    monkeypatch.setattr(installer, "BOOTSTRAP_RETRY_SECONDS", 0.4)
    calls = []

    def run(args, **kwargs):
        calls.append(args)
        raise subprocess.CalledProcessError(code, args)

    monkeypatch.setattr(installer.subprocess, "run", run)
    with pytest.raises(subprocess.CalledProcessError) as failure:
        installer.bootstrap("gui/501", installer.AGENTS / "listener.plist")
    assert failure.value.returncode == code
    assert len(calls) == (3 if code == 5 else 1)
    assert fake_clock["now"] == (installer.BOOTSTRAP_RETRY_SECONDS if code == 5 else 0)


def test_install_converges_after_manual_listener_recovery(
    installer, monkeypatch, tmp_path, fake_clock
):
    monkeypatch.setattr(Path, "home", lambda: tmp_path / "home")
    monkeypatch.setattr(installer, "revision", lambda: "corrected-main")
    monkeypatch.setattr(installer, "interpreter", lambda: "/fixed/python3")
    files = installer.contents("/fixed/python3")
    monkeypatch.setattr(installer, "contents", lambda python: files.copy())
    for name, content in files.items():
        target = installer.ROOT / name
        target.parent.mkdir(parents=True, exist_ok=True)
        target.write_bytes(content)
    installer.manifest_update(
        commit="7696401",
        files=sorted(files),
        pending_changes=sorted(files),
        pending_agents=["org.homelab.listener"],
    )
    # Listener was restored by hand from installed code; the rest still use the checkout.
    for kind, args in [
        ("listener", ["serve"]),
        ("router", ["serve"]),
        ("profile", ["tick"]),
        ("profile.job.dictation-recheck", ["job-worker", "dictation-recheck"]),
    ]:
        label = "org.homelab." + kind
        command = "homelab-" + kind.split(".")[0]
        prefix = (
            installer.installed_command(command)
            if kind == "listener"
            else [str(ROOT / ".venv/bin/python3"), str(ROOT / "mac" / command)]
        )
        payload = {"Label": label, "ProgramArguments": [*prefix, *args]}
        if ".job." in label:
            payload["StartCalendarInterval"] = {"Hour": 4, "Minute": 0}
        (installer.AGENTS / f"{label}.plist").write_bytes(plistlib.dumps(payload))
    jobs = Path.home() / ".local/state/homelab-profile/jobs.json"
    jobs.parent.mkdir(parents=True)
    original_jobs = b'{"dictation-recheck":{"command":["/other/repo/check"],"pending":true}}'
    jobs.write_bytes(original_jobs)
    info = Path.home() / "Applications/HomeLab.app/Contents/Info.plist"
    info.parent.mkdir(parents=True)
    info.write_bytes(plistlib.dumps({"HomeLabProfileCommand": str(ROOT / "mac/homelab-profile")}))
    loaded = {
        f"gui/{installer.os.getuid()}/{path.stem}" for path in installer.AGENTS.glob("*.plist")
    }
    calls = []
    listener_bootstraps = 0

    def run(args, **kwargs):
        nonlocal listener_bootstraps
        calls.append(args)
        if args[-1] == "queue-install":
            (installer.AGENTS / "org.homelab.profile.queue.plist").write_bytes(
                plistlib.dumps(
                    {
                        "Label": "org.homelab.profile.queue",
                        "ProgramArguments": [
                            *installer.installed_command("homelab-profile"),
                            "queue-worker",
                        ],
                        "KeepAlive": True,
                    }
                )
            )
        if args == [str(installer.ROOT / "install-menu-bar.sh")]:
            info.write_bytes(
                plistlib.dumps(
                    {
                        "HomeLabPython": "/fixed/python3",
                        "HomeLabProfileCommand": str(installer.ROOT / "homelab-profile"),
                    }
                )
            )
        elif args[1] == "print":
            return subprocess.CompletedProcess(
                args, 0 if args[2] in loaded else 1, b"state = waiting"
            )
        elif args[1] == "bootout":
            loaded.remove(args[2])
        elif args[1] == "bootstrap":
            if Path(args[3]).stem == "org.homelab.listener":
                listener_bootstraps += 1
                if listener_bootstraps == 1:
                    raise subprocess.CalledProcessError(5, args)
            loaded.add(f"{args[2]}/{Path(args[3]).stem}")
        return subprocess.CompletedProcess(args, 0)

    monkeypatch.setattr(installer.subprocess, "run", run)
    installer.install()
    manifest = json.loads((installer.ROOT / "install.json").read_text())
    assert manifest["commit"] == "corrected-main"
    assert manifest["pending_agents"] == manifest["pending_changes"] == []
    assert len(loaded) == 4
    assert listener_bootstraps == 2
    for path in installer.AGENTS.glob("*.plist"):
        assert str(ROOT) not in path.read_text()
    job = plistlib.loads(
        (installer.AGENTS / "org.homelab.profile.job.dictation-recheck.plist").read_bytes()
    )
    assert job["StartCalendarInterval"] == {"Hour": 4, "Minute": 0}
    assert job["ProgramArguments"][-2:] == ["job-worker", "dictation-recheck"]
    assert jobs.read_bytes() == original_jobs
    assert plistlib.loads(info.read_bytes())["HomeLabProfileCommand"] == str(
        installer.ROOT / "homelab-profile"
    )
    calls.clear()
    installer.install()
    assert calls == []
