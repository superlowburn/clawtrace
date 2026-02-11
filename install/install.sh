#!/bin/bash
# ClawTrace Installer — one-command setup for OpenClaw cost tracking
# Usage: curl -sL clawtrace.vybng.co/install | bash
set -euo pipefail

CLAWTRACE_DIR="$HOME/.clawtrace"
SENDER_URL="https://clawtrace.vybng.co/install/sender.py"
PLIST_LABEL="co.vybng.clawtrace"
PLIST_PATH="$HOME/Library/LaunchAgents/${PLIST_LABEL}.plist"

echo "=== ClawTrace Installer ==="
echo ""

# 1. Check python3
if ! command -v python3 &>/dev/null; then
    echo "ERROR: python3 is required but not found."
    echo "Install Python 3 from https://python.org and try again."
    exit 1
fi

# 2. Check requests module
if ! python3 -c "import requests" 2>/dev/null; then
    echo "Installing requests module..."
    python3 -m pip install --user requests -q 2>/dev/null || {
        echo "ERROR: Could not install 'requests' module."
        echo "Run: pip3 install requests"
        exit 1
    }
fi

# 3. Create directory
mkdir -p "$CLAWTRACE_DIR"

# 4. Download sender
echo "Downloading sender..."
curl -sL "$SENDER_URL" -o "$CLAWTRACE_DIR/sender.py"
chmod +x "$CLAWTRACE_DIR/sender.py"
echo "Saved to $CLAWTRACE_DIR/sender.py"

# 5. First sync (auto-registers device, syncs data, prints dashboard URL)
echo ""
echo "Running first sync..."
python3 "$CLAWTRACE_DIR/sender.py"

# 6. Set up auto-sync (every 15 minutes)
echo ""
echo "Setting up auto-sync..."

if [[ "$(uname)" == "Darwin" ]]; then
    # macOS: launchd
    mkdir -p "$HOME/Library/LaunchAgents"

    # Unload existing if present (idempotent)
    if launchctl list "$PLIST_LABEL" &>/dev/null 2>&1; then
        launchctl unload "$PLIST_PATH" 2>/dev/null || true
    fi

    cat > "$PLIST_PATH" <<PLIST
<?xml version="1.0" encoding="UTF-8"?>
<!DOCTYPE plist PUBLIC "-//Apple//DTD PLIST 1.0//EN" "http://www.apple.com/DTDs/PropertyList-1.0.dtd">
<plist version="1.0">
<dict>
    <key>Label</key>
    <string>${PLIST_LABEL}</string>
    <key>ProgramArguments</key>
    <array>
        <string>$(command -v python3)</string>
        <string>${CLAWTRACE_DIR}/sender.py</string>
    </array>
    <key>StartInterval</key>
    <integer>900</integer>
    <key>RunAtLoad</key>
    <true/>
    <key>StandardOutPath</key>
    <string>${CLAWTRACE_DIR}/sync.log</string>
    <key>StandardErrorPath</key>
    <string>${CLAWTRACE_DIR}/sync.log</string>
</dict>
</plist>
PLIST

    launchctl load "$PLIST_PATH"
    echo "launchd agent installed (syncs every 15 minutes)"

else
    # Linux: cron
    CRON_CMD="*/15 * * * * python3 $CLAWTRACE_DIR/sender.py >> $CLAWTRACE_DIR/sync.log 2>&1 # clawtrace-sync"

    # Remove existing entry if present (idempotent)
    (crontab -l 2>/dev/null | grep -v '# clawtrace-sync') | crontab - 2>/dev/null || true
    # Add new entry
    (crontab -l 2>/dev/null; echo "$CRON_CMD") | crontab -
    echo "Cron job installed (syncs every 15 minutes)"
fi

echo ""
echo "=== ClawTrace installed ==="
echo "Your data syncs automatically every 15 minutes."
echo "To sync manually: python3 ~/.clawtrace/sender.py"
echo "To uninstall: curl -sL clawtrace.vybng.co/uninstall | bash"
