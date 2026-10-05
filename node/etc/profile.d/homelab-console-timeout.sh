# Home Lab — console idle timeout. Phase 13 §6.5.
#
# Installed to /etc/profile.d/homelab-console-timeout.sh (root:root 0644).
#
# The console is the recovery path (ADR-041) and the box is in the owner's
# room; a tty left logged in after a rescue is a root-capable shell for whoever
# walks up next. This ends an idle *console* login after 15 minutes.
#
# It applies ONLY to /dev/ttyN logins. An SSH session has a pts, not a tty, and
# is deliberately untouched: the owner's unlock, validation and long-running
# runbook sessions must not be killed by a timeout (brief §9). `readonly` so a
# shell cannot unset it. Bash honours TMOUT at the interactive prompt; that is
# the only shell on this node.
case "$(tty 2>/dev/null)" in
  /dev/tty[0-9]*)
    TMOUT=900
    readonly TMOUT
    export TMOUT
    ;;
esac
