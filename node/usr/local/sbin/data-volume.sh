#!/usr/bin/env bash
#
# data-volume.sh -- unlock, lock and report the encrypted data volume.
#
# The owner unlocks over SSH after boot, with a passphrase. Three verbs give
# the owner and watchdog a way to inspect the volume without a daemon.
# cryptsetup open is the command whose wrong-passphrase refusal was tested;
# the equivalent systemd-cryptsetup unit is not needed here.
#
# status only reads and needs no privilege; unlock and lock require root.
# lock stops services, then unmounts, then closes the mapper. PartOf= stops
# volume users before unmounting, avoiding a busy mount or ongoing writes.
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
