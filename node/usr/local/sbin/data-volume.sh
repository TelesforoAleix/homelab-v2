#!/usr/bin/env bash
# Owner's passphrase opens data; its root-only key opens bulk. No boot daemon.
# Retry unlock after a partial failure; lock refuses to proceed past a failed stop.
set -euo pipefail

readonly NAME="homelab-data"
readonly SOURCE="/dev/ubuntu-vg/data"
readonly MAPPER="/dev/mapper/${NAME}"
readonly MOUNT="/srv/homelab"
readonly BULK_NAME="homelab-bulk"
readonly BULK_SOURCE="/dev/disk/by-id/wwn-0x500a07511802f8bd-part1"
readonly BULK_MAPPER="/dev/mapper/${BULK_NAME}"
readonly BULK_MOUNT="/srv/homelab-bulk"
readonly KEY="${MOUNT}/.keys/homelab-bulk.key"
readonly TARGET="homelab-data.target"

die() { printf 'data-volume: %s\n' "$*" >&2; exit 1; }
need_root() { [ "$(id -u)" -eq 0 ] || die "'$1' needs root. Re-run with sudo."; }
mounted() { findmnt -rn --mountpoint "$1" >/dev/null 2>&1; }

check_mount() {
    local source
    source="$(findmnt -rn -o SOURCE --mountpoint "$1")"
    [ "$(readlink -f "$source")" = "$(readlink -f "$2")" ] \
        || die "$1 is mounted from an unexpected source; refusing to continue"
}

status_volume() {
    printf '=== %s ===\n' "$1"
    if [ -b "$2" ]; then printf 'mapper:   present\n'
    else printf 'mapper:   absent -- LOCKED\n'; fi
    if mounted "$3"; then
        printf 'mount:    %s\n' "$(findmnt -rn -o SOURCE,FSTYPE,OPTIONS --mountpoint "$3")"
        printf 'space:    %s\n' "$(df -h --output=size,used,avail,pcent "$3" | tail -1)"
    else
        printf 'mount:    not mounted\n'
    fi
}

status() {
    status_volume "$NAME" "$MAPPER" "$MOUNT"
    status_volume "$BULK_NAME" "$BULK_MAPPER" "$BULK_MOUNT"
    local unit state
    for unit in "$TARGET" docker.service docker.socket containerd.service \
        $(systemctl show -p Wants --value "$TARGET" 2>/dev/null); do
        state="$(systemctl is-active "$unit" 2>/dev/null || true)"
        printf 'unit: %-42s %s\n' "$unit" "$state"
    done
}

unlock() {
    need_root unlock
    if [ ! -b "$MAPPER" ]; then
        [ -b "$SOURCE" ] || die "$SOURCE not found"
        printf 'Opening %s. The passphrase is not echoed.\n' "$NAME"
        cryptsetup open --allow-discards "$SOURCE" "$NAME"
    fi
    mounted "$MOUNT" || mount "$MOUNT"
    check_mount "$MOUNT" "$MAPPER"

    if [ ! -b "$BULK_MAPPER" ]; then
        [ -b "$BULK_SOURCE" ] || die "$BULK_SOURCE not found"
        if [ -L "$KEY" ] || [ ! -f "$KEY" ] \
            || [ "$(stat -c '%u:%g:%a:%s' "$KEY")" != "0:0:400:4096" ]; then
            die "bulk key must be a root:root 0400 regular file of 4096 bytes"
        fi
        cryptsetup open --key-file "$KEY" "$BULK_SOURCE" "$BULK_NAME"
    fi
    mounted "$BULK_MOUNT" || mount "$BULK_MOUNT"
    check_mount "$BULK_MOUNT" "$BULK_MAPPER"

    # Vendor Docker requires its socket and orders itself after containerd.
    systemctl start containerd.service
    systemctl start docker.service
    systemctl is-active --quiet containerd.service docker.service
    systemctl start "$TARGET"
    status
}

lock() {
    need_root lock
    # PartOf and After stop clients before Docker and containerd. Do not ignore
    # failures: a failed stop must not be followed by closing either filesystem.
    systemctl stop "$TARGET"
    systemctl stop docker.service docker.socket
    systemctl stop containerd.service
    if mounted "$BULK_MOUNT"; then
        check_mount "$BULK_MOUNT" "$BULK_MAPPER"
        umount "$BULK_MOUNT"
    fi
    if [ -b "$BULK_MAPPER" ]; then cryptsetup close "$BULK_NAME"; fi
    if mounted "$MOUNT"; then
        check_mount "$MOUNT" "$MAPPER"
        umount "$MOUNT"
    fi
    if [ -b "$MAPPER" ]; then cryptsetup close "$NAME"; fi
    status
}

case "${1:-status}" in
    unlock) unlock ;;
    lock)   lock ;;
    status) status ;;
    *)      die "usage: data-volume.sh [unlock|lock|status]" ;;
esac
