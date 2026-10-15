#!/bin/zsh
# Build the HomeLab menu bar app, install it ad hoc signed into ~/Applications, and (re)start
# its login LaunchAgent. Run from the owner's checkout, outside any sandbox. Switches nothing.
set -euo pipefail

MAC_DIR="${0:A:h}"
REPO="${MAC_DIR:h}"
APP="$HOME/Applications/HomeLab.app"
LABEL="org.homelab.menu"
AGENT="$HOME/Library/LaunchAgents/$LABEL.plist"
PYTHON="$REPO/.venv/bin/python3"
[[ -x "$PYTHON" ]] || PYTHON="$(command -v python3)"

swift build --package-path "$MAC_DIR/MenuBar" -c release
BINARY="$(swift build --package-path "$MAC_DIR/MenuBar" -c release --show-bin-path)/HomeLabMenuBar"

# Stop the running copy before replacing it.
/bin/launchctl bootout "gui/$(id -u)/$LABEL" 2>/dev/null || true
while /bin/launchctl print "gui/$(id -u)/$LABEL" >/dev/null 2>&1; do sleep 0.2; done
rm -rf "$APP"
/usr/bin/install -d -m 0755 "$APP/Contents/MacOS" "$HOME/Library/LaunchAgents" "$HOME/.local/bin"
/usr/bin/install -m 0755 "$BINARY" "$APP/Contents/MacOS/HomeLabMenuBar"
/usr/bin/install -m 0755 "$MAC_DIR/usage-snapshot" "$HOME/.local/bin/usage-snapshot"

# The app runs the same command and interpreter as the profile LaunchAgent: this checkout.
INFO="$APP/Contents/Info.plist"
/usr/bin/plutil -create xml1 "$INFO"
for key value in \
  CFBundleExecutable HomeLabMenuBar \
  CFBundleIdentifier "$LABEL" \
  CFBundleName HomeLab \
  CFBundlePackageType APPL \
  CFBundleShortVersionString 1.0 \
  CFBundleVersion 1 \
  LSMinimumSystemVersion 14.0 \
  HomeLabPython "$PYTHON" \
  HomeLabProfileCommand "$MAC_DIR/homelab-profile"; do
  /usr/bin/plutil -insert "$key" -string "$value" "$INFO"
done
/usr/bin/plutil -insert LSUIElement -bool YES "$INFO"
/usr/bin/codesign --force --sign - "$APP"

rm -f "$AGENT"
/usr/bin/plutil -create xml1 "$AGENT"
/usr/bin/plutil -insert Label -string "$LABEL" "$AGENT"
/usr/bin/plutil -insert ProgramArguments -array "$AGENT"
/usr/bin/plutil -insert ProgramArguments.0 -string "$APP/Contents/MacOS/HomeLabMenuBar" "$AGENT"
/usr/bin/plutil -insert RunAtLoad -bool YES "$AGENT"
/usr/bin/plutil -insert ProcessType -string Interactive "$AGENT"
/usr/bin/plutil -insert StandardOutPath -string /dev/null "$AGENT"
/usr/bin/plutil -insert StandardErrorPath -string /dev/null "$AGENT"
chmod 0600 "$AGENT"

/bin/launchctl bootstrap "gui/$(id -u)" "$AGENT"
echo "Installed $APP (ad hoc signed) and started $LABEL; it starts at each login."
