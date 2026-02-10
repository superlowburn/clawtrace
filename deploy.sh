#!/usr/bin/env bash
# ClawTrace VPS Deploy Script
# Syncs code, sets up venv, installs deps, inits DB
#
# Usage: ./deploy.sh [--sync-only] [--setup-only]
#   No args     = full deploy (sync + setup)
#   --sync-only = rsync files only, skip setup
#   --setup-only = run setup on VPS only, skip rsync

set -euo pipefail

VPS="root@143.110.218.78"
VPS_DIR="/root/clawtrace"
LOCAL_DIR="$(cd "$(dirname "$0")" && pwd)"

SYNC=true
SETUP=true

for arg in "$@"; do
    case "$arg" in
        --sync-only) SETUP=false ;;
        --setup-only) SYNC=false ;;
        *) echo "Unknown arg: $arg"; exit 1 ;;
    esac
done

# --- Step 1: Sync files ---
if $SYNC; then
    echo "=== Syncing code to VPS ==="
    rsync -avz --delete \
        --exclude '.venv/' \
        --exclude '__pycache__/' \
        --exclude '*.pyc' \
        --exclude '.pytest_cache/' \
        --exclude 'clawtrace.egg-info/' \
        --exclude '*.db' \
        --exclude 'web/' \
        --exclude 'app/' \
        --exclude 'node_modules/' \
        --exclude '.wrangler/' \
        --exclude 'tasks/' \
        "$LOCAL_DIR/" "$VPS:$VPS_DIR/"
    echo "--- Files synced ---"
fi

# --- Step 2: Setup on VPS ---
if $SETUP; then
    echo "=== Setting up VPS environment ==="
    ssh "$VPS" bash -s <<'REMOTE'
set -euo pipefail
VPS_DIR="/root/clawtrace"

# Create venv if missing
if [ ! -f "$VPS_DIR/.venv/bin/python" ]; then
    echo "--- Creating venv ---"
    python3 -m venv "$VPS_DIR/.venv"
fi

# Install/upgrade deps + install package in editable mode
echo "--- Installing dependencies ---"
"$VPS_DIR/.venv/bin/pip" install --upgrade pip -q
"$VPS_DIR/.venv/bin/pip" install -r "$VPS_DIR/engine/requirements.txt" -q
cd "$VPS_DIR" && "$VPS_DIR/.venv/bin/pip" install -e . -q
echo "flask=$("$VPS_DIR/.venv/bin/python" -c 'import flask; print(flask.__version__)' 2>/dev/null || echo 'FAILED')"
echo "gunicorn=$("$VPS_DIR/.venv/bin/python" -c 'import gunicorn; print(gunicorn.__version__)' 2>/dev/null || echo 'FAILED')"
echo "clawtrace=$("$VPS_DIR/.venv/bin/python" -c 'import engine; print("importable")' 2>/dev/null || echo 'FAILED')"

# Copy VPS config into place
if [ -f "$VPS_DIR/engine/config.vps.json" ]; then
    echo "--- Installing VPS config ---"
    cp "$VPS_DIR/engine/config.vps.json" "$VPS_DIR/engine/config.json"
fi

# Init DB
echo "--- Initializing database ---"
"$VPS_DIR/.venv/bin/python" -c "
from engine import db
path = db.init_db('/root/clawtrace/clawtrace.db')
print(f'DB initialized at {path}')
"

# Validate
echo "=== Validation ==="
echo "--- Health check test ---"
"$VPS_DIR/.venv/bin/python" -c "
from engine.server import create_app
app = create_app()
with app.test_client() as c:
    r = c.get('/api/health')
    print(f'Health: {r.get_json()}')
"

echo ""
echo "=== Deploy complete ==="
echo "Remember to:"
echo "  1. Create/update systemd service: /etc/systemd/system/clawtrace.service"
echo "  2. Create/update nginx config: /etc/nginx/sites-available/clawtrace"
echo "  3. systemctl daemon-reload && systemctl restart clawtrace"
REMOTE
fi
