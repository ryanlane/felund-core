# Felund Chat Web Client Plan (PWA)

## Goal
Build a web-based Felund chat client as a Progressive Web App (PWA) that preserves protocol compatibility where possible and supports internet-capable operation via Rendezvous + Relay.

## Architecture Direction
- App stack: Vite + React + TypeScript
- PWA: manifest + service worker for install/offline shell
- Storage: IndexedDB for app state
- Crypto: Web Crypto API (HMAC-SHA256) for invite/auth/message MAC compatibility
- Connectivity: Rendezvous + Relay (browser-safe), later optional WebRTC direct path

## Milestones
1. **Bootstrap**: scaffold app + PWA + core modules and chat shell UI
2. **Local Prototype**: circles/channels/messages locally with persistence
3. **API Integration**: rendezvous register/lookup + relay websocket sync
4. **Interop & Hardening**: compatibility checks with Python client payloads

## Prototype Scope (this iteration)
- Setup flow: Host or Join by invite code
- Chat shell: circle/channel list, message log, compose input
- Core modules: models, invite encode/decode, hmac helpers, state store interfaces
- Stub networking adapters for future rendezvous/relay integration

## Acceptance Criteria
- `npm run build` succeeds
- PWA metadata is present
- User can create or join circle and exchange messages in local prototype state
