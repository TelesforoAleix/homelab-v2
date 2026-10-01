#!/usr/bin/env bash
#
# homelab-notify.sh -- post plain text to every allowlisted Telegram chat_id.
#
# The watchdog and failure notifier share one send implementation so fixes
# and token redaction cannot drift between copies.
# --alert maps bot, model-helper, watchdog, workbench, harness and homelab
# to unit names and adds recent journal context. This stays in a script that
# bash -n and shellcheck can check, avoiding systemd's shell quoting and
# %-escaping rules for embedded commands.
#
# PID 1 supplies the credential in $CREDENTIALS_DIRECTORY. The script needs
# no source-token permission; running outside systemd is a hard failure.
# Calling units own retry policy. The script touches no model helper or
# volume and needs no privilege: it reads the credential and allowlist and
# makes an outbound HTTPS call per recipient. Plain text avoids formatting
# errors in operational messages.
#
# USAGE
#   homelab-notify.sh "message text"
#   homelab-notify.sh --alert <bot|model-helper|watchdog|workbench|harness|homelab>
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
            # redact defensively before logging the error.
            printf 'homelab-notify: send to chat_id %s failed: %s\n' \
                "$chat_id" "${response//$token/<redacted-token>}" >&2
        fi
    done < "$ALLOWLIST"

    [ "$sent" -gt 0 ] || die "no message was sent -- allowlist empty, unreadable, or every send failed"
    [ "$failed" -eq 0 ] || exit 1
}

# --- map a literal alias to its unit(s) and pull recent context ----------
compose_alert() {
    local alias="$1" unit logs

    case "$alias" in
        bot)          unit="homelab-telegram-bot.service" ;;
        model-helper) unit="homelab-model-helper@*.service" ;;
        watchdog)     unit="homelab-watchdog.service" ;;
        # The Workbench's OnFailure= drop-in names this alias.
        # An alias not listed here dies at the notifier, so the alert for a
        # new unit is lost exactly when it is wanted -- add the case with the
        # drop-in, same commit.
        workbench)    unit="homelab-workbench.service" ;;
        # The harness's OnFailure= drop-in names this alias.
        harness)      unit="homelab-harness.service" ;;
        homelab)      unit="homelab.service" ;;
        *) die "unknown alert alias: '${alias}' (expected bot, model-helper, watchdog, workbench, harness or homelab)" ;;
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
        [ $# -eq 2 ] || die "usage: homelab-notify.sh --alert <bot|model-helper|watchdog|workbench|harness|homelab>"
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
