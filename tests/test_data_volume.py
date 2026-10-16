"""Exercise the real shell functions with stubbed device/system commands only."""

import json
import os
import subprocess
import tomllib
from pathlib import Path

import pytest
import yaml

ROOT = Path(__file__).resolve().parents[1]
SCRIPT = ROOT / "node/usr/local/sbin/data-volume.sh"

# Source the production functions without dispatch, then stub privileged commands.
# The bracket wrapper supplies block-device presence without creating devices.
STUBS = r"""
log() { printf '%s\n' "$*" >> "$TRACE"; }
status() { :; }
id() { printf '0\n'; }
[() {
    set -- "${@:1:$#-1}"
    if test "$1" = "!"; then
        shift
        ! [ "$@" ]
    elif test "$#" -eq 2 && test "$1" = -b; then
        builtin test -e "$STATE/${2##*/}"
    elif test "$#" -eq 2 && test "$1" = -f && test "$2" = "$KEY"; then
        return 0
    else
        builtin test "$@"
    fi
}
stat() { printf '0:0:400:4096\n'; }
findmnt() {
    local mount="${!#}"
    test -e "$STATE/${mount##*/}.mounted" || return 1
    if test "$2" = -o; then
        if test "$mount" = /srv/homelab; then printf '/dev/mapper/homelab-data\n'
        else printf '/dev/mapper/homelab-bulk\n'; fi
    fi
}
readlink() { printf '%s\n' "$2"; }
cryptsetup() {
    log "cryptsetup $*"
    test "${FAIL:-}" != "cryptsetup $1 ${!#}" || return 1
    if test "$1" = open; then touch "$STATE/${!#}"
    else rm "$STATE/$2"; fi
}
mount() { log "mount $*"; touch "$STATE/${1##*/}.mounted"; }
umount() {
    log "umount $*"
    test "${FAIL:-}" != "umount $1" || return 1
    rm "$STATE/${1##*/}.mounted"
}
systemctl() {
    log "systemctl $*"
    test "${FAIL:-}" != "systemctl $*"
}
"""


def run_volume(tmp_path, verb, present=(), fail=""):
    for name in ["data", "wwn-0x500a07511802f8bd-part1", *present]:
        (tmp_path / name).touch()
    trace = tmp_path / "trace"
    functions = SCRIPT.read_text().split('case "${1:-status}" in')[0]
    result = subprocess.run(
        ["bash", "-c", functions + STUBS + f"\n{verb}\n"],
        env={**os.environ, "STATE": str(tmp_path), "TRACE": str(trace), "FAIL": fail},
        text=True,
        capture_output=True,
    )
    return result, trace.read_text().splitlines() if trace.exists() else []


def test_unlock_orders_both_volumes_before_runtimes_and_clients(tmp_path):
    result, calls = run_volume(tmp_path, "unlock")
    assert result.returncode == 0, result.stderr
    assert calls == [
        "cryptsetup open --allow-discards /dev/ubuntu-vg/data homelab-data",
        "mount /srv/homelab",
        "cryptsetup open --key-file /srv/homelab/.keys/homelab-bulk.key "
        "/dev/disk/by-id/wwn-0x500a07511802f8bd-part1 homelab-bulk",
        "mount /srv/homelab-bulk",
        "systemctl start containerd.service",
        "systemctl start docker.service",
        "systemctl is-active --quiet containerd.service docker.service",
        "systemctl start homelab-data.target",
    ]


def test_failed_bulk_open_never_starts_docker_or_target_and_can_retry(tmp_path):
    result, calls = run_volume(tmp_path, "unlock", fail="cryptsetup open homelab-bulk")
    assert result.returncode != 0
    assert not any(call.startswith("systemctl") for call in calls)
    (tmp_path / "trace").unlink()
    result, calls = run_volume(tmp_path, "unlock")
    assert result.returncode == 0, result.stderr
    assert not any("/dev/ubuntu-vg/data" in call for call in calls)
    assert "mount /srv/homelab" not in calls
    assert calls[-1] == "systemctl start homelab-data.target"


def test_unlock_can_restart_from_both_already_mounted(tmp_path):
    result, calls = run_volume(
        tmp_path,
        "unlock",
        ("homelab-data", "homelab.mounted", "homelab-bulk", "homelab-bulk.mounted"),
    )
    assert result.returncode == 0, result.stderr
    assert all(call.startswith("systemctl") for call in calls)


def test_lock_reverses_order(tmp_path):
    result, calls = run_volume(
        tmp_path,
        "lock",
        ("homelab-data", "homelab.mounted", "homelab-bulk", "homelab-bulk.mounted"),
    )
    assert result.returncode == 0, result.stderr
    assert calls == [
        "systemctl stop homelab-data.target",
        "systemctl stop docker.service docker.socket",
        "systemctl stop containerd.service",
        "umount /srv/homelab-bulk",
        "cryptsetup close homelab-bulk",
        "umount /srv/homelab",
        "cryptsetup close homelab-data",
    ]


@pytest.mark.parametrize(
    "failure",
    ["systemctl stop homelab-data.target", "umount /srv/homelab-bulk"],
)
def test_lock_failure_never_closes_a_busy_volume_or_main(tmp_path, failure):
    result, calls = run_volume(
        tmp_path,
        "lock",
        ("homelab-data", "homelab.mounted", "homelab-bulk", "homelab-bulk.mounted"),
        fail=failure,
    )
    assert result.returncode != 0
    assert calls[-1] == failure
    assert (tmp_path / "homelab.mounted").exists()
    assert not any(call.startswith("cryptsetup close") for call in calls)


def test_both_roots_and_default_builder_gc_are_configured():
    docker = json.loads((ROOT / "node/etc/docker/daemon.json").read_text())
    containerd = tomllib.loads((ROOT / "node/etc/containerd/config.toml").read_text())
    assert docker["data-root"] == "/srv/homelab-bulk/docker"
    assert docker["builder"]["gc"] == {"enabled": True, "defaultKeepStorage": "50GB"}
    assert containerd["root"] == "/srv/homelab-bulk/containerd"
    assert containerd["disabled_plugins"] == ["cri"]
    assert "state" not in containerd  # /run/containerd remains ephemeral.
    compose = yaml.safe_load((ROOT / "compose.yaml").read_text())
    for service in ["llama-embed", "llama-embed-large"]:
        assert compose["services"][service]["volumes"][0].startswith(
            "${HOMELAB_MODELS_PATH:-/srv/homelab-bulk/models}"
        )


@pytest.mark.parametrize("unit", ["docker.service", "docker.socket", "containerd.service"])
def test_runtime_guards_both_mounts_without_pulling_in_unlock(unit):
    text = (ROOT / f"node/etc/systemd/system/{unit}.d/data-volume.conf").read_text()
    assert "ConditionPathIsMountPoint=/srv/homelab\n" in text
    assert "ConditionPathIsMountPoint=/srv/homelab-bulk\n" in text
    assert "PartOf=homelab-data.target" in text
    assert "After=srv-homelab.mount srv-homelab\\x2dbulk.mount" in text
    assert "Requires=" not in text
    assert "RequiresMountsFor=" not in text


def test_crypto_and_mount_entries_do_not_start_at_boot():
    for file in ["crypttab", "fstab"]:
        rows = [
            row.split()
            for row in (ROOT / f"node/etc/{file}").read_text().splitlines()
            if row and not row.startswith("#")
        ]
        assert len(rows) == 2
        assert all("noauto" in row[3].split(",") for row in rows)


def test_systemd_validates_runtime_order_with_vendor_units(tmp_path):
    import shutil

    if not shutil.which("systemd-analyze"):
        pytest.skip("systemd validator runs on Linux CI")
    units = ROOT / "node/etc/systemd/system"
    shutil.copy2(units / "homelab-data.target", tmp_path)
    for unit in ["docker.service", "docker.socket", "containerd.service"]:
        shutil.copytree(units / f"{unit}.d", tmp_path / f"{unit}.d")
    # Stand in for fstab-generator output, without editing the runner's fstab.
    for name, mount in [
        ("srv-homelab.mount", "/srv/homelab"),
        ("srv-homelab\\x2dbulk.mount", "/srv/homelab-bulk"),
    ]:
        (tmp_path / name).write_text(f"[Mount]\nWhat=/dev/null\nWhere={mount}\nType=ext4\n")
    result = subprocess.run(
        [
            "systemd-analyze",
            "verify",
            "--man=no",
            "homelab-data.target",
            "docker.service",
            "docker.socket",
            "containerd.service",
        ],
        env={
            **os.environ,
            "SYSTEMD_UNIT_PATH": f"{tmp_path}:/etc/systemd/system:/usr/lib/systemd/system:"
            "/lib/systemd/system",
        },
        capture_output=True,
        text=True,
    )
    assert result.returncode == 0, result.stdout + result.stderr


def test_docker_start_failure_never_releases_clients(tmp_path):
    result, calls = run_volume(tmp_path, "unlock", fail="systemctl start docker.service")
    assert result.returncode != 0
    assert calls[-1] == "systemctl start docker.service"
    assert "systemctl start homelab-data.target" not in calls


def test_unexpected_mount_source_refuses_docker(tmp_path):
    (tmp_path / "homelab-data").touch()
    script = SCRIPT.read_text().split('case "${1:-status}" in')[0]
    script += STUBS + "\nfindmnt() { printf '/dev/wrong\\n'; }\nunlock\n"
    result = subprocess.run(
        ["bash", "-c", script],
        env={**os.environ, "STATE": str(tmp_path), "TRACE": str(tmp_path / "trace")},
        capture_output=True,
        text=True,
    )
    # Supply main as already open, so the check is on the mounted filesystem.
    assert result.returncode != 0
    assert "unexpected source" in result.stderr
    assert not (tmp_path / "trace").exists()
