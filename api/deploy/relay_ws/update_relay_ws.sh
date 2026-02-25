#!/usr/bin/env bash
set -euo pipefail

RELAY_DIR="${FELUND_RELAY_DIR:-/opt/felund/relay_ws}"
RELAY_REQ="$RELAY_DIR/api/relay_requirements.txt"
VENV_DIR="$RELAY_DIR/.venv"

if [[ ! -f "$RELAY_REQ" ]]; then
  echo "relay_requirements.txt not found at $RELAY_REQ" >&2
  exit 1
fi

if [[ -d "$RELAY_DIR/.git" ]]; then
  git -C "$RELAY_DIR" pull --ff-only
fi

"$VENV_DIR/bin/pip" install -r "$RELAY_REQ"

sudo systemctl daemon-reload
sudo systemctl restart felund-relay-ws.service

echo "Updated and restarted felund-relay-ws.service"
