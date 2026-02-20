# 🧚felund-core

Simple peer-to-peer group chat over direct connections with gossip-based sync.

## Requirements

- Python 3.10+ (3.11+ recommended)
- Two terminals/devices on the same LAN or reachable network
- Open/forwarded port (default: 9999) if connecting across networks

## Install

No package install is required.

1. Clone the repo
2. Open the project folder
3. Run the chat CLI:

```bash
python chat/felundchat.py
```

State is stored at:

- `~/.felundchat/state.json`

## Quick Start (Interactive)

Run:

```bash
python chat/felundchat.py
```

You will be prompted for:

- Mode: `host` or `client`
- Display name
- Listen port (default `9999`)

### Host flow

1. Choose `host`
2. The app generates a single **felund code** (contains secret + peer address)
3. Share that code with your friend
4. Chat starts immediately in the same terminal

### Client flow

1. Choose `client`
2. Paste the **felund code** from the host
3. Chat starts and syncs with host/peers

## Chat Commands (Interactive Mode)

- `/circles` list joined circles
- `/switch` switch active circle
- `/inbox` show recent messages
- `/debug` toggle local sync debug logs on/off
- `/quit` exit chat

## Manual/Legacy CLI Commands

Initialize local node settings:

```bash
python chat/felundchat.py init --bind 192.168.1.10 --port 9999 --name Ryan
```

Create a circle + show invite code:

```bash
python chat/felundchat.py invite
```

Join using a single code:

```bash
python chat/felundchat.py join --code <felund_code>
```

Legacy join (still supported):

```bash
python chat/felundchat.py join --secret <secret_hex> --peer <host:port>
```

Run gossip service:

```bash
python chat/felundchat.py run
```

Send a message from CLI:

```bash
python chat/felundchat.py send --circle-id <circle_id> "hello world"
```

Show inbox:

```bash
python chat/felundchat.py inbox --circle-id <circle_id> --limit 50
```

List circles/peers:

```bash
python chat/felundchat.py peers
python chat/felundchat.py peers --circle-id <circle_id>
```

## Notes

- The app auto-detects your local IP for peer sharing.
- Keep at least one node online so gossip can propagate messages.
- For cross-network use, ensure the chosen port is reachable.
- Sync debug logs are local-only and off by default.

## Optional API-Assisted Discovery (MVP)

An optional rendezvous API scaffold is included for internet-style peer discovery.

- API service: `api/rendezvous.py`
- Enable in chat client by setting `FELUND_API_BASE`

Linux/macOS:

```bash
export FELUND_API_BASE=http://127.0.0.1:8080
python chat/felundchat.py
```

Windows PowerShell:

```powershell
$env:FELUND_API_BASE = "http://127.0.0.1:8080"
python chat/felundchat.py
```

## Docs

- [Implementation reference](docs/felundchat-reference.md)
- [MVP API spec](docs/mvp-api-spec.md)
- [MVP API quickstart](docs/mvp-api-quickstart.md)
