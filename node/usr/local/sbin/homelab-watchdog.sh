#!/usr/bin/env bash
#
# homelab-watchdog.sh -- runs once per boot (Phase 12): tells the owner the
# node came back, how long it was down, whether the prior stop was clean, and
# whether the encrypted data volume needs unlocking.
#
# WHY THIS EXISTS
#
#   Phase 18.1 closed the volume's own gap (LUKS2, refuses without the
#   passphrase) but left an operational one open: the volume does not survive
#   an unattended reboot, and nothing told the owner. systemd's own timer is
#   the trigger source (homelab-watchdog.timer); this script is what it runs.
#
# WHY CLEAN-VS-UNPLANNED COMES FROM THE JOURNAL, NOT A HEARTBEAT
#
#   On a clean stop PID 1 logs exactly one "Shutting down." line as its last
#   act; on a power cut it logs nothing, because it never ran. That absence is
#   the entire signal -- the Phase 12 brief's §7.3 logic, ported from wtmp to
#   the journal. Downtime is (first entry of this boot) - (last entry of the
#   previous boot), read from `journalctl --list-boots`, by the same
#   mechanism in both branches. No daemon samples
#   anything while the node is healthy: "recovery-oriented, not real-time"
#   still holds in practice.
#
#   Match PID 1 ONLY (`_PID=1`). A user manager ending a login session logs
#   "Reached target shutdown.target" from its own PID inside a boot that was
#   later power-cut; matching on shutdown text without the PID filter would
#   call that power cut clean.
#
#   RECORD (PROJECT.md §11): the brief specified `last -x`, which this node
#   does not have -- util-linux 2.41.3 on Ubuntu 26.04 no longer ships it and
#   the wtmpdb replacement is not installed (§14: no package added). The first
#   version of this script concluded the journal heuristic was DISPROVED,
#   because it was tested against the boot -1 -> 0 transition believed to be
#   Phase 18.1's rescue reboot; that transition was in fact 18.1's power-cut
#   test. Re-run against both real transitions, the heuristic is correct: the
#   clean reboot (-2 -> -1) carries the PID-1 marker, the power cut (-1 -> 0)
#   carries none and boot 0 shows ext4 orphan cleanup on root.
#
# WHY THIS DOES NOT CALL `systemctl is-active` -- A DEVIATION FROM THE BRIEF'S
# OWN §7.4 TABLE
#
#   §7.4 as written checks `findmnt /srv/homelab` AND
#   `systemctl is-active homelab-data.target`. `systemctl` cannot ask PID 1
#   anything without an AF_UNIX socket (D-Bus, or /run/systemd/private
#   directly), and homelab-watchdog.service's own RestrictAddressFamilies=
#   deliberately excludes AF_UNIX (§6 of the brief -- the same restriction
#   that keeps this account off the model helper's socket). Those two
#   requirements cannot both hold in the same unit, for any account. This
#   script checks `findmnt` alone. It is a narrower signal than the brief
#   specifies, not a broader one: `findmnt` succeeding is necessary for
#   "unlocked" either way, and this node's own unlock procedure
#   (data-volume.sh unlock) always mounts before starting the target, so a
#   mount that exists without the target ever having been started is not a
#   state this node produces in practice. Recorded here and in the Phase 12
#   execution stage report, per §13's own instruction: the fix for something
#   RestrictAddressFamilies breaks is not to add AF_UNIX back.
#
# WHY IT DOES NOT, AND STRUCTURALLY CANNOT, REACH THE MODEL HELPER
#
#   §6 of the brief: nothing this phase schedules may call a model. The unit
#   running this script carries RestrictAddressFamilies=AF_INET AF_INET6 with
#   no AF_UNIX, so /run/homelab-model-helper.sock is unreachable at the
#   kernel's own seccomp filter before this script gets anywhere near it. This
#   script does not attempt to dial it, but the reason it MUST NOT is
#   enforced one layer below this file, in the unit, not by this comment.
#
# WHAT IT DELIBERATELY DOES NOT DO
#
#   - Hold any key material for the volume (ADR-046 §3). It only reads
#     /etc/crypttab (world-readable, no secret in it -- no keyfile path is
#     ever recorded there) and asks findmnt, exactly the unprivileged half of
#     what `data-volume.sh status` already asks.
#   - Retry on its own. homelab-watchdog.service's bounded on-failure restart
#     is the retry policy; a loop in here would duplicate it.
#   - Send the message itself. homelab-notify.sh is the one place this
#     project defines "post text to every allowlisted chat_id" -- this script
#     only composes the text and calls it.
#
set -euo pipefail

NOTIFY="/usr/local/sbin/homelab-notify.sh"
CRYPTTAB="/etc/crypttab"
MOUNT="/srv/homelab"

die() { printf 'homelab-watchdog: %s\n' "$*" >&2; exit 1; }

# --- §7.3: was the prior stop clean, and how long was the node down? -------
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
        # whole run over one number, per §7.3.
        DOWN_MIN=0
    fi
}

# --- §7.4: the data volume's lock state, unprivileged, three-way -----------
#
# Order matters: an unconfigured node must never be reported as LOCKED, so
# /etc/crypttab is checked first. Matches on the first field exactly, not on
# any line mentioning "homelab-data" as a comment or neighbour (§4 rule 7).
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
