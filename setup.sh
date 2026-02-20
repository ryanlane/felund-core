#!/usr/bin/env bash
# setup.sh — create (or reuse) .venv and install all dependencies
# Run from the project root: bash setup.sh

set -euo pipefail

VENV=".venv"
PYTHON="${PYTHON:-python3}"

# ── Python version check ──────────────────────────────────────────────────────
PY_VERSION=$("$PYTHON" -c "import sys; print('%d.%d' % sys.version_info[:2])" 2>/dev/null) || {
    echo "ERROR: python3 not found. Install Python 3.9 or later."
    exit 1
}
PY_MAJOR=$(echo "$PY_VERSION" | cut -d. -f1)
PY_MINOR=$(echo "$PY_VERSION" | cut -d. -f2)
if [ "$PY_MAJOR" -lt 3 ] || { [ "$PY_MAJOR" -eq 3 ] && [ "$PY_MINOR" -lt 9 ]; }; then
    echo "ERROR: Python $PY_VERSION found, but 3.9+ is required."
    exit 1
fi
echo "Python $PY_VERSION OK"

# ── Create venv if missing ────────────────────────────────────────────────────
if [ ! -d "$VENV" ]; then
    echo "Creating virtual environment at $VENV/ ..."
    "$PYTHON" -m venv "$VENV"
else
    echo "Reusing existing $VENV/"
fi

PIP="$VENV/bin/pip"

# ── Install dependencies ──────────────────────────────────────────────────────
echo ""
echo "Installing API dependencies (api/requirements.txt) ..."
"$PIP" install --quiet -r api/requirements.txt

echo "Installing chat dependencies (chat/requirements.txt) ..."
"$PIP" install --quiet -r chat/requirements.txt

echo ""
echo "All dependencies installed."

# ── Usage summary ─────────────────────────────────────────────────────────────
cat <<'EOF'

━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━
  felund-core — setup complete
━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━

  Activate the venv:
    source .venv/bin/activate

  Run the chat TUI:
    .venv/bin/python chat/felundchat.py

  Run a specific CLI command:
    .venv/bin/python chat/felundchat.py --help
    .venv/bin/python chat/felundchat.py invite
    .venv/bin/python chat/felundchat.py join --code <code>

  Run the rendezvous API (optional):
    .venv/bin/uvicorn api.rendezvous:app --reload
    (set FELUND_API_BASE=http://localhost:8000 in the chat env)

━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━
EOF
