# felund-core

Simple peer-to-peer group chat over direct connections with gossip-based sync.

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

This creates a `.venv/` at the project root and installs all dependencies for both
the chat client and the optional API service.

State is stored at `~/.felundchat/state.json`.

## Running the TUI

```bash
.venv/bin/python chat/felundchat.py
```

This launches the panel-based terminal UI (default). You can also be explicit:

```bash
.venv/bin/python chat/felundchat.py tui
```

### First run — Setup wizard

If no circles exist you are taken through a short wizard:

1. Choose **Host** or **Join**
2. Enter your display name and listen port (default: 9999)
3. **Host**: a felund invite code is generated — share it with your friend
4. **Join**: paste the felund code from the host

### TUI layout

```
┌─ felundchat ─────────────────────────────────────────────────────┐
│ node: a3f8b2 | #general | 2 peers                     ctrl+q=quit│
├──────────────────┬────────────────────────────────────────────────┤
│ Circles          │ [10:32] alice: hey everyone                    │
│ ────────────────  │ [10:33] bob: yo                               │
│ ● mygroup        │ [10:35] you: what's up                        │
│   #general ←     │                                                │
│   #random        │                                                │
│                  │                                                │
├──────────────────┴────────────────────────────────────────────────┤
│ > _                                                               │
└───────────────────────────────────────────────────────────────────┘
```

Click a channel in the sidebar to switch context. The header shows live peer count.

### TUI keyboard shortcuts

| Key | Action |
|-----|--------|
| `ctrl+q` | Quit |
| `ctrl+i` | Show invite code for active circle |
| `escape` | Re-focus input bar |

### TUI slash commands

Type these in the input bar:

| Command | Action |
|---------|--------|
| `/help` | List all commands |
| `/invite` | Show invite code for the active circle |
| `/join <code>` | Join a new circle via felund code |
| `/circles` | List joined circles |
| `/channels` | List channels in the active circle |
| `/channel create <name> [public\|key\|invite]` | Create a channel |
| `/channel join <name> [key]` | Join a channel |
| `/channel switch <name>` | Switch active channel |
| `/channel leave <name>` | Leave a channel |
| `/who [channel]` | Show members in the active (or named) channel |
| `/debug` | Toggle gossip debug log |
| `/quit` | Exit |

## CLI subcommands

All legacy CLI commands still work:

```bash
# Initialize local node settings
.venv/bin/python chat/felundchat.py init --bind 192.168.1.10 --port 9999 --name Alice

# Create a circle + print invite code
.venv/bin/python chat/felundchat.py invite

# Join via single invite code
.venv/bin/python chat/felundchat.py join --code <felund_code>

# Legacy join (still supported)
.venv/bin/python chat/felundchat.py join --secret <hex> --peer <host:port>

# Start gossip service (headless)
.venv/bin/python chat/felundchat.py run

# Send a message from the command line
.venv/bin/python chat/felundchat.py send --circle-id <id> "hello world"

# Show inbox
.venv/bin/python chat/felundchat.py inbox --circle-id <id> --limit 50

# List circles or peers in a circle
.venv/bin/python chat/felundchat.py peers
.venv/bin/python chat/felundchat.py peers --circle-id <id>
```

## Optional API-assisted discovery

An optional rendezvous API is included for internet-style peer discovery.

Start the API:

```bash
.venv/bin/uvicorn api.rendezvous:app --reload
```

Enable in the chat client:

```bash
export FELUND_API_BASE=http://127.0.0.1:8000
.venv/bin/python chat/felundchat.py
```

Windows PowerShell:

```powershell
$env:FELUND_API_BASE = "http://127.0.0.1:8000"
.venv\Scripts\python chat\felundchat.py
```

## Package structure

```
felund-core/
├── setup.sh                  # One-shot venv + dependency install
├── .venv/                    # Shared virtual environment
├── api/
│   ├── rendezvous.py         # FastAPI rendezvous service (optional)
│   └── requirements.txt
└── chat/
    ├── felundchat.py         # Entry-point shim
    ├── requirements.txt
    └── felundchat/
        ├── config.py         # Constants (state file path, limits)
        ├── models.py         # Dataclasses: State, Circle, Peer, Channel, ChatMessage
        ├── crypto.py         # HMAC MAC generation and SHA-256 helpers
        ├── invite.py         # felund code encode/decode
        ├── transport.py      # TCP framing, IP detection
        ├── persistence.py    # load_state / save_state (JSON)
        ├── gossip.py         # GossipNode — TCP server + gossip loop
        ├── channel_sync.py   # Channel event messages and apply logic
        ├── rendezvous_client.py  # Optional API peer discovery
        ├── chat.py           # Circle/channel management helpers
        ├── cli.py            # argparse subcommands
        └── tui.py            # Textual panel TUI
```

## Notes

- The app auto-detects your local IP for peer sharing.
- Keep at least one node running so gossip can propagate messages.
- For cross-network use, ensure the chosen port is reachable from the internet.
- Sync debug logs are local-only and off by default.

## Docs

- [Implementation reference](docs/felundchat-reference.md)
- [MVP API spec](docs/mvp-api-spec.md)
- [MVP API quickstart](docs/mvp-api-quickstart.md)
