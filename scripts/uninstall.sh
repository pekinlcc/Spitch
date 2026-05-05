#!/usr/bin/env bash
# Reverse the user-level install. Does NOT touch ~/.config/spitch so
# the user's saved Doubao credentials survive a reinstall.
set -euo pipefail

BIN_DAEMON="$HOME/.local/bin/spitch-daemon"
BIN_CONFIG="$HOME/.local/bin/spitch-config"
BIN_CLI="$HOME/.local/bin/spitch-cli"
BIN_CONSOLE="$HOME/.local/bin/spitch-console"
SYSTEMD_UNIT="$HOME/.config/systemd/user/spitch.service"
DESKTOP_FILE="$HOME/.local/share/applications/spitch.desktop"
ICON_FILE="$HOME/.local/share/icons/hicolor/scalable/apps/spitch.svg"

removed_any=0
for f in "$BIN_DAEMON" "$BIN_CONFIG" "$BIN_CLI" "$BIN_CONSOLE" "$SYSTEMD_UNIT" "$DESKTOP_FILE" "$ICON_FILE"; do
    if [ -e "$f" ]; then
        rm -f "$f"
        echo "spitch-uninstall: removed $f"
        removed_any=1
    fi
done

# If the systemd unit was active, disable it so it does not try to
# auto-start a now-missing binary on next login.
if command -v systemctl >/dev/null 2>&1; then
    systemctl --user disable --now spitch.service 2>/dev/null || true
fi

# Best-effort refresh of the icon + desktop databases so the entry
# disappears from the application menu without a logout. Failures
# are harmless — the file deletions above are the load-bearing part.
if command -v gtk-update-icon-cache >/dev/null 2>&1; then
    gtk-update-icon-cache -f -t "$HOME/.local/share/icons/hicolor" 2>/dev/null || true
fi
if command -v update-desktop-database >/dev/null 2>&1; then
    update-desktop-database -q "$HOME/.local/share/applications" 2>/dev/null || true
fi

# Stop any running daemon process. Use a literal substring rather than
# a regex with optional groups — pgrep on Linux uses BRE where ``?`` and
# ``( |$|--)`` are not metacharacters, so the previous pattern matched
# nothing and uninstall left the daemon running.
if pgrep -f -- "-m spitch" >/dev/null 2>&1; then
    pkill -f -- "-m spitch" || true
    echo "spitch-uninstall: stopped running daemon"
    removed_any=1
fi

if [ "$removed_any" -eq 0 ]; then
    echo "spitch-uninstall: nothing to remove."
fi
echo "spitch-uninstall: done. Config at \$XDG_CONFIG_HOME/spitch was preserved."
