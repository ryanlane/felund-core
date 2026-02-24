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

- [x] Choose stack: `api/relay_ws.py` — Python aiohttp (handles HTTP + WebSocket in one process)
- [x] Implement `POST /v1/register`, `GET /v1/peers`, `DELETE /v1/register` (full presence layer, API-compatible with PHP)
- [x] Implement `POST /v1/messages` — store in SQLite + broadcast to live WS subscribers
- [x] Implement `GET /v1/messages` — retrieve with `since` cursor (same as PHP)
- [x] Implement `WebSocket /v1/relay/ws?circle_hint=&node_id=`
  - On connect: send buffered messages from last 120 s so client catches up immediately
  - On new message push: broadcast `{"t": "MESSAGES", "messages": [...]}` to all room subscribers
  - Keepalive: `{"t": "PING"}` every 15 s; client responds `{"t": "PONG"}`
- [x] SQLite storage (same schema as PHP: `presence` + `relay_messages` tables)
- [x] CORS middleware on all HTTP responses
- [x] Create `api/relay_requirements.txt` (`aiohttp>=3.9`, `aiosqlite>=0.19`)
- [-] nginx/Apache proxy config for WebSocket upgrade — deferred; document in README

### Python client

- [-] Push messages to WS relay from Python TUI — deferred; Python TUI uses direct TCP gossip; relay is primarily for web clients in Phase 2
- [-] WS-based gossip tunnel for Python TUI — deferred to Phase 2.5/3; TCP gossip already works

### Web client

- [x] Add `openRelayWS(base, circleId, secretHex, nodeId, onMessages, onStatus)` to [chat-webclient/src/network/relay.ts](../chat-webclient/src/network/relay.ts)
  - Decrypts incoming messages inline (reuses `fromWire`)
  - Auto-reconnects on close (5 s backoff)
  - Returns cleanup function for React `useEffect`
- [x] Export `WsStatus` type (`'connecting' | 'live' | 'closed'`)
- [x] Add WS subscription `useEffect` to [chat-webclient/src/App.tsx](../chat-webclient/src/App.tsx)
  - One WS connection per circle; re-established when circles or rendezvousBase change
  - New messages merged into state immediately (same path as HTTP pull)
  - Falls back to HTTP poll automatically if WS unavailable
- [x] Surface connection state in header: `◦ live` (WS connected) vs `○ poll` (HTTP-only)
- [x] HTTP poll kept as fallback (still runs every 5 s)

### Verification

- [x] Web client shows `◦ live` when WS relay is reachable
- [x] Send message from Python TUI; web client receives it < 500 ms (via HTTP push → WS broadcast)
- [ ] Kill WS relay; web client falls back to HTTP poll (shows `○ poll`); WS reconnects when relay restarts
- [ ] HTTP poll path works when pointed at PHP relay (WS unavailable — stays `○ poll`)

---

## Phase 2.5 — Peer Anchor System (Hybrid Relay)

### Models & events

- [x] Add capability fields to `NodeConfig` in [chat/felundchat/models.py](../chat/felundchat/models.py)
  - `can_anchor: bool = False`
  - `is_mobile: bool = False`
  - `public_reachable: bool = False` (updated from observed connects)
- [x] Add `ANCHOR_ANNOUNCE` to control event types in [chat/felundchat/channel_sync.py](../chat/felundchat/channel_sync.py)
  - Fields: `node_id`, `capabilities {can_anchor, public_reachable, is_mobile}`, `announced_at`
  - Helpers: `make_anchor_announce_message`, `parse_anchor_announce_event`, `apply_anchor_announce_event`
- [x] Add `AnchorRecord` dataclass to [chat/felundchat/models.py](../chat/felundchat/models.py): `node_id`, `capabilities`, `announced_at`, `last_seen_ts`
- [x] Add `anchor_records: Dict[circle_id, Dict[node_id, AnchorRecord]]` to `State` (persisted)

### Anchor ranking

- [x] Implement `rank_anchor_candidates(state, circle_id) -> List[str]` in [chat/felundchat/anchor.py](../chat/felundchat/anchor.py)
  - Score: `public_reachable*8 + can_anchor*4 + (not is_mobile)*2 + node_id_tiebreak`
- [x] Implement `get_current_anchor(state, circle_id, current, ts) -> Optional[str]` with hysteresis
- [x] Hysteresis state (`_current_anchor`, `_current_anchor_ts`) in `GossipNode` (in-memory)
- [x] Cooldown 60s, staleness threshold 20s per Policy 1

### Anchor storage

- [x] Add `anchor_store: Dict[circle_id, Dict[msg_id, dict]]` in `GossipNode` (encrypted envelopes, in-memory)
- [x] Implement `store_anchor_envelope(anchor_store, circle_id, msg_id, envelope)` in [chat/felundchat/anchor.py](../chat/felundchat/anchor.py)
- [x] Implement `prune_anchor_store(anchor_store, circle_id)` per Policy 3 (24h / 500 msgs / 50 MB)
- [x] Anchor push/pull integrated into gossip TCP protocol (ANCHOR_PUSH / ANCHOR_PUSH_ACK / ANCHOR_PULL / ANCHOR_MSGS frames)
  - Initiator pushes 50 most-recent encrypted envelopes to anchor after normal sync
  - Initiator pulls stored envelopes from anchor using `_anchor_pull_since` cursor

### Routing integration

- [x] `_anchor_push_pull` / `_anchor_serve` methods in [chat/felundchat/gossip.py](../chat/felundchat/gossip.py)
  - `is_initiator=True` + `remote_can_anchor=True` → push + pull
  - `is_initiator=False` + `self.state.node.can_anchor` → serve anchor requests (3s timeout for old clients)
- [x] `can_anchor` advertised in HELLO and WELCOME handshake frames
- [x] Periodically broadcast `ANCHOR_ANNOUNCE` every ~60s if `node.can_anchor == True` (in `gossip_loop`)
- [x] Update [chat-webclient/src/network/rendezvous.ts](../chat-webclient/src/network/rendezvous.ts) to register `can_anchor: false`
- [x] Update [chat/felundchat/rendezvous_client.py](../chat/felundchat/rendezvous_client.py) to include `can_anchor` in capabilities
- [x] Add `--anchor` flag to `run` subcommand in [chat/felundchat/cli.py](../chat/felundchat/cli.py)

### Verification

- [ ] Anchor ranking produces identical result on all nodes given same peer set
- [ ] Anchor failover: primary anchor goes offline → new anchor elected within 20s → sync resumes without hosted relay
- [ ] Anchor store does not grow beyond retention limits
- [ ] `strings` on anchor store memory contains no recoverable plaintext

---

## Phase 3 — WebRTC DataChannel Transport (Web P2P Spine)

### Signaling endpoint

- [x] Add `POST /v1/signal` to [api/relay_ws.py](../api/relay_ws.py)
  - Body: `session_id`, `from_node_id`, `to_node_id`, `circle_hint`, `type` (offer/answer/candidate), `payload`, `ttl_s`
  - Response: `ok`, `server_time`
- [x] Add `GET /v1/signal?to_node_id=&since_id=&session_id=` for polling candidates
- [x] Enforce TTL pruning (max 60s for candidates; 120s for offer/answer) — expired rows deleted on every write
- [x] Rate-limit signal endpoint per `from_node_id` (20 requests / 10 s, in-memory)
- [x] `session_id` is `sorted([nodeA, nodeB]).join(':')` — deterministic, stable, reusable in Phase 4

### Web client transport

- [x] Implement [chat-webclient/src/network/transport.ts](../chat-webclient/src/network/transport.ts) `WebRTCTransport` class
  - Offerer/answerer roles determined by lexicographic node_id comparison (smaller = offerer)
  - Signal polling every 2 s; handles offer / answer / candidate messages
  - ICE candidates trickled via `/v1/signal`; ICE timeout 15 s
- [x] Open `RTCDataChannel` named `felund-gossip` (ordered, offerer creates it; answerer receives via `ondatachannel`)
- [x] Gossip frame protocol over DataChannel (JSON text messages):
  - `HELLO` with node_id, circle_hint, random nonce
  - `AUTH` = `HMAC(circleSecret, "dc-auth:" + myNonce + ":" + peerNonce)` — mutual proof of circle membership
  - `MSGS_SEND` with up to 100 most-recent non-control messages (AES-256-GCM encrypted, same format as relay)
  - `MSG_NEW` for real-time delivery of newly composed messages
- [x] Handle DataChannel `open` / `close` / `error` events
- [x] Connection state machine: `signaling → connecting → open → closed | failed`
- [x] Fallback: if ICE fails/times out, relay WS + HTTP poll continue independently — no action needed
- [x] STUN: `stun.l.google.com:19302` and `stun1.l.google.com:19302`

### App.tsx integration

- [x] `webrtcRef` holds one `WebRTCTransport` per circle (created/destroyed with circles)
- [x] `connectToPeer()` called for each discovered peer during rendezvous poll (every 5 s)
- [x] Messages received via DataChannel merged into state (same path as relay messages)
- [x] `broadcastMessage()` called on new outgoing messages for real-time P2P delivery
- [x] Header shows `◦ p2p(N)` when N direct DataChannels are open; falls back to `◦ live` / `○ poll`

### Python TUI (optional, enables Python↔browser)

- [-] `aiortc` adapter — deferred; browser↔browser DataChannel works; Python↔browser requires aiortc which adds heavy C dependencies

### Verification

- [ ] Browser ↔ browser on different networks: messages sync without relay (confirm in server logs — no relay request)
- [ ] Python TUI ↔ browser: direct DataChannel connection (with aiortc enabled)
- [ ] Symmetric NAT scenario: ICE fails → fallback to anchor/relay → messages still arrive
- [ ] DataChannel disconnect: session cleaned up; peer is re-discoverable next rendezvous poll

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
