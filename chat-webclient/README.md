# felundchat — web client

A browser-based chat client for felund circles.  It uses the same invite
code format and HMAC-signed message protocol as the Python TUI client, so
users on both clients can share circles and exchange messages seamlessly.

## What it is

- **Full chat client** — not a monitor or debug UI.  Create circles, join via
  invite code, send and receive messages, switch channels.
- **Relay-only** — browsers cannot open raw TCP sockets, so the web client
  syncs exclusively through the relay API (`POST / GET /v1/messages`).  No
  direct peer-to-peer connectivity.
- **PWA-capable** — can be installed to the home screen on mobile or desktop.
- **Terminal aesthetic** — deliberately styled to match the Python TUI layout:
  full-screen, monospace font, sidebar + message log + input bar.

## How it talks to the backend

```
Browser ──── HTTPS ────► Relay API  (POST /v1/messages  push)
                                    (GET  /v1/messages  pull, every 5 s)
                         Rendezvous (POST /v1/register   presence)
                                    (GET  /v1/peers      peer count)
```

The relay server stores messages for up to 30 days.  Integrity is guaranteed
by HMAC-SHA256 — the server never sees the circle secret and cannot forge
messages.  The browser verifies every pulled message before displaying it.

## Development

```bash
cd chat-webclient
cp .env.example .env        # set VITE_FELUND_API_BASE=http://localhost:8000
npm install
npm run dev
```

## E2E tests (Phase 5 call media)

These tests spin up the WebRTC media call flow across two Chromium contexts
using Playwright and the Python relay server.

```bash
cd chat-webclient
pip install -r ../api/relay_requirements.txt
npm install
npx playwright install
npm run test:e2e
```

Optional overrides:

- Use an existing relay server:
  `FELUND_RELAY_EXTERNAL=1 FELUND_RELAY_BASE=http://127.0.0.1:8765 npm run test:e2e`
- Choose a Python executable for the relay server:
  `FELUND_RELAY_PYTHON=python3 npm run test:e2e`

## Production build

```bash
npm run build               # outputs to dist/
```

Deploy the `dist/` folder to any static host (Netlify, Cloudflare Pages,
nginx, etc.).  Set `VITE_FELUND_API_BASE` in your hosting environment's
environment variable settings before building.

## Environment variables

| Variable | Description |
|----------|-------------|
| `VITE_FELUND_API_BASE` | Base URL of the relay/rendezvous API server. Leave blank to start with relay disabled — users can configure it in Settings (F1). |

## Keyboard shortcuts

| Key | Action |
|-----|--------|
| `F1` | Open Settings (display name, relay URL) |
| `F2` | Show invite code for the active circle |
| `Escape` | Close modal |

## Slash commands

Type in the message bar:

| Command | Action |
|---------|--------|
| `/invite` | Show invite code |
| `/join <code>` | Join a circle via invite code |
| `/name <name>` | Change display name |
| `/channel create <name>` | Create a channel |
| `/channel switch <name>` | Switch active channel |
| `/channels` | List channels |
| `/settings` | Open settings modal |
| `/help` | List all commands |
