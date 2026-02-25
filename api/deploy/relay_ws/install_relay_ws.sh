#!/usr/bin/env bash
set -euo pipefail

RELAY_DIR="${FELUND_RELAY_DIR:-/opt/felund/relay_ws}"
RELAY_USER="${FELUND_RELAY_USER:-felund}"
RELAY_GROUP="${FELUND_RELAY_GROUP:-felund}"
RELAY_APP="$RELAY_DIR/api/relay_ws.py"
RELAY_REQ="$RELAY_DIR/api/relay_requirements.txt"
VENV_DIR="$RELAY_DIR/.venv"
DATA_DIR="${FELUND_RELAY_DATA_DIR:-/var/lib/felund/relay_ws}"
ENV_FILE="/etc/felund/relay_ws.env"
SERVICE_FILE="/etc/systemd/system/felund-relay-ws.service"

if [[ ! -f "$RELAY_APP" ]]; then
  echo "relay_ws.py not found at $RELAY_APP" >&2
  exit 1
fi
if [[ ! -f "$RELAY_REQ" ]]; then
  echo "relay_requirements.txt not found at $RELAY_REQ" >&2
  exit 1
fi

if ! id -u "$RELAY_USER" >/dev/null 2>&1; then
  sudo useradd --system --create-home --home-dir /var/lib/felund --shell /usr/sbin/nologin "$RELAY_USER"
fi

sudo mkdir -p "$DATA_DIR"
sudo mkdir -p /etc/felund
sudo chown -R "$RELAY_USER":"$RELAY_GROUP" "$DATA_DIR"

if [[ ! -d "$VENV_DIR" ]]; then
  python3 -m venv "$VENV_DIR"
fi

"$VENV_DIR/bin/pip" install --upgrade pip
"$VENV_DIR/bin/pip" install -r "$RELAY_REQ"

if [[ ! -f "$ENV_FILE" ]]; then
  sudo tee "$ENV_FILE" >/dev/null <<'EOF'
FELUND_RELAY_HOST=0.0.0.0
FELUND_RELAY_PORT=8765
FELUND_RELAY_DB=/var/lib/felund/relay_ws/relay.sqlite
FELUND_SIGNAL_RATE_LIMIT=200
FELUND_SIGNAL_RATE_WINDOW_S=10.0
EOF
fi

sudo tee "$SERVICE_FILE" >/dev/null <<EOF
[Unit]
Description=Felund Relay WS
After=network.target

[Service]
Type=simple
User=$RELAY_USER
Group=$RELAY_GROUP
WorkingDirectory=$RELAY_DIR
EnvironmentFile=$ENV_FILE
ExecStart=$VENV_DIR/bin/python $RELAY_APP --host \${FELUND_RELAY_HOST} --port \${FELUND_RELAY_PORT} --db \${FELUND_RELAY_DB}
Restart=on-failure
RestartSec=1

[Install]
WantedBy=multi-user.target
EOF

sudo systemctl daemon-reload
sudo systemctl enable felund-relay-ws.service
sudo systemctl restart felund-relay-ws.service

echo "Installed and started felund-relay-ws.service"
