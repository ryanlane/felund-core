# Felund Implementation Checklist

_Derived from [felund-review-plan.md](felund-review-plan.md) v2 · 2026-02-23_

Track each item as `- [ ]` (pending), `- [x]` (done), or `- [-]` (skipped/deferred).

---

## Phase 1 — Canonical Encrypted Envelope Everywhere

### Schema

- [x] Define v2 message envelope shape and document it (JSON schema or dataclass comment)
  - Clear headers (AAD): `msg_id`, `circle_id`, `channel_id`, `author_node_id`, `created_ts`, `type`
  - Encrypted payload: `enc` (b64 AES-GCM ciphertext), `iv` (b64 nonce), `alg`, `key_id`
  - Top-level: `schema_version: 2`
- [x] Add `schema_version` field to `ChatMessage` in [chat/felundchat/models.py](../chat/felundchat/models.py)
- [x] Define migration shim: v1 messages (no `schema_version` field) default to `schema_version=1` on load

### Python crypto

- [x] Add `derive_session_key(circle_secret, client_nonce, server_nonce) -> bytes` to [chat/felundchat/crypto.py](../chat/felundchat/crypto.py)
  - `HKDF-SHA256(ikm=circle_secret_bytes, salt=client_nonce||server_nonce, info=b"felund-session-v1")`
- [x] Add `encrypt_frame_bytes(key, plaintext) -> bytes` to [chat/felundchat/crypto.py](../chat/felundchat/crypto.py)
  - AES-256-GCM, random 12-byte nonce, prepend nonce to ciphertext
- [x] Add `decrypt_frame_bytes(key, ciphertext) -> bytes` with GCM auth tag verification
- [-] `encrypt_message_v2` / `decrypt_message_v2` — deferred; existing `encrypt_message_fields` already uses AES-256-GCM; session-level frame encryption covers the direct-path gap

### Python gossip

- [x] Update [chat/felundchat/gossip.py](../chat/felundchat/gossip.py) handshake: client sends `nonce` in `HELLO`; server uses existing `CHALLENGE` nonce as server_nonce
- [x] Derive session key in server after `HELLO_AUTH` verified; signal `enc_ready: true` in `WELCOME`
- [x] Derive session key in client after `WELCOME` with `enc_ready: true`
- [x] Wrap all subsequent `write_frame` / `read_frame` calls with encrypted frame layer via local `_read`/`_write` helpers in `_sync_with_connected_peer`
- [-] `MSGS_SEND` v2 envelope — deferred; session-level encryption already protects message content on the direct TCP path
- [x] Merge logic unchanged — existing `ChatMessage(**md)` construction works for both v1 and v2 (schema_version defaults to 1)

### Web client crypto

- [x] Add `deriveSessionKey(secretHex, clientNonceHex, serverNonceHex)` to [chat-webclient/src/core/crypto.ts](../chat-webclient/src/core/crypto.ts)
- [-] `encryptMessageV2` / `decryptMessageV2` — deferred; existing `encryptMessageFields` / `decryptMessageFields` already use AES-256-GCM; align when WebRTC DataChannel transport (Phase 3) is built
- [-] App.tsx and relay.ts envelope updates — deferred to Phase 3 (no direct-path transport in web client yet)

### Verification

- [ ] Wireshark capture on LAN shows no plaintext `display_name` or `text` in gossip frames
- [ ] Bit-flip in frame ciphertext causes `decrypt_frame_bytes` to raise `InvalidTag` (GCM auth failure)
- [ ] Existing relay-stored messages (v1 `schema_version`) still render correctly after upgrade
- [ ] Python TUI ↔ Python TUI sync works end-to-end with session encryption (`enc_ready: true` logged)

---

## Phase 2 — Real-time Relay Transport (WebSocket Tunnel)

### Service

- [ ] Choose stack for companion WS service: `api/relay_ws.py` (uvicorn + websockets) or `api/relay_ws.js` (Node.js)
- [ ] Implement `POST /v1/relay/session` endpoint
  - Body: `node_id`, `target_node_id`, `circle_hint`, `ttl_s`
  - Response: `session_id`, `token`, `relay_url`, `expires_at`
  - Token: `HMAC-SHA256(server_secret, session_id || node_id)`
- [ ] Implement `WebSocket /v1/relay/ws?token=<token>`
  - Validate token on connect
  - Accept `FRAME` envelopes: `{t, session_id, seq, payload_b64}`
  - Forward payload to the other session participant
  - Send `ACK {t, seq}` back to sender
  - Enforce max frame size (16 KB) and idle timeout (30s)
- [ ] Add session cleanup on disconnect or TTL expiry
- [ ] Add nginx/Apache proxy config for WebSocket upgrade

### Python client

- [ ] Add `open_relay_ws_session(api_base, state, circle_id, target_node_id)` to [chat/felundchat/rendezvous_client.py](../chat/felundchat/rendezvous_client.py)
- [ ] Run existing gossip frame protocol over the WebSocket tunnel (send/recv frames)
- [ ] Implement reconnect with exponential backoff on WS disconnect
- [ ] Fall through to HTTP store-and-forward if WS session creation fails

### Web client

- [ ] Update [chat-webclient/src/network/relay.ts](../chat-webclient/src/network/relay.ts) to attempt WS relay before HTTP poll
- [ ] Implement `RelayWebSocket` class: connect, send frame, receive frame, reconnect
- [ ] Preserve HTTP poll as explicit fallback path
- [ ] Surface connection state in UI (`connecting` / `live` / `polling` / `offline`)

### Verification

- [ ] Two clients exchange a message via WS relay; round-trip latency < 500ms
- [ ] Client sleep/wake re-establishes WS session automatically
- [ ] HTTP poll path still works when WS endpoint is absent (e.g. dev server)
- [ ] Oversized frame (> 16 KB) is rejected by relay

---

## Phase 2.5 — Peer Anchor System (Hybrid Relay)

### Models & events

- [ ] Add capability fields to `NodeConfig` in [chat/felundchat/models.py](../chat/felundchat/models.py)
  - `can_anchor: bool = False`
  - `is_mobile: bool = False`
  - `public_reachable: bool = False` (updated from observed connects)
- [ ] Add `ANCHOR_ANNOUNCE` to control event types in [chat/felundchat/channel_sync.py](../chat/felundchat/channel_sync.py)
  - Fields: `node_id`, `capabilities {can_anchor, public_reachable, is_mobile, bandwidth_hint}`, `announced_at`
- [ ] Add `AnchorRecord` dataclass: `node_id`, `capabilities`, `announced_at`, `last_seen_ts`
- [ ] Add `anchor_records: Dict[circle_id, Dict[node_id, AnchorRecord]]` to `State`

### Anchor ranking

- [ ] Implement `rank_anchor_candidates(state, circle_id) -> List[str]` (returns node_ids, best first)
  - Score: `public_reachable*8 + can_anchor*4 + (not is_mobile)*2 + uptime_score + node_id_tiebreak`
- [ ] Implement `get_current_anchor(state, circle_id) -> Optional[str]`
  - Returns current anchor node_id with hysteresis applied
- [ ] Add hysteresis state: `anchor_selected_ts`, `current_anchor_node_id` per circle
- [ ] Enforce cooldown (60s) and staleness threshold (20s) per Policy 1

### Anchor storage

- [ ] Add `anchor_store: Dict[circle_id, Dict[msg_id, bytes]]` (encrypted envelopes, in-memory)
- [ ] Implement `store_anchor_envelope(state, circle_id, channel_id, msg_id, envelope_bytes)`
- [ ] Implement `prune_anchor_store(state, circle_id)` per Policy 3 (24h / 500 msgs / 50 MB)
- [ ] Expose `GET /anchor/messages?circle_hint=&since=` over gossip TCP (or re-use relay API shape)
- [ ] Expose `POST /anchor/messages` for peers to push envelopes to the anchor

### Routing integration

- [ ] Update routing in [chat/felundchat/rendezvous_client.py](../chat/felundchat/rendezvous_client.py) to follow Policy 2:
  1. Direct P2P gossip
  2. Anchor push/pull
  3. Next anchor if current offline
  4. Hosted WS relay
  5. Hosted HTTP poll
- [ ] Periodically broadcast `ANCHOR_ANNOUNCE` if `node.can_anchor == True`
- [ ] Update [chat-webclient/src/network/rendezvous.ts](../chat-webclient/src/network/rendezvous.ts) to register `can_anchor: false`

### Verification

- [ ] Anchor ranking produces identical result on all nodes given same peer set
- [ ] Anchor failover: primary anchor goes offline → new anchor elected within 20s → sync resumes without hosted relay
- [ ] Anchor store does not grow beyond retention limits
- [ ] `strings` on anchor store memory contains no recoverable plaintext

---

## Phase 3 — WebRTC DataChannel Transport (Web P2P Spine)

### Signaling endpoint

- [ ] Add `POST /v1/signal` to rendezvous server [api/php/rendezvous.php](../api/php/rendezvous.php)
  - Body: `session_id`, `from_node_id`, `to_node_id`, `circle_hint`, `type` (offer/answer/candidate), `payload`, `ttl_s`
  - Response: `ok`, `server_time`
- [ ] Add `GET /v1/signal?session_id=&to_node_id=&since=` for polling candidates
- [ ] Enforce TTL pruning (max 60s for candidates; 120s for offer/answer)
- [ ] Rate-limit signal endpoint per node_id
- [ ] Design `session_id` as stable across both DataChannel and call signaling (reused in Phase 4)

### Web client transport

- [ ] Create [chat-webclient/src/network/transport.ts](../chat-webclient/src/network/transport.ts) `WebRTCTransport` class
  - `createOffer(sessionId, peerId)` → post SDP to `/v1/signal`
  - `waitForAnswer(sessionId)` → poll `/v1/signal` with backoff
  - `addIceCandidate(sessionId, candidate)` → post to `/v1/signal`
  - `pollRemoteCandidates(sessionId)` → poll and apply
- [ ] Open `RTCDataChannel` named `felund-gossip`
- [ ] Run existing gossip frame protocol over DataChannel (same JSON-line frames as TCP)
- [ ] Handle DataChannel `open` / `close` / `error` events
- [ ] Implement connection state machine: `idle → signaling → connecting → open → closed`
- [ ] Integrate fallback: on ICE failure or timeout → try peer anchor → WS relay → HTTP poll
- [ ] Configure STUN servers (at minimum `stun:stun.l.google.com:19302`)

### Python TUI (optional, enables Python↔browser)

- [ ] Add `aiortc` to `chat/requirements.txt` (optional dependency)
- [ ] Implement `WebRTCTransportAdapter` in [chat/felundchat/transport.py](../chat/felundchat/transport.py)
  - Accept incoming DataChannel connections from browsers
  - Send/receive gossip frames over DataChannel
- [ ] Integrate with existing `GossipNode` as an alternate connection type

### Verification

- [ ] Browser ↔ browser on different networks: messages sync without relay (confirm in server logs — no relay request)
- [ ] Python TUI ↔ browser: direct DataChannel connection (with aiortc enabled)
- [ ] Symmetric NAT scenario: ICE fails → fallback to anchor/relay → messages still arrive
- [ ] DataChannel disconnect: automatic reconnect attempt within 10s

---

## Phase 4 — Call Session Control Plane

### Event types

- [ ] Add `call.*` event types to [chat/felundchat/channel_sync.py](../chat/felundchat/channel_sync.py)
  - `call.create {session_id, host_node_id, circle_id, channel_id, created_ts}`
  - `call.invite {session_id, target_node_id}`
  - `call.join {session_id, node_id}`
  - `call.leave {session_id, node_id, reason}`
  - `call.end {session_id, host_node_id}`
  - `call.signal.offer {session_id, from, to, sdp}`
  - `call.signal.answer {session_id, from, to, sdp}`
  - `call.signal.candidate {session_id, from, to, candidate}`
- [ ] All `call.*` events use v2 encrypted envelope (same as chat messages)

### Models

- [ ] Add `CallSession` dataclass to [chat/felundchat/models.py](../chat/felundchat/models.py)
  - `session_id`, `host_node_id`, `circle_id`, `participants: Set[str]`, `viewers: Set[str]`, `state`, `created_ts`
- [ ] Add `active_calls: Dict[session_id, CallSession]` to `State`

### Role enforcement

- [ ] Only `host_node_id` can emit `call.end` or `call.invite`
- [ ] Non-members (not in circle) cannot join
- [ ] Viewers receive media only; cannot emit `call.signal.*` toward non-hosts

### Web client

- [ ] Add call session state to [chat-webclient/src/App.tsx](../chat-webclient/src/App.tsx)
- [ ] Render call status indicator in header (active call in this channel)
- [ ] Show join/leave button for active calls
- [ ] Render participant list in call UI

### Verification

- [ ] Full call lifecycle: create → invite → join → leave → end — all events received by all circle members
- [ ] Non-member node cannot join call (event rejected on receive)
- [ ] Host call.end terminates session for all participants

---

## Phase 5 — 1:1 Audio/Video (WebRTC Media MVP)

### Web client

- [ ] Add `getUserMedia` request (audio first; video as opt-in)
- [ ] Add local media track to existing `RTCPeerConnection` used for DataChannel
- [ ] Handle `ontrack` event for remote stream
- [ ] Render remote video/audio in call UI
- [ ] Add mute/unmute and camera toggle controls
- [ ] Add connection health states: `connecting` / `connected` / `reconnecting` / `failed`
- [ ] Implement adaptive resolution hints via `RTCRtpSender.setParameters`

### TURN support

- [ ] Add TURN server configuration field to settings (optional)
- [ ] Include TURN credentials in `RTCPeerConnection` `iceServers` when configured
- [ ] Document self-hosted TURN options (coturn)

### Python client (optional)

- [ ] Add audio capture/playback via `aiortc` + `pyaudio` (if shipping Python media)

### Verification

- [ ] 1:1 audio call across NAT without TURN (when ICE direct succeeds)
- [ ] 1:1 audio call falling back to TURN (symmetric NAT)
- [ ] 1:1 video call at 480p; adapts resolution on bandwidth throttle
- [ ] Mute/unmute does not drop connection

---

## Phase 6 — 1→Many Broadcast

### Step A — P2P fanout

- [ ] Host creates N `RTCPeerConnection` instances (one per viewer)
- [ ] Limit: enforce max N viewers (default 6; configurable)
- [ ] Viewer connections are receive-only (no `addTrack` from viewer side)
- [ ] Host UI: viewer count indicator, revoke button per viewer
- [ ] Implement `call.revoke {session_id, target_node_id}` event → host closes that peer connection

### Step B — Optional SFU-lite (deferred)

- [ ] Evaluate need: only if P2P fanout proves unreliable at target viewer count
- [ ] Document self-hosted SFU options (mediasoup, Janus) if proceeding

### Verification

- [ ] Host streams to 4 viewers simultaneously; all receive video
- [ ] Host revokes one viewer; that viewer's stream stops; others unaffected
- [ ] Viewer count > max is rejected (new join request refused by host)

---

## Phase 7 — API Request Signing

### Signing implementation

- [ ] Add `sign_request(circle_secret, method, path, body, ts, nonce) -> str` to [chat/felundchat/rendezvous_client.py](../chat/felundchat/rendezvous_client.py)
  - Signing key: `HMAC-SHA256(circle_secret_bytes, b"api-v1")`
  - Canonical: `method.upper() + path + sha256(body) + str(ts) + nonce`
  - Signature: `HMAC-SHA256(signing_key, canonical.encode())`
- [ ] Add `signRequest(circleSecret, method, path, body, ts, nonce)` to [chat-webclient/src/network/rendezvous.ts](../chat-webclient/src/network/rendezvous.ts)
- [ ] Attach headers on all outbound API calls: `X-Felund-Node`, `X-Felund-Ts`, `X-Felund-Nonce`, `X-Felund-Signature`

### Server enforcement

- [ ] Add signature verification to [api/php/rendezvous.php](../api/php/rendezvous.php)
  - Derive signing key from `circle_hint` is NOT sufficient — signature requires the circle secret on the client; server verifies by re-deriving from stored data... **Note:** server doesn't have the secret. Use token-based verification instead: client sends `X-Felund-Auth-Token = HMAC(circle_secret, canonical)`, server can't verify the secret directly but can enforce nonce/timestamp to prevent replays. Revisit verification model.
- [ ] Add nonce deduplication cache (5-minute TTL, `nonce → ts` map in SQLite/memory)
- [ ] Enforce timestamp window: reject requests where `|server_time - X-Felund-Ts| > 300s`
- [ ] Return `401 {code: INVALID_SIGNATURE}` for bad signatures
- [ ] Return `409 {code: NONCE_REPLAY}` for duplicate nonces
- [ ] Return `400 {code: EXPIRED_TIMESTAMP}` for stale timestamps

### Verification

- [ ] Unsigned request → 401
- [ ] Replayed nonce → 409
- [ ] Timestamp > 5 min stale → 400
- [ ] Valid signed request → 200

---

## Phase 8 — UDP Transport for Python (Optional)

> Only begin this phase if Python↔Python NAT traversal via `aiortc` proves insufficient or unwanted.

- [ ] Evaluate: does `aiortc` (Phase 3) cover all Python↔Python NAT traversal needs?
- [ ] If proceeding:
  - [ ] Create `chat/felundchat/udp_transport.py` with `asyncio` datagram protocol
  - [ ] Implement frame fragmentation (max UDP payload 1400 bytes; reassemble by seq)
  - [ ] Implement AES-GCM per-frame encryption (session key from Phase 1)
  - [ ] Abstract `Transport` interface in [chat/felundchat/transport.py](../chat/felundchat/transport.py)
  - [ ] Add UDP hole-punch coordination endpoint to rendezvous server
  - [ ] Update `gossip.py` to select UDP transport when peer endpoint advertises it
  - [ ] Test: Python↔Python behind separate NATs connect without relay

---

## Cross-Cutting Items

### Protocol compatibility

- [ ] Confirm Python TUI v2 envelope is byte-for-byte compatible with web client v2 envelope (same AAD construction, same key derivation)
- [ ] Write one interop test: Python sends v2 message → web client decrypts and renders
- [ ] Document envelope schema version negotiation (what happens when v1 and v2 clients meet)

### Message retention

- [ ] Define and document per-circle retention constants (currently `MAX_MESSAGES_PER_CIRCLE = 1000`, `MESSAGE_MAX_AGE_S = 30 days`)
- [ ] Add per-channel limits (currently per-circle only)
- [ ] Confirm relay server TTL matches client-side pruning window

### FastAPI server

- [ ] Decide: bring `api/rendezvous.py` to full parity OR mark as deprecated
- [ ] If deprecated: add `# DEPRECATED: use api/php/rendezvous.php` header comment and update README

### Documentation

- [ ] Update `README.md` transport/security section after Phase 1 ships
- [ ] Update `docs/mvp-api-spec.md` with `/v1/signal` and `/v1/relay/ws` specs after Phase 2–3
- [ ] Add `ANCHOR_ANNOUNCE` and `call.*` events to `docs/felundchat-reference.md`
