# Felund Chat Reference

This document captures the current chat implementation details and operational notes for future development.

## Scope

- Project path: `chat/felundchat/`
- Entry point shim: `chat/felundchat.py`
- Purpose: peer-to-peer gossip chat with invite-based circles

## Current Architecture

- `chat/felundchat/models.py`
  - Core dataclasses (`State`, `NodeConfig`, `Circle`, `Peer`, `Channel`, `ChatMessage`)
- `chat/felundchat/persistence.py`
  - Load/save state (`~/.felundchat/state.json`)
  - Message pruning (age + per-circle cap)
- `chat/felundchat/transport.py`
  - TCP JSON-line framing, host:port parsing, local IP detection
- `chat/felundchat/crypto.py`
  - HMAC token + message MAC helpers
- `chat/felundchat/gossip.py`
  - Server, sync protocol, peer/message merge, gossip loop
- `chat/felundchat/chat.py`
  - Interactive host/client flow and CLI chat UX
- `chat/felundchat/channel_sync.py`
  - Signed channel control events (create/join/request/approve/leave/rename)
- `chat/felundchat/cli.py`
  - Command parser and command handlers

## Protocol Notes

- Handshake uses challenge-response auth:
  1. `HELLO`
  2. `CHALLENGE` (nonce)
  3. `HELLO_AUTH` with token = HMAC(secret, node_id|circle_id|nonce)
  4. `WELCOME`
- Sync exchange:
  - `PEERS`
  - `MSGS_HAVE`
  - `MSGS_REQ`
  - `MSGS_SEND`
- Per-message MAC is required before merge.
- Cross-circle injection is blocked (`m.circle_id` must match active sync circle).
- Channel control events are gossiped on a reserved control channel and applied by all peers.

## Interactive UX (Current)

Running `python chat/felundchat.py` launches guided interactive mode.

- Prompts for host/client, display name, port
- Host mode:
  - Creates a circle
  - Prints a single `felund code` containing secret + peer endpoint
- Client mode:
  - Accepts `felund code` (or legacy secret + peer)
- Chat commands:
  - `/help`, `/help <command>`
  - `/circles`, `/switch`
  - `/channels`
  - `/channel create|join|switch|leave|requests|approve ...`
  - `/who [channel]`
  - `/name`, `/name <new_name>`
  - `/inbox`, `/debug`, `/quit`

Display names:

- Display names are persisted locally.
- `/name <new_name>` emits a signed control event and updates peers.
- Rendering prefers latest known name per node when available.

## Debug Logging

- Sync debug logs are **off by default**.
- `/debug` toggles local sync logs on/off for the current process only.
- Logs are emitted in `gossip.py` through a gated `_sync_log(...)` helper.

## Networking and Reliability Notes

- Local bind IP defaults to detected local IP (not `0.0.0.0` in normal flow).
- Peer learning normalizes advertised port with observed remote IP to reduce bad address gossip.
- Read timeout is enforced to avoid hanging handlers.
- Connect/auth/protocol failures are logged only when sync debug is enabled.

## Known Operational Constraints

- State is global per OS user by default:
  - `~/.felundchat/state.json`
- Running multiple clients on one machine under one user shares identity/state.
- Multiple local clients also require unique listen ports.

## Recommended Future Improvements

1. Add configurable state root (`--state-dir` or env var) for multiple local identities.
2. Add optional retry burst on send to reduce occasional delivery lag.
3. Add invite code checksum for typo detection.
4. Add basic integration test harness (2–3 node local simulation).
5. Build a Python GUI frontend (likely PySide6) on top of existing backend modules.

## Quick Troubleshooting

- Error: connection refused
  - Confirm host is running and listening on expected IP:port.
  - Verify firewall/network allows inbound TCP on the chosen port.
  - Verify client invite code points to current host endpoint.
- Symptom: only local messages visible
  - Enable `/debug` and inspect sync failures.
  - Confirm both peers are in same circle and can connect bidirectionally.

## Last Update Context

This reference reflects the implementation that includes:

- interactive host/client flow
- single invite code (`felund code`)
- prompt redraw improvements for incoming messages
- runtime `/debug` sync logging toggle
- handshake replay hardening + message MAC validation
