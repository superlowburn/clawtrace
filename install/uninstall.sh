#!/bin/bash
# ClawTrace Uninstaller — clean removal
# Usage: curl -sL clawtrace.vybng.co/uninstall | bash
# Flags: --purge  Remove all data including device credentials
set -euo pipefail

CLAWTRACE_DIR="$HOME/.clawtrace"
BIN_DIR="$HOME/.local/bin"
PLIST_LABEL="co.vybng.clawtrace"
PLIST_PATH="$HOME/Library/LaunchAgents/${PLIST_LABEL}.plist"

PURGE=false
for arg in "$@"; do
    case "$arg" in
        --purge) PURGE=true ;;
    esac
done

echo "=== ClawTrace Uninstaller ==="
echo ""

# 1. Remove auto-sync
if [[ "$(uname)" == "Darwin" ]]; then
    if [ -f "$PLIST_PATH" ]; then
        launchctl unload "$PLIST_PATH" 2>/dev/null || true
        rm -f "$PLIST_PATH"
        echo "Removed launchd agent"
    else
        echo "No launchd agent found"
    fi
else
    if crontab -l 2>/dev/null | grep -q '# clawtrace-sync'; then
        (crontab -l 2>/dev/null | grep -v '# clawtrace-sync') | crontab -
        echo "Removed cron job"
    else
        echo "No cron job found"
    fi
fi

# 2. Remove CLI wrapper
if [ -f "$BIN_DIR/clawtrace" ]; then
    rm -f "$BIN_DIR/clawtrace"
    echo "Removed clawtrace command"
fi

# 3. Remove venv
if [ -d "$CLAWTRACE_DIR/venv" ]; then
    rm -rf "$CLAWTRACE_DIR/venv"
    echo "Removed ClawTrace venv"
fi

# 4. Remove sender and logs
rm -f "$CLAWTRACE_DIR/sender.py"
rm -f "$CLAWTRACE_DIR/sync.log"

# 5. Optionally purge all data
if $PURGE; then
    if [ -d "$CLAWTRACE_DIR" ]; then
        rm -rf "$CLAWTRACE_DIR"
        echo "Purged all data (including device credentials)"
    fi
else
    echo ""
    echo "Kept ~/.clawtrace/device.json (your device credentials)"
    echo "To remove everything: curl -sL clawtrace.vybng.co/uninstall | bash -s -- --purge"
fi

echo ""
echo "=== ClawTrace uninstalled ==="
