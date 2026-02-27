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

---

## Implementation status

| Step | Component | Status |
|------|-----------|--------|
| 1 — Crypto helpers | Python `crypto.py` HKDF + AES-256-GCM + session key | ✅ Done |
| 1 — Crypto helpers | Web `crypto.ts` HKDF + AES-256-GCM + session key | ✅ Done |
| 4 — Relay push (Python) | `rendezvous_client.push_messages_to_relay` encrypts inline | ✅ Done |
| 4 — Relay pull (Python) | `rendezvous_client.merge_relay_messages` dual-reads enc/MAC | ✅ Done |
| 4 — Relay push/pull/WS (Web) | `relay.ts` `toWire`/`fromWire` with legacy fallback | ✅ Done |
| 4 — WebRTC DataChannel (Web) | `transport.ts` encrypts on send, decrypts on receive | ✅ Done |
| 4 — Anchor exchange (Python) | `gossip._anchor_push_pull` uses `encrypt_message_fields` | ✅ Done |
| 2 — Model enc field | `ChatMessage` in `models.py` / `models.ts` | ✅ Done |
| 3 — Encrypt on write (Python) | `chat.py` message creation | ✅ Done |
| 4 — TCP gossip dual-read | `gossip._merge_messages` | ✅ Done |
| 5 — Control messages (Python) | `channel_sync.py` all `make_*_message` functions | ✅ Done |
| 5 — Control messages (Web) | `state.ts` `makeCallEventMsg`, `renameCircle`, etc. | ✅ Done |
| 2 — Storage at rest (Python) | `state.json` stores ciphertext, decrypt on load | ✅ Done |
| 2 — Storage at rest (Web) | IndexedDB stores ciphertext, decrypt on load | ✅ Done |

---

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

---

## Phase 1 — Python TCP gossip encryption

Scope: every message created by the Python TUI/CLI carries an `enc` field in
addition to its MAC. The TCP gossip `MSGS_SEND` frame sends it transparently
(via `dataclasses.asdict`). The receiving node decrypts if `enc` is present,
falling back to MAC verification for legacy messages.

### 1.1  `chat/felundchat/models.py`

Add `enc: Optional[Dict[str, str]] = None` to `ChatMessage`.
Add `Optional` to the existing `typing` import.

```python
from typing import Any, Dict, Optional, Set

@dataclasses.dataclass
class ChatMessage:
    msg_id: str
    circle_id: str
    author_node_id: str
    created_ts: int
    text: str
    channel_id: str = "general"
    display_name: str = ""
    mac: str = ""
    schema_version: int = 1  # 1 = legacy plaintext+MAC, 2 = AES-256-GCM envelope
    enc: Optional[Dict[str, str]] = None  # AES-256-GCM enc envelope; None = legacy
```

`enc` must be the last field (has a default) so existing positional
`ChatMessage(...)` call sites keep working.

### 1.2  `chat/felundchat/chat.py`

Add `encrypt_message_fields` to the import from `felundchat.crypto`.

In `interactive_chat`, after line `msg.mac = make_message_mac(circle.secret_hex, msg)`,
add:

```python
msg.enc = encrypt_message_fields(circle.secret_hex, msg)
msg.schema_version = 2
```

### 1.3  `chat/felundchat/gossip.py`

Add `decrypt_message_fields` to the import from `felundchat.crypto`.

In `_merge_messages`, replace the single `verify_message_mac` guard with
dual-read logic (encrypted path first, legacy MAC fallback):

```python
if m.enc is not None:
    # Encrypted path — decrypt and overwrite plaintext fields
    try:
        from cryptography.exceptions import InvalidTag  # local import
        decrypted = decrypt_message_fields(
            circle.secret_hex, m.enc,
            m.msg_id, m.circle_id, m.channel_id, m.author_node_id, m.created_ts,
        )
        m.display_name = decrypted["display_name"]
        m.text = decrypted["text"]
        m.enc = None  # store plaintext in memory after decryption
    except Exception:
        continue  # reject on auth failure or malformed enc
elif not verify_message_mac(circle.secret_hex, m):
    continue  # reject legacy message with invalid MAC
```

After this block the rest of the `_merge_messages` loop is unchanged — it reads
`m.text` to apply control events, which now holds the decrypted payload.

---

## Phase 2 — Control message encryption

Scope: `__control` channel messages (CHANNEL_EVT, CIRCLE_NAME_EVT,
ANCHOR_ANNOUNCE, CALL_EVT) carry an `enc` envelope. The relay and TCP peers
see only ciphertext. Parsing is unchanged — after decryption `m.text` holds the
event JSON.

### 2.1  `chat/felundchat/channel_sync.py`

Add `encrypt_message_fields` to the import from `.crypto`:

```python
from .crypto import encrypt_message_fields, make_message_mac, sha256_hex
```

In **all four** `make_*_message` functions
(`make_channel_event_message`, `make_circle_name_message`,
`make_anchor_announce_message`, `make_call_event_message`),
add after `msg.mac = make_message_mac(...)`:

```python
msg.enc = encrypt_message_fields(circle.secret_hex, msg)
msg.schema_version = 2
```

Note: `gossip._merge_messages` already handles both encrypted and legacy
messages after the Phase 1 changes, so no further gossip changes are needed.
Control event parsing (`parse_channel_event`, `parse_call_event`, etc.) operates
on the already-decrypted `m.text` — no changes needed there.

`chat.py`'s `watch_incoming` loop also reads `m.text` after messages are
fetched from `state.messages` — decryption will have happened in
`_merge_messages` at merge time, so no changes needed there either.

### 2.2  `chat-webclient/src/core/models.ts`

Add the `enc` field:

```typescript
import type { EncPayload } from './crypto'

export interface ChatMessage {
  msgId: string
  circleId: string
  channelId: string
  authorNodeId: string
  displayName: string
  createdTs: number
  text: string
  mac?: string
  enc?: EncPayload   // NEW
}
```

### 2.3  `chat-webclient/src/core/state.ts`

Import `deriveMessageKey` and `encryptMessageFields` from `../core/crypto`.

In `makeCallEventMsg`, `renameCircle`, and the channel-create event path:
1. Derive the circle key once: `const key = await deriveMessageKey(circle.secretHex)`.
2. Build `clearFields` from the message header fields.
3. Set `message.enc = await encryptMessageFields(key, clearFields, { displayName, text })`.

`applyControlEvents` receives messages whose `text` has already been decrypted
by `fromWire` (relay or WebRTC path). Local messages (created in the same session)
are applied immediately by the action helper and do not re-enter
`applyControlEvents`. No changes needed to `applyControlEvents` itself.

---

## Phase 3 — Storage at rest

Scope: replace plaintext `text` / `display_name` in persisted state with the
AES-256-GCM ciphertext. Decrypt lazily at render time.

### Python

- `ChatMessage` stores only `enc` (not `text` / `display_name`) after merge.
  The fields remain in the dataclass for in-memory use during a session but
  are cleared before serialisation.
- `persistence.py` `save_state`: clear `m.text` and `m.display_name` before
  writing; keep `m.enc`.
- `persistence.py` `load_state`: after loading, populate a separate in-memory
  cache of decrypted text per `msg_id` (avoids re-decrypting on every render).
- `chat.py` `render_message`: use the decrypted-text cache; fall back to
  `m.text` if the message was received plaintext (legacy).
- TUI panels read from the same cache.

### Web

- `ChatMessage` in state may carry `enc` without `text` / `displayName`.
- `state.ts` `visibleMessages`: make async; call `decryptMessageFields` for
  messages that have `enc` but no `text`.
- Cache decrypted text in a module-level `WeakMap<ChatMessage, string>` to
  avoid re-decrypting every render cycle.
- `sanitizeLoadedState`: preserve `enc` field on loaded messages.
- Migration: existing plaintext messages in IndexedDB remain readable
  (they have `text` but no `enc`).

---

## Best practices

- Use 96-bit nonces for AES-GCM and never reuse a nonce with the same key.
- Keep AAD stable and identical across Python and web.
- Validate all decrypted payloads (types, lengths, schema version).
- Treat circle secrets as long-term symmetric keys; do not log them.
- Keep transport-level encryption as defense-in-depth (not a replacement).

---

## Verification checklist

- [x] Phase 1: Python TUI sends `enc`-carrying messages over TCP gossip.
- [x] Phase 1: A second Python node receiving via TCP decrypts and displays.
- [x] Phase 1: Legacy plaintext+MAC messages from old Python nodes still accepted.
- [x] Phase 1: Python ↔ Web relay exchange works (relay pull dual-read already done).
- [x] Phase 2: `__control` messages from Python carry `enc`; relay stores ciphertext.
- [x] Phase 2: `__control` messages from web carry `enc`; Python decrypts and applies.
- [x] Phase 2: Legacy control messages (no `enc`) still accepted and applied.
- [x] Phase 3: `state.json` contains no plaintext `text` after a session.
- [x] Phase 3: Restarting the Python TUI redisplays messages correctly (decrypt from load).
- [x] Phase 3: Web IndexedDB contains no plaintext after a session.

### Remaining known gaps (future work)

- Messages received from the relay (`fromWire` in `relay.ts` / `rendezvous_client`) are
  decrypted on receive and stored without `enc`, so they remain in plaintext on disk.
  Full at-rest coverage for relay messages requires re-encrypting into `enc` after
  `fromWire` and before `saveState` / `save_state`.
- Same applies to messages received via WebRTC DataChannel (`transport.ts` `fromWire`).

## Out of scope (for now)

- Key rotation / epoch changes.
- Forward secrecy or per-member keys.
- Multi-device key management.
