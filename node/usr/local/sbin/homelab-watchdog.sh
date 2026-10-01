#!/usr/bin/env bash
#
# homelab-watchdog.sh -- one boot notice with downtime, prior stop cleanliness
# and whether the encrypted data volume needs unlocking.
#
# A passphrase-protected volume needs manual unlocking after a reboot.
# systemd's timer triggers the notice; no daemon samples a healthy node.
# PID 1's final "Shutting down." marker distinguishes clean shutdowns from
# power cuts. Only _PID=1 is matched: a user manager's session shutdown could
# otherwise misclassify a later power cut. Journal boot timestamps provide
# downtime in both branches.
#
# The node does not ship last -x or its wtmpdb replacement. The journal
# heuristic was checked against both a clean reboot and a power cut: only
# the clean stop carried the PID 1 marker.
#
# The unit excludes AF_UNIX to block the model helper socket at the kernel
# filter. systemctl also needs UNIX sockets, so findmnt alone probes the
# mount. The unlock procedure mounts before starting homelab-data.target;
# a mounted volume without the target starting is not a state it produces.
#
# No volume key material is read: /etc/crypttab contains no keyfile path,
# and findmnt is unprivileged. The calling unit owns retries. This script
# composes text and delegates sends to the shared homelab-notify.sh.
#
set -euo pipefail

NOTIFY="/usr/local/sbin/homelab-notify.sh"
CRYPTTAB="/etc/crypttab"
MOUNT="/srv/homelab"

die() { printf 'homelab-watchdog: %s\n' "$*" >&2; exit 1; }

# --- was the prior stop clean, and how long was the node down? -------
#
# Sets CLASS ("clean reboot" | "unplanned reboot") and DOWN_MIN (integer
# minutes, approximate -- the message says "~N minutes" for exactly that
# reason, never an exact figure).
#
# Takes the previous boot's journal offset as an optional parameter (default
# -1, i.e. the boot before this one) so the clean branch can be proven on a
# real past transition without rebooting. Production never passes one.
classify_boot() {
    local prev="${1:--1}" cur prev_last cur_first down_seconds
    cur=$(( prev + 1 ))

    # No previous boot in the journal at all: fail loudly. The unit landing in
    # `failed` and paging the owner via homelab-notify@watchdog.service with
    # this line in its journal is the honest outcome; inventing a class is not.
    journalctl -b "$prev" -n 1 -q --no-pager >/dev/null 2>&1 \
        || die "no journal for boot $prev; cannot classify the prior stop as clean or unplanned"

    # journalctl's own -g, not `| grep -q`: grep -q closing the pipe early
    # would SIGPIPE journalctl, and inside `if` that 141 reads as "no marker"
    # -- a clean reboot silently reported unplanned. Same trap as the
    # --list-boots note below. -g exits 1 when nothing matches.
    if journalctl -b "$prev" -q --no-pager _PID=1 -g 'Shutting down\.' >/dev/null; then
        CLASS="clean reboot"
    else
        CLASS="unplanned reboot"
    fi

    # Both endpoints from one `--list-boots` row each (fields: index, id,
    # first-entry day/date/time/tz, last-entry day/date/time/tz). Not
    # `journalctl -b N | head -1`: under pipefail, head closing the pipe kills
    # journalctl with SIGPIPE and the whole script exits 141 -- observed.
    prev_last="$(journalctl --list-boots -q --no-pager | awk -v i="$prev" '$1 == i { print $8, $9, $10 }')"
    cur_first="$(journalctl --list-boots -q --no-pager | awk -v i="$cur" '$1 == i { print $4, $5, $6 }')"
    if [ -n "$prev_last" ] && [ -n "$cur_first" ] \
        && date -d "$prev_last" >/dev/null 2>&1 && date -d "$cur_first" >/dev/null 2>&1; then
        down_seconds=$(( $(date -d "$cur_first" +%s) - $(date -d "$prev_last" +%s) ))
        [ "$down_seconds" -ge 0 ] || down_seconds=0
        DOWN_MIN=$(( down_seconds / 60 ))
    else
        # Timestamps unparseable -- approximate as 0 rather than fail the
        # whole run over one approximate number.
        DOWN_MIN=0
    fi
}

# --- the data volume's lock state, unprivileged, three-way -----------
#
# Order matters: an unconfigured node must never be reported as LOCKED, so
# /etc/crypttab is checked first. Matches on the first field exactly, not on
# any line mentioning "homelab-data" as a comment or neighbour.
volume_state() {
    if ! awk '$1 !~ /^#/ && $1 == "homelab-data" { f=1 } END { exit !f }' "$CRYPTTAB" 2>/dev/null; then
        STATE="not configured on this node"
    elif findmnt -n "$MOUNT" >/dev/null 2>&1; then
        STATE="unlocked"
    else
        STATE="LOCKED -- ssh homelab && sudo data-volume.sh unlock"
    fi
}

classify_boot
volume_state

MESSAGE="$(printf 'Home Lab back up. Down ~%dm, %s. Data volume: %s' "$DOWN_MIN" "$CLASS" "$STATE")"

# Logged before sending: journalctl -u homelab-watchdog.service -b shows
# exactly what was (or would have been) sent, with no separate debug path to
# drift from what homelab-notify.sh actually receives.
printf '%s\n' "$MESSAGE"

"$NOTIFY" "$MESSAGE"
