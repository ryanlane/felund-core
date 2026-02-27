# felund E2EE plan (AES-256-GCM)

This document defines the end-to-end encryption (E2EE) plan for felund chat
clients, using AES-256-GCM. The goal is to encrypt message content across all
transports (TCP gossip, relay HTTP/WS, WebRTC DataChannel), store ciphertext at
rest, and remain backward compatible with legacy plaintext+MAC messages.

## Summary

- Apply E2EE to all transports (TCP gossip, relay HTTP/WS, WebRTC DataChannel).
- Store messages encrypted at rest (ciphertext in local state).
- Dual-read legacy plaintext+MAC; encrypt new writes by default.
- No key rotation in this phase; use the circle secret as the base key.

## Current state (high level)

- Python TUI/CLI creates plaintext `ChatMessage` and attaches HMAC MAC.
- Relay/WebRTC already support AES-GCM envelopes in some paths.
- Transport-level session encryption exists for TCP gossip frames but does not
	define a canonical encrypted message schema.
- Invite codes contain the circle secret; that is the shared key material.

## Target model

### Canonical encrypted message envelope

Standardize an encrypted payload for messages across Python and web:

- `enc`: AES-256-GCM ciphertext (base64 or hex, consistently encoded)
- `nonce`: 96-bit nonce used for GCM
- `key_id`: default `epoch-0` (no rotation yet)
- `schema_version`: message schema version for migrations

AAD (additional authenticated data) must match across clients and include:

- `msg_id`, `circle_id`, `channel_id`, `author_node_id`, `created_ts`

This preserves cross-client compatibility and ensures header fields are
tamper-evident even though only the content is encrypted.

### Storage at rest

Persist ciphertext in local state for both Python and web clients. Decryption
occurs only when rendering in the UI or exporting messages.

### Backward compatibility

- Read both encrypted and plaintext+MAC messages.
- Write encrypted messages by default.
- Keep legacy MAC verification until the majority of clients migrate.

## Implementation plan

1. **Align crypto helpers**
	 - Ensure AES-GCM helpers and AAD fields are identical in:
		 - Python: `chat/felundchat/crypto.py`
		 - Web: `chat-webclient/src/core/crypto.ts`
	 - Use consistent enc/nonce encoding and key derivation rules.

2. **Define encrypted message schema**
	 - Update message models to include `enc`, `nonce`, `key_id`, `schema_version`.
	 - Python: `chat/felundchat/models.py`
	 - Web: `chat-webclient/src/core/models.ts`

3. **Encrypt on write**
	 - Update message creation paths to write encrypted payloads by default:
		 - Python TUI/CLI: `chat/felundchat/chat.py`
		 - Web: `chat-webclient/src/core/state.ts`
	 - Keep legacy MAC fields only for compatibility as needed.

4. **Dual-read transport paths**
	 - TCP gossip: decrypt on merge, accept legacy MAC messages.
	 - Relay push/pull: prefer encrypted envelopes, fall back to legacy.
	 - WebRTC DataChannel: ensure encrypted envelope is the standard format.
	 - Files:
		 - `chat/felundchat/gossip.py`
		 - `chat/felundchat/rendezvous_client.py`
		 - `chat-webclient/src/network/relay.ts`
		 - `chat-webclient/src/network/transport.ts`

5. **Control messages**
	 - Encrypt control-channel (`__control`) events the same way as chat messages.
	 - Update parsing paths to decrypt before applying events:
		 - `chat/felundchat/channel_sync.py`
		 - `chat-webclient/src/core/state.ts`

6. **Docs + threat model**
	 - Update security notes to reflect E2EE behavior and legacy fallback:
		 - `README.md`
		 - `docs/felundchat-reference.md`
		 - `docs/felund-review-plan.md`

## Best practices

- Use 96-bit nonces for AES-GCM and never reuse a nonce with the same key.
- Keep AAD stable and identical across Python and web.
- Validate all decrypted payloads (types, lengths, schema version).
- Treat circle secrets as long-term symmetric keys; do not log them.
- Keep transport-level encryption as defense-in-depth (not a replacement).

## Verification checklist

- Python TUI and web client can exchange encrypted messages via relay.
- TCP gossip between Python nodes still syncs with encrypted payloads.
- WebRTC DataChannel continues to deliver encrypted messages.
- Legacy plaintext messages are still accepted and displayed.
- Stored state contains ciphertext (no plaintext in local storage).

## Out of scope (for now)

- Key rotation / epoch changes.
- Forward secrecy or per-member keys.
- Multi-device key management.
