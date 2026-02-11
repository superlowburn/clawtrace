#!/bin/bash
# ClawTrace Installer — one-command setup for OpenClaw cost tracking
# Usage: curl -sL clawtrace.vybng.co/install | bash
set -euo pipefail

CLAWTRACE_DIR="$HOME/.clawtrace"
VENV_DIR="$CLAWTRACE_DIR/venv"
BIN_DIR="$HOME/.local/bin"
REPO_URL="https://github.com/superlowburn/clawtrace.git"
SENDER_URL="https://clawtrace.vybng.co/install/sender.py"
PLIST_LABEL="co.vybng.clawtrace"
PLIST_PATH="$HOME/Library/LaunchAgents/${PLIST_LABEL}.plist"

echo ""
echo "  ╔═══════════════════════════════════╗"
echo "  ║         ClawTrace Installer        ║"
echo "  ║  Local-first cost tracking for     ║"
echo "  ║  Claude Code & OpenClaw agents     ║"
echo "  ╚═══════════════════════════════════╝"
echo ""

# --- Step 1: Check python3 ---
if ! command -v python3 &>/dev/null; then
    echo "ERROR: python3 is required but not found."
    echo "Install Python 3: https://python.org"
    exit 1
fi

PY_VERSION=$(python3 -c 'import sys; print(f"{sys.version_info.major}.{sys.version_info.minor}")')
echo "  Found Python $PY_VERSION"

# --- Step 2: Create directory ---
mkdir -p "$CLAWTRACE_DIR"

# --- Step 3: Create venv and install ClawTrace ---
echo "  Installing ClawTrace..."

if [ -d "$VENV_DIR" ]; then
    echo "  Upgrading existing install..."
fi

python3 -m venv "$VENV_DIR"
"$VENV_DIR/bin/pip" install --upgrade pip -q 2>/dev/null
"$VENV_DIR/bin/pip" install "git+${REPO_URL}" -q 2>/dev/null

# Verify install
if ! "$VENV_DIR/bin/clawtrace" --help &>/dev/null; then
    echo "ERROR: Installation failed. Try manually:"
    echo "  python3 -m venv ~/.clawtrace/venv"
    echo "  ~/.clawtrace/venv/bin/pip install git+${REPO_URL}"
    exit 1
fi

echo "  Installed ClawTrace engine"

# --- Step 4: Add clawtrace to PATH ---
mkdir -p "$BIN_DIR"

# Create wrapper script (not symlink — more portable)
cat > "$BIN_DIR/clawtrace" <<'WRAPPER'
#!/bin/bash
exec "$HOME/.clawtrace/venv/bin/clawtrace" "$@"
WRAPPER
chmod +x "$BIN_DIR/clawtrace"

# Check if ~/.local/bin is on PATH
if ! echo "$PATH" | tr ':' '\n' | grep -qx "$BIN_DIR"; then
    SHELL_NAME=$(basename "$SHELL")
    case "$SHELL_NAME" in
        zsh)  RC_FILE="$HOME/.zshrc" ;;
        bash) RC_FILE="$HOME/.bashrc" ;;
        *)    RC_FILE="$HOME/.profile" ;;
    esac
    if ! grep -q '.local/bin' "$RC_FILE" 2>/dev/null; then
        echo 'export PATH="$HOME/.local/bin:$PATH"' >> "$RC_FILE"
        echo "  Added ~/.local/bin to PATH in $RC_FILE"
    fi
    export PATH="$BIN_DIR:$PATH"
fi

echo "  clawtrace command available"

# --- Step 5: Quick test ---
SUMMARY=$(clawtrace status --json 2>/dev/null || echo '{}')
COST=$(echo "$SUMMARY" | python3 -c "import sys,json; print(f'${json.load(sys.stdin).get(\"total_cost_usd\", 0):.2f}')" 2>/dev/null || echo "0.00")
SESSIONS=$(echo "$SUMMARY" | python3 -c "import sys,json; print(json.load(sys.stdin).get('session_count', 0))" 2>/dev/null || echo "0")

echo ""
echo "  ┌─────────────────────────────────┐"
echo "  │  Today: \$$COST across $SESSIONS sessions"
echo "  └─────────────────────────────────┘"

# --- Step 6: Optional hosted sync ---
echo ""
echo "  Want to sync your dashboard to the cloud?"
echo "  (Access from any device at clawtrace.vybng.co)"
echo ""

# If running non-interactively (piped), skip the prompt
if [ -t 0 ]; then
    printf "  Set up hosted sync? [y/N] "
    read -r SETUP_SYNC
else
    SETUP_SYNC="n"
    echo "  Run with --sync to set up hosted sync later."
fi

if [[ "${SETUP_SYNC:-n}" =~ ^[Yy] ]] || [[ "${1:-}" == "--sync" ]]; then
    echo ""
    echo "  Setting up hosted sync..."

    # Download sender
    curl -sL "$SENDER_URL" -o "$CLAWTRACE_DIR/sender.py"
    chmod +x "$CLAWTRACE_DIR/sender.py"

    # Install requests into venv for sender
    "$VENV_DIR/bin/pip" install requests -q 2>/dev/null

    # First sync
    "$VENV_DIR/bin/python" "$CLAWTRACE_DIR/sender.py" 2>/dev/null || true

    # Set up auto-sync
    if [[ "$(uname)" == "Darwin" ]]; then
        mkdir -p "$HOME/Library/LaunchAgents"
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
        <string>${VENV_DIR}/bin/python</string>
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
        echo "  Syncing every 15 minutes (launchd)"
    else
        CRON_CMD="*/15 * * * * $VENV_DIR/bin/python $CLAWTRACE_DIR/sender.py >> $CLAWTRACE_DIR/sync.log 2>&1 # clawtrace-sync"
        (crontab -l 2>/dev/null | grep -v '# clawtrace-sync') | crontab - 2>/dev/null || true
        (crontab -l 2>/dev/null; echo "$CRON_CMD") | crontab -
        echo "  Syncing every 15 minutes (cron)"
    fi
fi

# --- Done ---
echo ""
echo "  ╔═══════════════════════════════════╗"
echo "  ║         Setup complete!            ║"
echo "  ╚═══════════════════════════════════╝"
echo ""
echo "  Start your dashboard:"
echo "    clawtrace serve"
echo ""
echo "  Then open http://localhost:19898"
echo ""
echo "  Other commands:"
echo "    clawtrace status        Quick cost summary"
echo "    clawtrace cost-report   Detailed breakdown"
echo "    clawtrace anomalies     Recent cost spikes"
echo ""
echo "  Uninstall: curl -sL clawtrace.vybng.co/uninstall | bash"
echo ""
