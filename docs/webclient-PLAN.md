# Felund Chat Web Client Plan (PWA)

## Goal
Build a web-based Felund chat client as a Progressive Web App (PWA) that preserves protocol compatibility where possible and supports internet-capable operation via Rendezvous + Relay.

## Constraints from Current Client
- Existing client is Python and peer-first over direct TCP gossip.
- Browser cannot host raw inbound TCP listeners.
- Browser-friendly transport must be WebSocket/WebRTC (with relay fallback).
- Message integrity/auth should remain equivalent to current HMAC model.

## Architecture Direction

### 1) Web App (this folder)
- Stack: Vite + React + TypeScript.
- PWA: manifest + service worker for install/offline shell.
- Storage: IndexedDB for state, localStorage for small UI prefs.
- Crypto: Web Crypto API (HMAC-SHA256) for invite/auth/message MAC compatibility.

### 2) Protocol/Core Layer
- Pure TS modules for:
  - state models
  - invite encode/decode
  - message signing/verification
  - channel control event helpers
- Keep payload shapes compatible with current chat implementation where practical.

### 3) Connectivity Layer
- MVP transport path:
  1. Rendezvous register + peer lookup
  2. Relay websocket tunnel for sync frames
- Future path:
  - Add WebRTC DataChannel direct path first, relay second.

### 4) UI Layer
- Setup flow: Host or Join by invite code.
- Main chat: circles, channels, message list, compose box.
- Command parity (incremental):
  - `/join`, `/invite`, `/channels`, `/channel create|switch|join|leave`, `/name`, `/who`.

## Milestones

### Milestone 0 — Bootstrap (Now)
- Scaffold React TS PWA project.
- Add core folder structure.
- Implement basic setup + chat shell with local in-memory state.
- Add invite encode/decode and crypto helpers.
- Add no-op transport adapters with clear interfaces.

### Milestone 1 — Local Functional Prototype
- Persist state to IndexedDB.
- Implement channel/circle operations in UI.
- Implement message send/render in one local session.
- Add command parser for a subset of slash commands.

### Milestone 2 — API Integration
- Implement Rendezvous client (register/lookup).
- Implement Relay WS tunnel and frame exchange.
- Sync messages/channels via relay between two browser clients.

### Milestone 3 — Interop + Hardening
- Validate payload compatibility against Python client structures.
- Add migration/import utility for legacy state snapshots.
- Add reconnection/backoff and error handling.
- Add basic e2e tests for two-client sync.

## Acceptance Criteria (MVP Prototype in this chat)
- App builds and runs locally.
- Installable PWA metadata exists.
- User can:
  - choose host/join mode
  - create or join a circle via invite code
  - send/view messages in at least one channel locally
- Core interfaces exist for future rendezvous/relay wiring.

## Open Decisions
- React UI framework: plain CSS modules vs utility CSS.
- IndexedDB library: Dexie vs minimal wrapper.
- Interop strategy: strict byte-for-byte protocol clone vs compatibility shim.

## Immediate Next Steps
1. Scaffold Vite React TS app in this folder.
2. Enable PWA plugin and manifest.
3. Implement core models + invite + crypto modules.
4. Build setup/chat prototype screens.
5. Validate with local build and document run steps.
