#!/usr/bin/env bash
# Reverse the user-level install. Does NOT touch ~/.config/spitch so
# the user's saved Doubao credentials survive a reinstall.
set -euo pipefail

BIN_DAEMON="$HOME/.local/bin/spitch-daemon"
BIN_CONFIG="$HOME/.local/bin/spitch-config"
SYSTEMD_UNIT="$HOME/.config/systemd/user/spitch.service"

removed_any=0
for f in "$BIN_DAEMON" "$BIN_CONFIG" "$SYSTEMD_UNIT"; do
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
