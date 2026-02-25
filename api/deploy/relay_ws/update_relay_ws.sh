#!/usr/bin/env bash
set -euo pipefail

RELAY_DIR="${FELUND_RELAY_DIR:-/opt/felund/relay_ws}"
RELAY_REQ="$RELAY_DIR/relay_requirements.txt"
RELAY_APP="$RELAY_DIR/relay_ws.py"
VENV_DIR="$RELAY_DIR/.venv"
ENV_FILE="/etc/felund/relay_ws.env"
SERVICE_FILE="/etc/systemd/system/felund-relay-ws.service"

if [[ ! -f "$RELAY_REQ" ]]; then
  echo "relay_requirements.txt not found at $RELAY_REQ" >&2
  exit 1
fi

if [[ -d "$RELAY_DIR/.git" ]]; then
  git -C "$RELAY_DIR" pull --ff-only
fi

"$VENV_DIR/bin/pip" install -r "$RELAY_REQ"

if [[ -f "$SERVICE_FILE" && -f "$ENV_FILE" ]]; then
  if ! grep -q "/relay_ws.py" "$SERVICE_FILE"; then
    sudo tee "$SERVICE_FILE" >/dev/null <<EOF
[Unit]
Description=Felund Relay WS
After=network.target

[Service]
Type=simple
User=${FELUND_RELAY_USER:-felund}
Group=${FELUND_RELAY_GROUP:-felund}
WorkingDirectory=$RELAY_DIR
EnvironmentFile=$ENV_FILE
ExecStart=$VENV_DIR/bin/python $RELAY_APP --host \${FELUND_RELAY_HOST} --port \${FELUND_RELAY_PORT} --db \${FELUND_RELAY_DB}
Restart=on-failure
RestartSec=1

[Install]
WantedBy=multi-user.target
EOF
  fi
fi

sudo systemctl daemon-reload
sudo systemctl restart felund-relay-ws.service

echo "Updated and restarted felund-relay-ws.service"
