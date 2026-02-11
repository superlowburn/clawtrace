#!/bin/bash
# ClawTrace Uninstaller — clean removal
# Usage: curl -sL clawtrace.vybng.co/uninstall | bash
# Flags: --purge  Remove all data including device credentials
set -euo pipefail

CLAWTRACE_DIR="$HOME/.clawtrace"
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
    # macOS: launchd
    if [ -f "$PLIST_PATH" ]; then
        launchctl unload "$PLIST_PATH" 2>/dev/null || true
        rm -f "$PLIST_PATH"
        echo "Removed launchd agent"
    else
        echo "No launchd agent found"
    fi
else
    # Linux: cron
    if crontab -l 2>/dev/null | grep -q '# clawtrace-sync'; then
        (crontab -l 2>/dev/null | grep -v '# clawtrace-sync') | crontab -
        echo "Removed cron job"
    else
        echo "No cron job found"
    fi
fi

# 2. Remove sender script
if [ -f "$CLAWTRACE_DIR/sender.py" ]; then
    rm -f "$CLAWTRACE_DIR/sender.py"
    echo "Removed sender.py"
fi

# Remove sync log
rm -f "$CLAWTRACE_DIR/sync.log"

# 3. Optionally purge all data
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
