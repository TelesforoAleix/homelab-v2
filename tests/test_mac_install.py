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

    def run(args, **kwargs):
        calls.append(args)
        return subprocess.CompletedProcess(args, 0)

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

    def run(args, **kwargs):
        calls.append(args)
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
        return subprocess.CompletedProcess(args, 0, b"state = waiting")

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
        f"gui/{installer.os.getuid()}/org.homelab.profile"
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

    def fail(args, **kwargs):
        calls.append(args)
        if args[1] == "bootstrap":
            raise subprocess.CalledProcessError(1, args)
        return subprocess.CompletedProcess(args, 0, b"state = waiting")

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
