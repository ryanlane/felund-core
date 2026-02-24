Deploying relay_ws.py with Caddy
1 — Install dependencies on the server

# Python deps (in a venv is cleaner)
python3 -m venv /opt/felund-relay/venv
/opt/felund-relay/venv/bin/pip install aiohttp aiosqlite

# Copy the relay script
cp /path/to/felund-core/api/relay_ws.py /opt/felund-relay/relay_ws.py
mkdir -p /opt/felund-relay/data
2 — Create the systemd service

sudo nano /etc/systemd/system/felund-relay.service

[Unit]
Description=Felund Relay WS Service
After=network.target

[Service]
Type=simple
User=www-data
WorkingDirectory=/opt/felund-relay
ExecStart=/opt/felund-relay/venv/bin/python relay_ws.py \
    --host 127.0.0.1 \
    --port 8765 \
    --db /opt/felund-relay/data/felund.sqlite
Restart=always
RestartSec=5
StandardOutput=journal
StandardError=journal
SyslogIdentifier=felund-relay

# Keep the process from eating unbounded memory
MemoryMax=256M

[Install]
WantedBy=multi-user.target

# Enable and start
sudo systemctl daemon-reload
sudo systemctl enable felund-relay
sudo systemctl start felund-relay
sudo systemctl status felund-relay
Note: bind to 127.0.0.1 (not 0.0.0.0) so the port is only reachable through Caddy, not directly.

3 — Caddy configuration

sudo nano /etc/caddy/Caddyfile

relay.yourdomain.com {
    reverse_proxy localhost:8765 {
        # Required for WebSocket upgrade (/v1/relay/ws)
        header_up Connection {http.upgrade}
        header_up Upgrade {http.upgrade}
    }
}

sudo systemctl reload caddy
Caddy auto-provisions a Let's Encrypt TLS cert on first request. Give it 30–60 seconds on first startup.

4 — Verify it's working

# Health check
curl https://relay.yourdomain.com/v1/health

# Expected response:
# {"ok":true,"version":"0.2.0","time":1234567890}
Then in the Felund web client settings, set the relay URL to:


https://relay.yourdomain.com
5 — Checking logs
Service logs (live tail):


sudo journalctl -u felund-relay -f
Last 100 lines:


sudo journalctl -u felund-relay -n 100
Since a specific time:


sudo journalctl -u felund-relay --since "1 hour ago"
Caddy logs (access + errors):


sudo journalctl -u caddy -f
Check service is running / auto-restarted:


sudo systemctl status felund-relay
6 — Updating the relay

# Copy new version
cp /path/to/felund-core/api/relay_ws.py /opt/felund-relay/relay_ws.py

# Restart (zero downtime for clients — they reconnect within 5s)
sudo systemctl restart felund-relay
7 — Data directory permissions (if not using root/www-data)
If you want to run as a dedicated user instead of www-data:


sudo useradd -r -s /bin/false felund
sudo chown -R felund:felund /opt/felund-relay
# Then set User=felund in the .service file