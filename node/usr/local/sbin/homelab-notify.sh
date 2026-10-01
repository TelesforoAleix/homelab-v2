#!/usr/bin/env bash
#
# homelab-notify.sh -- post one line of plain text to every allowlisted
# Telegram chat_id (Phase 12).
#
# WHY THIS EXISTS, AND WHY IT IS THE ONLY PLACE THIS PROJECT DEFINES "SEND"
#
#   Two independent units need to reach Telegram: homelab-watchdog.service
#   (the once-per-boot recovery notice) and homelab-notify@.service (a
#   service's failure alert, §7.6 of the Phase 12 brief). Two copies of
#   "build the URL, read the token, loop the allowlist, POST" is how they
#   drift -- one gets a bug fix, the token-redaction logic in the other one
#   doesn't, and nobody notices until the wrong one leaks something into a
#   log. This file is called by both, so there is exactly one definition to
#   get right.
#
# WHY IT ALSO KNOWS HOW TO COMPOSE A FAILURE ALERT (--alert)
#
#   §7.6 needs a short case statement mapping a literal alias (bot,
#   model-helper, watchdog; workbench since Phase 18.2; harness since Phase
#   23.0) to the unit(s) that actually failed, then a few
#   journal lines for context, before sending. That composition step lives
#   here, in one `bash -n`- and shellcheck-able file, rather than as an inline
#   one-liner inside homelab-notify@.service's ExecStart= -- systemd's own
#   quoting and %-escaping rules for a shell one-liner embedded in a unit file
#   are exactly the kind of thing Phase 18.1's seven runbook defects were made
#   of (written to be read once, not typed -- or in this case, parsed --
#   under real conditions). See that unit file for the fuller argument.
#
# WHY THE TOKEN IS READ FROM $CREDENTIALS_DIRECTORY, NEVER FROM
# /etc/homelab-telegram-bot/token DIRECTLY
#
#   Both calling units get their own LoadCredential=bot-token:... line naming
#   the same file the bot's own unit names. systemd (PID 1, root) resolves
#   that before this script ever runs and hands it a private tmpfs copy --
#   this script has no DAC read permission on the source file and does not
#   need any. Running this script outside systemd (no LoadCredential=) is a
#   deliberate hard failure, not a fallback to a world-readable path.
#
# WHAT IT DELIBERATELY DOES NOT DO
#
#   - Retry a failed send. The calling unit's own bounded on-failure restart
#     (or, for homelab-notify@.service, no restart at all -- see that unit's
#     header) is the retry policy; a loop in here would duplicate or fight it.
#   - Touch the model helper, the data volume, or anything requiring
#     privilege. It reads one credential and one allowlist file and makes one
#     outbound HTTPS call per recipient.
#   - Use parse_mode or any Markdown/HTML formatting. Plain text only, matching
#     the bot's own /status reply style (services/telegram-bot/executors.py)
#     and avoiding a formatting foot-gun in a message this project does not
#     need styled.
#
# USAGE
#
#   homelab-notify.sh "message text"        # send this exact text
#   homelab-notify.sh --alert <alias>       # compose and send a failure alert
#                                            # for bot | model-helper | watchdog
#                                            #     | workbench | harness
#
set -euo pipefail

ALLOWLIST="/etc/homelab-telegram-bot/allowlist"

die() { printf 'homelab-notify: %s\n' "$*" >&2; exit 1; }

trim() {
    local s="$1"
    s="${s#"${s%%[![:space:]]*}"}"
    s="${s%"${s##*[![:space:]]}"}"
    printf '%s' "$s"
}

# --- the shared primitive: send $1 to every allowlisted chat_id ------------
send_to_allowlist() {
    local message="$1" token line chat_id response sent=0 failed=0

    [ -n "${CREDENTIALS_DIRECTORY:-}" ] \
        || die "CREDENTIALS_DIRECTORY is not set. This must be started by systemd with LoadCredential=bot-token:/etc/homelab-telegram-bot/token."
    [ -r "${CREDENTIALS_DIRECTORY}/bot-token" ] \
        || die "cannot read ${CREDENTIALS_DIRECTORY}/bot-token"
    token="$(<"${CREDENTIALS_DIRECTORY}/bot-token")"
    [ -n "$token" ] || die "the bot token credential is empty"

    [ -r "$ALLOWLIST" ] || die "cannot read ${ALLOWLIST}"

    while IFS= read -r line || [ -n "$line" ]; do
        line="$(trim "${line%%#*}")"
        [ -n "$line" ] || continue
        case "$line" in
            *[!0-9]*)
                printf 'homelab-notify: ignoring non-numeric allowlist entry in %s\n' "$ALLOWLIST" >&2
                continue
                ;;
        esac
        chat_id="$line"

        response="$(curl --silent --max-time 30 \
            --data-urlencode "chat_id=${chat_id}" \
            --data-urlencode "text=${message}" \
            "https://api.telegram.org/bot${token}/sendMessage" 2>&1)" && curl_status=0 || curl_status=$?

        if [ "$curl_status" -eq 0 ] && printf '%s' "$response" | grep -q '"ok":true'; then
            sent=$((sent + 1))
        else
            failed=$((failed + 1))
            # The token never appears in Telegram's own response, but a
            # connection-level curl error can echo back the URL it tried --
            # redact defensively, the same discipline bot.py's redact() uses.
            printf 'homelab-notify: send to chat_id %s failed: %s\n' \
                "$chat_id" "${response//$token/<redacted-token>}" >&2
        fi
    done < "$ALLOWLIST"

    [ "$sent" -gt 0 ] || die "no message was sent -- allowlist empty, unreadable, or every send failed"
    [ "$failed" -eq 0 ] || exit 1
}

# --- §7.6: map a literal alias to its unit(s) and pull recent context ------
compose_alert() {
    local alias="$1" unit logs

    case "$alias" in
        bot)          unit="homelab-telegram-bot.service" ;;
        model-helper) unit="homelab-model-helper@*.service" ;;
        watchdog)     unit="homelab-watchdog.service" ;;
        # Phase 18.2: the Workbench's OnFailure= drop-in names this alias.
        # An alias not listed here dies at the notifier, so the alert for a
        # new unit is lost exactly when it is wanted -- add the case with the
        # drop-in, same commit.
        workbench)    unit="homelab-workbench.service" ;;
        # Phase 23.0: the harness's OnFailure= drop-in names this alias.
        harness)      unit="homelab-harness.service" ;;
        *) die "unknown alert alias: '${alias}' (expected bot, model-helper, watchdog, workbench or harness)" ;;
    esac

    # journalctl -u accepts a glob (systemd >= 246; this node runs 259.5), so
    # the model-helper case pulls from whichever templated instance failed
    # without needing to know its connection-specific name.
    logs="$(journalctl -u "$unit" -n 5 --no-pager -o short-iso 2>/dev/null | cut -c1-200)"
    [ -n "$logs" ] || logs="(no journal lines available)"

    send_to_allowlist "$(printf 'Home Lab alert: %s failed.\n%s' "$unit" "$logs")"
}

case "${1:-}" in
    --alert)
        [ $# -eq 2 ] || die "usage: homelab-notify.sh --alert <bot|model-helper|watchdog|workbench|harness>"
        compose_alert "$2"
        ;;
    -* )
        die "unknown option: $1"
        ;;
    "")
        die 'usage: homelab-notify.sh "message text"  |  homelab-notify.sh --alert <alias>'
        ;;
    *)
        [ $# -eq 1 ] || die 'usage: homelab-notify.sh "message text" (one argument, quoted)'
        send_to_allowlist "$1"
        ;;
esac
