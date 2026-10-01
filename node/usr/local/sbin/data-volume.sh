#!/usr/bin/env bash
#
# data-volume.sh -- unlock, lock and report the encrypted data volume (Phase 18.1, ADR-037).
#
# WHY THIS EXISTS
#
#   ADR-037 §3 unlocks the volume over SSH after boot, not from the initramfs and not from a TPM.
#   That makes unlocking a thing a person does, and a thing Phase 12's watchdog will later need to
#   ask about. Three verbs, one file, no daemon.
#
# WHY `cryptsetup open` RATHER THAN THE SYSTEMD UNIT
#
#   `systemctl start systemd-cryptsetup@homelab\x2ddata.service` is equivalent and would work. This
#   uses `cryptsetup open` because it is the command whose refusal on a wrong passphrase the owner
#   watched in step C4. The command you have seen refuse is the command you should trust.
#
# WHY `status` NEEDS NO PRIVILEGE
#
#   Asking whether the volume is unlocked must never require sudo: the owner asks it constantly, and
#   Phase 12's watchdog will ask it unattended, as a service account. Only `unlock` and `lock` are
#   privileged. `status` reads; it never opens, mounts or stops anything.
#
# ORDER MATTERS IN `lock`
#
#   Services, then mount, then key. Stopping the target first stops every unit that declares
#   `PartOf=homelab-data.target`, so nothing is still writing when the umount happens. Reversing it
#   gives a busy mount and a volume that will not close.
#
set -euo pipefail

readonly NAME="homelab-data"
readonly SOURCE="/dev/ubuntu-vg/data"
readonly MAPPER="/dev/mapper/${NAME}"
readonly MOUNT="/srv/homelab"
readonly TARGET="homelab-data.target"

die() { printf 'data-volume: %s\n' "$*" >&2; exit 1; }
need_root() { [ "$(id -u)" -eq 0 ] || die "'$1' needs root. Re-run with sudo."; }

status() {
    printf '=== %s ===\n' "$NAME"
    if [ -b "$MAPPER" ]; then printf 'mapper:   present  (%s)\n' "$MAPPER"
    else                      printf 'mapper:   absent   -- the volume is LOCKED\n'; fi

    if findmnt -n "$MOUNT" >/dev/null 2>&1; then
        printf 'mount:    %s\n' "$(findmnt -no SOURCE,FSTYPE,OPTIONS "$MOUNT")"
        printf 'space:    %s\n' "$(df -h --output=size,used,avail,pcent "$MOUNT" | tail -1)"
    else
        printf 'mount:    not mounted\n'
    fi

    printf 'target:   %s\n' "$(systemctl is-active "$TARGET" 2>/dev/null || true)"
    local unit state
    for unit in $(systemctl show -p Wants --value "$TARGET" 2>/dev/null); do
        state="$(systemctl is-active "$unit" 2>/dev/null || true)"
        printf '  unit:   %-42s %s\n' "$unit" "$state"
    done
}

unlock() {
    need_root unlock
    [ -b "$MAPPER" ] && die "already unlocked -- $MAPPER exists. Nothing to do."
    [ -b "$SOURCE" ] || die "$SOURCE not found. Is the LV there? Run: sudo lvs ubuntu-vg"

    printf 'Opening %s as %s. The passphrase is not echoed.\n' "$SOURCE" "$NAME"
    cryptsetup open --allow-discards "$SOURCE" "$NAME"   # --allow-discards: crypttab says `discard`
    printf 'opened.\n'

    mount "$MOUNT"
    printf 'mounted %s.\n' "$MOUNT"

    systemctl start "$TARGET"
    printf 'started %s.\n\n' "$TARGET"
    status
}

lock() {
    need_root lock
    findmnt -n "$MOUNT" >/dev/null 2>&1 || [ -b "$MAPPER" ] || die "already locked. Nothing to do."

    # Services, then mount, then key. See the header.
    systemctl stop "$TARGET" || true
    printf 'stopped %s and everything PartOf it.\n' "$TARGET"

    if findmnt -n "$MOUNT" >/dev/null 2>&1; then
        umount "$MOUNT" || die "umount failed -- something is still using $MOUNT. Try: sudo fuser -vm $MOUNT"
        printf 'unmounted %s.\n' "$MOUNT"
    fi

    if [ -b "$MAPPER" ]; then
        cryptsetup close "$NAME"
        printf 'closed %s -- the key is no longer in memory.\n\n' "$NAME"
    fi
    status
}

case "${1:-status}" in
    unlock) unlock ;;
    lock)   lock ;;
    status) status ;;
    *)      die "usage: data-volume.sh [unlock|lock|status]" ;;
esac
