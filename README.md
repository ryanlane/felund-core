# felund-core

Peer-to-peer group chat with gossip-based sync, optional relay for
browser clients, and a terminal UI + web client.

## Requirements

- Python 3.9+ (3.11+ recommended)
- Two terminals/devices on the same LAN or reachable network
- Open/forwarded port (default: 9999) if connecting across networks

## Setup

```bash
git clone <repo>
cd felund-core
bash setup.sh
```

This creates a `.venv/` at the project root and installs all dependencies for
both the chat client and the optional API service.

State is stored at `~/.felundchat/state.json`.

## Clients

### Python TUI

```bash
.venv/bin/python chat/felundchat.py
```

Launches the panel-based terminal UI.  On first run a short wizard guides
you through creating or joining a circle.

```
┌─ felundchat ──────────────────────────────────────────────────────┐
│ node: a3f8b2 | #general | 2 peers                     ctrl+q=quit │
├──────────────────┬────────────────────────────────────────────────┤
│ Circles          │ [10:32] alice: hey everyone                    │
│ ──────────────── │ [10:33] bob: yo                                │
│ ● mygroup        │ [10:35] you: what's up                         │
│   #general ←     │                                                │
│   #random        │                                                │
├──────────────────┴────────────────────────────────────────────────┤
│ > _                                                               │
└───────────────────────────────────────────────────────────────────┘
```

#### TUI keyboard shortcuts

| Key | Action |
|-----|--------|
| `F3` | Settings (display name, relay URL) |
| `F2` | Show invite code for active circle |
| `F1` | Help |
| `ctrl+q` | Quit |
| `Escape` | Re-focus input bar |

#### TUI slash commands

| Command | Action |
|---------|--------|
| `/help [command]` | List all commands or detail one |
| `/invite` | Show invite code for the active circle |
| `/join <code>` | Join a new circle via felund code |
| `/circles` | List joined circles |
| `/channels` | List channels in the active circle |
| `/channel create <name> [public\|key\|invite]` | Create a channel |
| `/channel join <name> [key]` | Join a channel |
| `/channel switch <name>` | Switch active channel |
| `/channel leave <name>` | Leave a channel |
| `/who [channel]` | Show members |
| `/name [new_name]` | Show or update display name |
| `/settings` | Open settings modal |
| `/debug` | Toggle gossip debug log |
| `/quit` | Exit |

### Web client

A full chat client that runs in the browser.  Uses the same invite codes and
message format as the Python TUI — users on both clients can share circles.
See [`chat-webclient/README.md`](chat-webclient/README.md) for setup.

```bash
cd chat-webclient
cp .env.example .env   # set VITE_FELUND_API_BASE
npm install && npm run dev
```

## Relay / rendezvous server

An API server handles two concerns:

- **Rendezvous** — peers register their endpoints; others look them up to
  attempt direct connections.
- **Relay** — a simple store-and-forward message bus used by browser clients
  (which cannot open raw TCP connections) and useful as a fallback when
  direct peer connections are not possible.

### PHP server (recommended for shared hosting)

Requires PHP 8.1+ with `pdo_sqlite`.  No other dependencies.

```bash
# Development (built-in server)
php -S 0.0.0.0:8000 api/php/rendezvous.php

# Production — copy api/php/ into your document root.
# Apache .htaccess and an nginx config are included.
```

### Python/FastAPI server (alternative, rendezvous-only)

Requires the `api/` virtualenv.

```bash
.venv/bin/uvicorn api.rendezvous:app --reload
```

Note: the FastAPI server currently implements presence/rendezvous only
(`/v1/register`, `/v1/peers`, `/v1/health`).  For full relay support
(`/v1/messages`) use the PHP server.

### Connecting clients to the server

```bash
# Python TUI — set env var or configure via F3 in the TUI
export FELUND_API_BASE=http://your-server/api
.venv/bin/python chat/felundchat.py

# Web client — set in chat-webclient/.env before npm run dev / build
VITE_FELUND_API_BASE=http://your-server/api
```

## CLI subcommands

```bash
# Initialize local node settings
.venv/bin/python chat/felundchat.py init --bind 192.168.1.10 --port 9999 --name Alice

# Create a circle + print invite code
.venv/bin/python chat/felundchat.py invite

# Join via single invite code
.venv/bin/python chat/felundchat.py join --code <felund_code>

# Start gossip service (headless)
.venv/bin/python chat/felundchat.py run

# Send a message from the command line
.venv/bin/python chat/felundchat.py send --circle-id <id> "hello world"

# Show inbox
.venv/bin/python chat/felundchat.py inbox --circle-id <id> --limit 50

# List circles or peers in a circle
.venv/bin/python chat/felundchat.py peers [--circle-id <id>]
```

## Topology examples

### LAN / same network

Both nodes on the same local network.  No relay needed, no port forwarding
needed.  Direct TCP gossip.

```
Alice (192.168.1.10:9999)  ◄──────────►  Bob (192.168.1.11:9999)
```

### Internet with port forwarding

One node has a reachable public port.  The other initiates the connection.
The host shares their public IP:port in the invite code.

```
Alice (1.2.3.4:9999, port-forwarded)  ◄──────────►  Bob (behind NAT)
```

### Internet via relay (browser or NAT-to-NAT)

Neither peer can reach the other directly, or one is a browser client.
Both push and pull messages through the relay API.

```
Alice (Python TUI)  ──► relay server ◄──  Bob (browser)
                              │
                    (HMAC-authenticated,
                     server cannot read
                     or forge messages)
```

The relay is store-and-forward: messages are retained for up to 30 days.
Peers pull at 5-second intervals.

## Trust and threat model

- **Authentication** — every message carries an HMAC-SHA256 MAC derived from
  the circle secret.  Any tampered or forged message is rejected by recipients.
- **Confidentiality** — messages are **not encrypted** in the current version.
  The relay server and any network observer can read message content.
  Treat felund as "tamper-evident chat," not "end-to-end encrypted chat."
- **Circle membership** — anyone who obtains the invite code (which embeds the
  circle secret) can join the circle and read all messages.  Protect invite
  codes accordingly.
- **Relay trust** — the relay server is untrusted.  It stores opaque payloads
  and cannot forge valid MACs without the circle secret.

## Package structure

```
felund-core/
├── setup.sh                  # One-shot venv + dependency install
├── test_relay.py             # Integration test for the relay API
├── .env.example              # Environment variable reference
├── api/
│   ├── php/
│   │   ├── rendezvous.php    # PHP relay + rendezvous server (recommended)
│   │   ├── .htaccess         # Apache rewrite rules
│   │   └── nginx.conf        # nginx config example
│   ├── rendezvous.py         # FastAPI rendezvous server (presence only)
│   └── requirements.txt
├── chat/
│   ├── felundchat.py         # Entry-point shim
│   ├── requirements.txt
│   └── felundchat/
│       ├── config.py         # Constants (state file path, limits)
│       ├── models.py         # Dataclasses: State, Circle, Peer, Channel, ChatMessage
│       ├── crypto.py         # HMAC MAC generation and SHA-256 helpers
│       ├── invite.py         # felund code encode/decode
│       ├── transport.py      # TCP framing, IP detection
│       ├── persistence.py    # load_state / save_state (JSON)
│       ├── gossip.py         # GossipNode — TCP server + gossip loop
│       ├── channel_sync.py   # Channel event messages and apply logic
│       ├── rendezvous_client.py  # Relay + rendezvous API client
│       ├── chat.py           # Circle/channel management helpers
│       ├── cli.py            # argparse subcommands
│       └── tui/              # Textual panel TUI
└── chat-webclient/           # React + TypeScript browser client
```

## Notes

- The app auto-detects your local IP for peer sharing.
- Keep at least one node running so gossip can propagate messages.
- For cross-network direct connections, ensure the chosen port is reachable
  from the internet.
- Sync debug logs are local-only and off by default.

## Docs

- [Implementation reference](docs/felundchat-reference.md)
- [MVP API spec](docs/mvp-api-spec.md)
- [MVP API quickstart](docs/mvp-api-quickstart.md)
