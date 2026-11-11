"""The imported fence's argument and network contracts, without Docker or credentials."""

import importlib.machinery
import importlib.util
from ipaddress import ip_network
from pathlib import Path
from types import SimpleNamespace

import pytest
import yaml

ROOT = Path(__file__).resolve().parents[1]
loader = importlib.machinery.SourceFileLoader(
    "homelab_fence", str(ROOT / "node/usr/local/sbin/homelab-fence")
)
spec = importlib.util.spec_from_loader(loader.name, loader)
fence = importlib.util.module_from_spec(spec)
loader.exec_module(fence)


@pytest.fixture
def folders(tmp_path, monkeypatch):
    for attribute in ("WORK_ROOT", "RUN_ROOT", "LOGIN_ROOT"):
        root = tmp_path / attribute
        root.mkdir()
        monkeypatch.setattr(fence, attribute, root)
    repo, run = fence.WORK_ROOT / "repo", fence.RUN_ROOT / "turn"
    repo.mkdir()
    run.mkdir()
    credentials = {fence.LOGIN_ROOT / tool for tool in fence.TOOLS}
    for path in credentials:
        path.mkdir(mode=0o700)
    metadata = SimpleNamespace(st_mode=0o40700, st_uid=21000, st_gid=21000)
    original_stat = Path.stat

    def synthetic_login_stat(path, *args, **kwargs):
        if path in credentials and kwargs.get("follow_symlinks", True):
            return metadata
        return original_stat(path, *args, **kwargs)

    monkeypatch.setattr(Path, "stat", synthetic_login_stat)
    return repo, run, metadata


def invocation(folders, tool="codex", command=("codex", "exec", "--", "ticket"), **kwargs):
    repo, run, _ = folders
    return fence.docker_arguments(tool, repo=repo, run=run, command=command, **kwargs)


def values(args, option):
    return [args[index + 1] for index, value in enumerate(args[:-1]) if value == option]


def test_sealed_options():
    assert fence.sealed_arguments("homelab-fence") == [
        "docker",
        "run",
        "--rm",
        "--pull",
        "never",
        "--init",
        "--user",
        "21000:21000",
        "--cap-drop",
        "ALL",
        "--security-opt",
        "no-new-privileges",
        "--read-only",
        "--tmpfs",
        "/tmp:rw,exec,nosuid,nodev,size=512m,mode=1777",
        "--tmpfs",
        "/home/agent:rw,exec,nosuid,nodev,size=512m,uid=21000,gid=21000,mode=0700",
        "--memory",
        "4g",
        "--memory-swap",
        "4g",
        "--cpus",
        "2",
        "--pids-limit",
        "256",
        "--log-driver",
        "none",
        "--network",
        "homelab-fence",
    ]


def test_codex_mounts_environment_and_command(folders, monkeypatch):
    monkeypatch.setenv("FENCE_TEST_SECRET", "must not be forwarded")
    args = invocation(folders)
    repo, run, _ = folders
    assert values(args, "--mount") == [
        f"type=bind,src={fence.LOGIN_ROOT / 'codex'},dst=/login",
        f"type=bind,src={repo},dst=/work",
        f"type=bind,src={run},dst=/run/factory",
    ]
    assert values(args, "--env") == [
        "HOME=/home/agent",
        "XDG_CONFIG_HOME=/login/xdg",
        "HTTP_PROXY=http://cli-proxy:8888",
        "HTTPS_PROXY=http://cli-proxy:8888",
        "http_proxy=http://cli-proxy:8888",
        "https_proxy=http://cli-proxy:8888",
        "NO_PROXY=",
        "no_proxy=",
        "CODEX_HOME=/login",
    ]
    assert args[args.index("homelab-fence:1") :] == [
        "homelab-fence:1",
        "codex",
        "--dangerously-bypass-approvals-and-sandbox",
        "-c",
        'cli_auth_credentials_store="file"',
        "exec",
        "--",
        "ticket",
    ]


@pytest.mark.parametrize(
    "field,value", [("st_mode", 0o40750), ("st_mode", 0o40777), ("st_uid", 0), ("st_gid", 0)]
)
def test_login_metadata_refused(folders, field, value):
    setattr(folders[2], field, value)
    with pytest.raises(ValueError, match="login directory"):
        invocation(folders)


def test_missing_or_linked_login_refused(folders):
    path = fence.LOGIN_ROOT / "codex"
    path.rmdir()
    with pytest.raises(FileNotFoundError):
        invocation(folders)
    path.symlink_to(fence.LOGIN_ROOT / "claude", target_is_directory=True)
    with pytest.raises(ValueError, match="symlink"):
        invocation(folders)


@pytest.mark.parametrize(
    "tool,command",
    [
        ("claude", ["claude", "auth", "login"]),
        ("codex", ["codex", "-c", 'cli_auth_credentials_store="file"', "login", "--device-auth"]),
    ],
)
def test_login_arguments(folders, tool, command):
    args = fence.docker_arguments(tool, login=True)
    assert args[-len(command) :] == command
    assert "--interactive" in args and "--tty" in args
    assert values(args, "--workdir") == ["/home/agent"]
    assert len(values(args, "--mount")) == 1
    with pytest.raises(ValueError, match="login takes only"):
        fence.docker_arguments(tool, login=True, repo=folders[0])
    with pytest.raises(ValueError, match="login takes only"):
        fence.docker_arguments(tool, login=True, command=[tool])


def test_login_requires_owner_terminal(monkeypatch, capsys):
    monkeypatch.setattr(fence.sys.stdin, "isatty", lambda: False)
    monkeypatch.setattr(fence.subprocess, "run", lambda *a, **k: pytest.fail("invoked Docker"))
    assert fence.main(["login", "codex"]) == 2
    assert capsys.readouterr().err.startswith("homelab-fence: refused;")


@pytest.mark.parametrize(
    "path", ["relative", "/tmp/../tmp", "/tmp/a,b", "/tmp/a\nb", "/tmp/a\rb", "/tmp/a\x00b"]
)
def test_ambiguous_paths_refused(path):
    with pytest.raises(ValueError, match="absolute, unambiguous"):
        fence.real_directory(path)


def test_paths_must_be_real_directories_below_root(tmp_path):
    directory = tmp_path / "directory"
    directory.mkdir()
    assert fence.within(directory, tmp_path) == directory
    link = tmp_path / "link"
    link.symlink_to(directory, target_is_directory=True)
    (directory / "child").mkdir()
    file = tmp_path / "file"
    file.touch()
    for path in (link, link / "child", file):
        with pytest.raises(ValueError, match="symlink or non-directory"):
            fence.real_directory(path)
    for path in (tmp_path, tmp_path.parent):
        with pytest.raises(ValueError, match="outside"):
            fence.within(path, tmp_path)


@pytest.mark.parametrize("which", ["repo", "run"])
def test_run_and_bundle_check_both_roots(folders, which):
    repo, run, _ = folders
    kwargs = {"repo": repo, "run": run}
    kwargs[which] = repo.parent if which == "repo" else run.parent
    with pytest.raises(ValueError, match="outside"):
        fence.docker_arguments("codex", command=["codex", "exec"], **kwargs)
    with pytest.raises(ValueError, match="outside"):
        fence.bundle_arguments(branch="slice/test", **kwargs)


@pytest.mark.parametrize(
    "branch",
    [
        "",
        "-option",
        ".hidden",
        "a//b",
        "a/./b",
        "a/../b",
        "a/.hidden",
        "a/b.",
        "a/b.lock",
        "a..b",
        "a b",
        "a\nb",
    ],
)
def test_bad_branch_refused(folders, branch):
    with pytest.raises(ValueError, match="invalid branch"):
        fence.bundle_arguments(repo=folders[0], run=folders[1], branch=branch)


@pytest.mark.parametrize("branch", ["main", "slice/feature-1", "release/v1.2_3"])
def test_bundle_is_offline_read_only_and_disables_git_execution(folders, branch):
    repo, run, _ = folders
    args = fence.bundle_arguments(repo=repo, run=run, branch=branch)
    assert args[: len(fence.sealed_arguments("none"))] == fence.sealed_arguments("none")
    assert values(args, "--mount") == [
        f"type=bind,src={repo},dst=/work,readonly",
        f"type=bind,src={run},dst=/run/factory",
    ]
    assert values(args, "--env") == [
        "HOME=/home/agent",
        "GIT_CONFIG_GLOBAL=/dev/null",
        "GIT_CONFIG_NOSYSTEM=1",
        "GIT_TERMINAL_PROMPT=0",
    ]
    assert args[args.index("homelab-fence:1") :] == [
        "homelab-fence:1",
        "git",
        "-c",
        "core.hooksPath=/dev/null",
        "-c",
        "core.fsmonitor=false",
        "-c",
        "core.sshCommand=/bin/false",
        "-c",
        "protocol.allow=never",
        "bundle",
        "create",
        "/run/factory/branch.bundle",
        "HEAD",
        f"refs/heads/{branch}",
    ]


@pytest.mark.parametrize(
    "tool,command",
    [
        ("other", ["other"]),
        ("codex", []),
        ("codex", ["sh", "-c", "id"]),
        ("codex", ["codex"]),
        ("codex", ["codex", "login"]),
        ("codex", ["codex", "debug", "models", "extra"]),
        ("claude", ["claude"]),
        ("claude", ["claude", "--", "-p"]),
    ],
)
def test_invalid_tool_commands(folders, tool, command):
    with pytest.raises(ValueError):
        invocation(folders, tool, command)


def test_codex_debug_models_is_preserved(folders):
    command = ["codex", "debug", "models"]
    args = invocation(folders, command=command)
    assert args[-4:] == ["homelab-fence:1", *command]


@pytest.mark.parametrize("option", ["-p", "--print"])
def test_claude_print_disables_cli_sandbox_and_updater(folders, option):
    command = ["claude", option, "--", "ticket"]
    args = invocation(folders, "claude", command)
    assert command == ["claude", option, "--", "ticket"]
    assert args[args.index("homelab-fence:1") :] == [
        "homelab-fence:1",
        "claude",
        "--dangerously-skip-permissions",
        "--settings",
        '{"sandbox":{"enabled":false}}',
        option,
        "--",
        "ticket",
    ]
    assert values(args, "--env")[-4:] == [
        "CLAUDE_CONFIG_DIR=/login",
        "DISABLE_AUTOUPDATER=1",
        "CLAUDE_CODE_DISABLE_NONESSENTIAL_TRAFFIC=1",
        "ENABLE_CLAUDEAI_MCP_SERVERS=false",
    ]


def test_read_only_exports_and_session_name(folders):
    repo = folders[0]
    export = repo / "repos/project"
    export.mkdir(parents=True)
    args = invocation(folders, read_only_paths=[export], container_name="factory-session-turn-7")
    assert values(args, "--mount")[-1] == f"type=bind,src={export},dst=/work/repos/project,readonly"
    assert values(args, "--name") == ["factory-session-turn-7"]
    for name in ("homelab-session-turn-7", "factory-session-turn--1", "factory-session-turn-7/x"):
        with pytest.raises(ValueError, match="session container name"):
            invocation(folders, container_name=name)
    (export / "nested").mkdir()
    for path in (repo, repo / "repos", export / "nested", folders[1]):
        with pytest.raises(ValueError):
            invocation(folders, read_only_paths=[path])


def test_fence_subnets_are_fixed_and_do_not_overlap_stack():
    config = yaml.safe_load((ROOT / "node/opt/homelab-fence/compose.yaml").read_text())
    assert config["name"] == "homelab-fence"
    networks = config["networks"]
    assert networks["cli"]["name"] == "homelab-fence"
    assert networks["cli"]["internal"] is True
    assert networks["egress"]["name"] == "homelab-fence-egress"
    assert networks["cli"]["ipam"]["config"] == [{"subnet": "172.29.0.0/16"}]
    assert networks["egress"]["ipam"]["config"] == [{"subnet": "172.30.0.0/16"}]
    stack = yaml.safe_load((ROOT / "compose.yaml").read_text())
    # Include the supplied running stack assignments as well as all declared subnets.
    stack_subnets = {
        "172.18.0.0/16",
        "172.19.0.0/16",
        "172.25.0.0/16",
        "172.26.0.0/16",
        "10.253.87.0/24",
    }
    stack_subnets.update(
        entry["subnet"]
        for network in stack["networks"].values()
        for entry in network.get("ipam", {}).get("config", [])
    )
    internal, egress = (
        ip_network(networks[key]["ipam"]["config"][0]["subnet"]) for key in ("cli", "egress")
    )
    assert not internal.overlaps(egress)
    for network in (internal, egress):
        assert all(not network.overlaps(ip_network(subnet)) for subnet in stack_subnets)
