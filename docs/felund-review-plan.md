# Felund Core: Project Review & Forward Plan

_Review date: 2026-02-23 · Plan v2 revised: 2026-02-23_

---

## Original Vision

> Private, secure multi-user peer-to-peer chat that is fast and operates over UDP. Each peer behaves like a node in a mesh network and acts as a repeater for other clients in the same circle. Message data is **never transmitted over a proxy web service** — the web proxy exists only to find clients behind NATs. A terminal TUI client is the primary interface; a web client mirrors its look and feel for mobile and restricted-OS environments. Clients talk directly to each other whenever possible.

---

## Architecture Overview (Current State)

```
┌─────────────────────────────────────────────────────────────────┐
│  Python TUI (Textual)          Web Browser (React PWA)          │
│  chat/felundchat/              chat-webclient/src/              │
│                                                                 │
│  GossipNode (TCP)              relay.ts (HTTP poll, 5s)         │
│  gossip.py                     transport.ts  ← STUB             │
│       │                              │                          │
│       │  Direct TCP gossip           │  HTTP REST only          │
│       │  (if peer is reachable)      │  (always via relay)      │
│       └──────────────┬───────────────┘                          │
│                      ▼                                          │
│           Rendezvous / Relay Server                             │
│           api/php/rendezvous.php                                │
│           SQLite or MySQL                                       │
│           ┌─────────────────────┐                              │
│           │ presence            │  (rendezvous)                │
│           │ relay_messages      │  (store-and-forward)         │
│           └─────────────────────┘                              │
└─────────────────────────────────────────────────────────────────┘
```

**Transport:** TCP only (JSON-line framing, 16 KB frame limit)
**Auth:** HMAC-SHA256 challenge-response on all peer handshakes
**Encryption:** AES-256-GCM on relay transit; direct TCP path is plaintext-but-authenticated
**Relay role:** Used as both rendezvous (peer discovery) AND message store-and-forward fallback

---

## What Is Working Well

| Area | Notes |
|------|-------|
| **Gossip mesh protocol** | Bidirectional sync, challenge-response HMAC auth, peer propagation, concurrent connections |
| **Cryptography** | HMAC-SHA256 message auth + AES-256-GCM for relay transit; HKDF key derivation |
| **PHP relay server** | Production-ready single-file; SQLite default, MySQL option; automatic TTL pruning |
| **Circle/channel model** | Secret-based membership, 3 access modes (public / key / invite), control-channel gossip |
| **Invite codes** | Clean `felund1.<b64>` format supporting both TCP bootstrap and relay-URL bootstrap |
| **Python TUI** | Textual framework, full slash command suite, gossip loop with rendezvous integration |
| **Web client UI parity** | Monospace TUI aesthetic, keyboard shortcuts (F1/F2/Escape), slash command interface |
| **IndexedDB persistence** | Web client survives page reload; state pruning mirrors Python client |
| **PWA setup** | Vite PWA plugin, service worker, installable on mobile |

---

## Gaps vs. Original Vision

### Gap 1 — Web client has no P2P path (High Priority)

**Current state:** `chat-webclient/src/network/transport.ts` is a stub with no implementation. All web client messages flow through the HTTP relay (5-second polling).

**Vision violation:** "Clients talk directly to each other whenever possible."

**Why it happened:** Browsers cannot open raw TCP/UDP sockets. The `webclient-PLAN.md` acknowledged WebRTC DataChannel as a "future path" but it has not been implemented.

**Fix:** Implement WebRTC DataChannel in `transport.ts`. WebRTC's ICE framework handles UDP hole punching natively, which also solves Gap 2.

---

### Gap 2 — No real NAT traversal (High Priority)

**Current state:** For two Python TUI peers to connect directly, at least one must have a publicly reachable IP:port (port-forwarded router, VPS, etc.). There is no STUN/TURN/ICE logic and no UDP hole punching.

**Vision violation:** Most internet users are behind NAT. Without hole punching, the relay becomes the only usable path for the majority of deployments.

**Fix:** WebRTC ICE (for browser clients) provides UDP hole punching out of the box. For Python TUI clients, a lightweight UDP hole-punch coordination endpoint on the rendezvous server would allow simultaneous UDP sends to each other's observed endpoint.

---

### Gap 3 — Message data flows through relay (High Priority)

**Current state:** The web client routes all messages through `POST /v1/messages` → `GET /v1/messages` (store-and-forward). The Python client also falls back to relay when direct TCP fails. The relay stores AES-GCM ciphertext it cannot decrypt.

**Vision violation:** "Never transmit message data over a proxy web service." The relay is blind due to encryption, but the architectural intent — rendezvous-only relay — is not met.

**Mitigation already in place:** AES-256-GCM encryption means the relay operator cannot read message content. This makes the current state acceptable as a fallback, but it should not be the primary path.

**Fix:** Implement direct P2P connections (WebRTC for browsers, UDP hole punching for Python). Relay becomes a last-resort fallback for symmetric double-NAT scenarios.

---

### Gap 4 — Direct TCP gossip is unencrypted (Medium Priority)

**Current state:** Python TUI gossip over direct TCP sends message frames as plaintext JSON (only HMAC-authenticated). A network observer on the same LAN or at a router can read message content.

**Vision violation:** "Private, secure." HMAC guarantees integrity but not confidentiality on the direct path.

**Context:** AES-256-GCM was recently added for relay transit but was not extended to the direct path.

**Fix:** Derive a per-session symmetric key during the HELLO/WELCOME handshake (HKDF on circle secret + exchanged nonces), then wrap `write_frame`/`read_frame` in the same AES-GCM layer used for relay.

---

### Gap 5 — No real-time relay (Medium Priority)

**Current state:** The relay uses HTTP store-and-forward with a 5-second client poll interval. Message delivery lag via relay is 0–10 seconds.

**Spec gap:** `docs/mvp-api-spec.md` fully specifies `POST /v1/relay/session` and `WebSocket /v1/relay/ws` for bi-directional low-latency relay, but these endpoints are not implemented.

**Fix:** Implement the WebSocket relay endpoint. PHP is poor at long-lived connections; a thin Node.js or Python companion service alongside the PHP rendezvous server is the practical path.

---

### Gap 6 — UDP transport not implemented (Low-Medium Priority)

**Current state:** Transport is TCP-only (`transport.py`, JSON-line framing).

**Vision note:** UDP was described as "optional but potentially faster." Since gossip is already eventually-consistent, UDP loss only causes a missed sync round — not data loss. This simplifies the reliability requirement.

**Fix:** New `udp_transport.py` module with fragmentation/reassembly and AES-GCM frame encryption. Abstract `Transport` interface in `transport.py` to let `gossip.py` use either TCP or UDP.

---

### Gap 7 — API request signing not enforced (Low Priority)

**Current state:** The relay API accepts unauthenticated requests. Any party who knows a `circle_hint` can register fake presence records or inject relay messages (though injected messages would fail HMAC verification by clients).

**Spec gap:** `docs/mvp-api-spec.md` specifies `X-Felund-Signature` headers with HMAC-based request authentication and nonce replay protection. These are not implemented in `api/php/rendezvous.php` or any client.

**Fix:** Implement request signing in both clients and enforce verification in the PHP server.

---

### Gap 8 — FastAPI server is incomplete (Low Priority)

**Current state:** `api/rendezvous.py` implements presence only (no relay), uses in-memory state (lost on restart), and is not suitable for production.

**Fix:** The PHP server is the recommended production path. Either bring the FastAPI server to parity or deprecate/remove it to reduce confusion.

---

## Forward Plan (v2)

### Vision (updated, durable)

Private, secure, multi-user "circle" chat that prefers **direct peer connections**, uses a rendezvous service for **discovery + signaling only**, and allows transport relays as a **last-resort fallback**.

**Non-negotiables:**
- End-to-end encryption of message content on **every path** (direct, relay, WebRTC)
- Relay servers may route/store ciphertext but are not message-aware and cannot decrypt
- Clients do the right thing automatically: **Direct P2P → TURN/relay fallback → store-and-forward last**

**Live comms constraint:**
- Video supported for **1:1 and 1→many only**
- No many-to-many group calls (avoids SFU complexity as a hard dependency)

---

### Architecture: three planes, one identity

**A) Secure Event Plane (Chat + Call signaling)**
- Existing message log replication / anti-entropy gossip model
- Carries `call.*` session control events later
- One canonical encrypted envelope on every path

**B) Peer Anchor Layer (Hybrid relay)**
- Any active circle member can act as a high-availability anchor
- Anchors store-and-forward encrypted envelopes only — never decrypt
- Anchor role rotates automatically via deterministic ranking + hysteresis
- Reduces dependency on the hosted relay to a true last resort

**C) Media Plane (later)**
- WebRTC audio/video
- Uses the event plane for signaling
- Same circle identity and membership governs both

---

### Phase 1 — Canonical Encrypted Envelope Everywhere

**Goal:** Remove the security split-brain. Direct path must be confidential, not just authenticated.

**Why first:** Everything else becomes safer to build once the protocol is stable and privacy claims are true.

**Deliverables:**

1. **Message envelope v2** (single canonical shape)
   - Headers (cleartext, authenticated as AAD): `msg_id, circle_id, channel_id, author_node_id, created_ts, type`
   - Payload: `enc` (AES-GCM ciphertext) + `iv` + optional `compression` flag
   - Schema version field for future migrations

2. **Direct TCP path uses the same AES-GCM protection**
   - Derive per-session key during HELLO/WELCOME handshake:
     `HKDF-SHA256(circle_secret, salt=client_nonce||server_nonce, info="felund-session-v1")`
   - Wrap `write_frame`/`read_frame` with AES-GCM using session key
   - No additional handshake round-trips needed (nonce already in challenge)

3. **Update web client** to use v2 envelope in `src/core/crypto.ts`

**Files to modify:**
- [chat/felundchat/crypto.py](../chat/felundchat/crypto.py) — add `derive_session_key()`
- [chat/felundchat/transport.py](../chat/felundchat/transport.py) — add encrypted frame read/write
- [chat/felundchat/gossip.py](../chat/felundchat/gossip.py) — pass session key after handshake
- [chat-webclient/src/core/crypto.ts](../chat-webclient/src/core/crypto.ts) — align to v2 envelope

**Effort:** Low–Medium. Reuses existing AES-GCM primitives; both clients need envelope alignment.

**Acceptance tests:**
- [ ] Wireshark on a LAN shows **no plaintext chat content** for direct peer sync
- [ ] Bit-flip in ciphertext is rejected (AES-GCM auth tag failure)
- [ ] Relay-stored ciphertext continues to work unchanged

---

### Phase 2 — Real-time Relay Transport (WebSocket Tunnel)

**Goal:** Make the relay fallback feel instant — drop 0–10s polling lag to <500ms.

**Important constraint:** Relay remains a fallback, not the design goal. But it must be good because it handles symmetric NAT and restrictive networks.

**Deliverables:**
- Add a companion WebSocket service (Node.js or Python/uvicorn) alongside the PHP rendezvous
- Implement `POST /v1/relay/session` → returns `relay_url`, `session_id`, `token`
- Implement `WebSocket /v1/relay/ws` per the spec in [docs/mvp-api-spec.md](mvp-api-spec.md)
- Update `chat-webclient/src/network/relay.ts` to prefer WebSocket; fallback to HTTP poll
- Update `chat/felundchat/rendezvous_client.py` to use WebSocket relay when direct TCP fails

**Files to modify:**
- `api/relay_ws.js` (or `relay_ws.py`) — new companion service
- [chat-webclient/src/network/relay.ts](../chat-webclient/src/network/relay.ts)
- [chat/felundchat/rendezvous_client.py](../chat/felundchat/rendezvous_client.py)

**Effort:** Medium. PHP is poor at long-lived connections; the companion process approach is practical.

**Acceptance tests:**
- [ ] Two clients behind NAT exchange messages via relay in <500ms typical
- [ ] Clean reconnect on sleep/wake and mobile network change
- [ ] HTTP poll fallback still works when WebSocket is unavailable

---

### Phase 2.5 — Peer Anchor System (Hybrid Relay)

**Goal:** Let any willing, reachable circle member serve as an anchor — a high-availability peer relay and store-and-forward node — so the hosted relay is only used when no peer anchor is reachable.

This is the design complement to Phase 2: the WebSocket relay becomes a last resort rather than the default fallback.

---

#### What the anchor does (narrow scope)

| Responsibility | In scope | Out of scope |
|----------------|----------|-------------|
| High-availability sync partner | ✅ | Decrypting messages |
| Store-and-forward for offline members | ✅ | Being a "central server" |
| Propagation boost (more frequent sync) | ✅ | Owning the role permanently |
| Signaling mailbox | ❌ (that's the rendezvous) | — |
| TURN media relay | ❌ (that's Phase 5+) | — |

---

#### Anchor election: deterministic ranking + hysteresis

Every node independently ranks peers using the same algorithm, so they converge on the same anchor without a leader-election protocol.

**Rank score inputs (highest weight first):**
1. `public_reachable` — peer has a stable public IP:port (learned from successful connects)
2. `can_anchor` — peer has advertised willingness via capability flag
3. `is_desktop_or_server` — inferred from platform hint (not mobile)
4. `recent_uptime` — fraction of last N sync rounds where peer was reachable
5. `node_id` — stable lexicographic tie-breaker

**Failover with hysteresis:**
- Anchor is considered offline when `now - last_seen_ts > T` (default 20s for chat)
- After switching, a cooldown period (default 60s) prevents flapping back
- No explicit election message needed — ranking is computed locally and deterministically

---

#### Anchor capability advertisement

Anchors publish a `ANCHOR_ANNOUNCE` control event periodically (same `__control` channel as channel events):

```json
{
  "t": "ANCHOR_ANNOUNCE",
  "node_id": "...",
  "capabilities": {
    "can_anchor": true,
    "public_reachable": true,
    "is_mobile": false,
    "bandwidth_hint": "high"
  },
  "announced_at": 1708700000
}
```

Election can function without this announcement — it serves as an explicit opt-in and capability signal.

---

#### Routing fallback chain (full)

```
1. Direct P2P (WebRTC DataChannel or TCP gossip)
2. Current peer anchor (application-level store-and-forward, ciphertext only)
3. Next-best anchor candidate (if current anchor is down)
4. Hosted WebSocket relay (Phase 2)
5. Hosted HTTP store-and-forward (existing fallback)
```

---

#### Anchor storage model

Anchors store **encrypted envelopes only**, indexed by:
- `circle_id` (via circle_hint)
- `channel_id`
- `msg_id`
- `created_ts`

The anchor never needs plaintext. It cannot distinguish message content from garbage.

**Retention policy (anchor-local):**
- Last 24 hours OR last 500 messages per channel (whichever is smaller)
- Max total: configurable, default 50 MB
- Purge on restart is acceptable — anchor storage is opportunistic, not durable

---

#### Deliverables

- Add `ANCHOR_ANNOUNCE` event type to [chat/felundchat/channel_sync.py](../chat/felundchat/channel_sync.py)
- Add `can_anchor`, `is_mobile`, `public_reachable` fields to `NodeConfig` in [chat/felundchat/models.py](../chat/felundchat/models.py)
- Add anchor ranking function to `gossip.py` (or new `anchor.py` module)
- Add per-circle anchor store (in-memory + optional flush to state file)
- Web client: advertise `can_anchor: false` by default (browsers not suitable as anchors)
- Update routing in `rendezvous_client.py` to try anchor before hosted relay

**Files to modify:**
- [chat/felundchat/channel_sync.py](../chat/felundchat/channel_sync.py) — `ANCHOR_ANNOUNCE` event
- [chat/felundchat/models.py](../chat/felundchat/models.py) — capability fields
- [chat/felundchat/gossip.py](../chat/felundchat/gossip.py) — anchor ranking + anchor store
- [chat/felundchat/rendezvous_client.py](../chat/felundchat/rendezvous_client.py) — routing fallback chain
- [chat-webclient/src/network/rendezvous.ts](../chat-webclient/src/network/rendezvous.ts) — register `can_anchor: false`

**Effort:** Medium.

**Acceptance tests:**
- [ ] Three peers on different networks: primary anchor goes offline, remaining peers elect next anchor within 20s, sync resumes without hosted relay
- [ ] Anchor ranking is identical across all nodes for the same peer set
- [ ] Anchor does not store messages once retention limit is hit (oldest pruned)
- [ ] Anchor storage contains only ciphertext (no recoverable plaintext)

---

### Phase 3 — WebRTC DataChannel Transport (Web P2P Spine)

**Goal:** Give the web client a real P2P path and lay the groundwork for video. Closes Gaps 1, 2, and 3 simultaneously.

**Deliverables:**

1. Implement `chat-webclient/src/network/transport.ts` using **WebRTC DataChannel**:
   - Create `RTCPeerConnection` with STUN servers
   - Post SDP offer to `/v1/signal`
   - Remote peer fetches offer, posts answer
   - Exchange ICE candidates (polling or push)
   - Open DataChannel and run existing gossip frame protocol over it

2. Add signaling endpoints to rendezvous server:
   - `POST /v1/signal` — store offers/answers/candidates; short TTL; rate-limited
   - Use **session-based** signaling (not ad-hoc peer-pair) to reuse for video later

3. Fallback chain: **WebRTC DataChannel → peer anchor → WebSocket relay → HTTP store-and-forward**

4. Optional: Python TUI `aiortc` adapter for accepting WebRTC connections from browsers

**Files to modify:**
- [api/php/rendezvous.php](../api/php/rendezvous.php) — add `/v1/signal` signaling endpoint
- [chat-webclient/src/network/transport.ts](../chat-webclient/src/network/transport.ts) — WebRTC implementation
- [chat/felundchat/transport.py](../chat/felundchat/transport.py) — optional aiortc adapter

**Effort:** High. WebRTC signaling and DataChannel is non-trivial. ICE handles UDP hole punching natively.

**Acceptance tests:**
- [ ] Browser ↔ browser chat across different networks without relay when ICE succeeds
- [ ] Python TUI ↔ Browser direct DataChannel connection works (with aiortc)
- [ ] Relay fallback activates automatically when WebRTC ICE fails (symmetric NAT)

---

### Phase 4 — Call Session Control Plane (Video-ready without video)

**Goal:** Add the session abstraction that makes video additive rather than invasive. Ships no media yet.

**Deliverables:**

New encrypted event types carried over the secure event plane:

| Event | Purpose |
|-------|---------|
| `call.create` | Host starts a call session |
| `call.invite` | Host invites one or many |
| `call.join` / `call.leave` | Participant lifecycle |
| `call.end` | Host ends session |
| `call.signal.offer` / `.answer` / `.candidate` | WebRTC signaling over event plane |

**Role model:**
- One `host`
- Many `viewers` (receive-only allowed)
- Optional `viewer_chat` rules (reuse channel or session chat)

**Files to modify:**
- [chat/felundchat/channel_sync.py](../chat/felundchat/channel_sync.py) — add `call.*` event types
- [chat-webclient/src/App.tsx](../chat-webclient/src/App.tsx) — call session UI states

**Effort:** Medium.

**Acceptance tests:**
- [ ] Create call session, invite, join/leave, exchange offers/candidates as encrypted events
- [ ] Only circle members can join; only host can end/kick

---

### Phase 5 — 1:1 Audio/Video (WebRTC Media MVP)

**Goal:** Ship live comms for 1:1.

**Deliverables:**
- Web client: WebRTC audio/video using call session control plane for signaling
- Python client: optional (media can be web-only initially)
- Connection health UI: connecting / reconnecting / failed states

**Acceptance tests:**
- [ ] 1:1 audio works reliably across NAT; TURN fallback if configured
- [ ] 1:1 video works with adaptive resolution on weak networks

---

### Phase 6 — 1→Many Broadcast

**Goal:** Broadcast from one host to multiple viewers.

**Step A — Pure P2P fanout (no SFU):**
- Host establishes N WebRTC peer connections to viewers
- Target maximum: **4–8 viewers** (uplink-constrained)

**Step B — Optional SFU-lite (only if larger audiences needed):**
- Self-hostable SFU
- End-to-end encrypted at message plane
- Media encryption posture depends on complexity tolerance

**Acceptance tests:**
- [ ] Host streams to N viewers; viewers are receive-only by default
- [ ] Host can revoke viewers; call teardown is clean

---

### Phase 7 — API Request Signing (Hardening)

**Goal:** Prevent spoofed presence and relay abuse.

**Approach (per [docs/mvp-api-spec.md](mvp-api-spec.md)):**
- Signing key: `HMAC-SHA256(circle_secret, "api-v1")`
- Canonical payload: `method + path + body_sha256 + timestamp + nonce`
- Signature: `HMAC-SHA256(signing_key, canonical_payload)`
- Headers: `X-Felund-Node`, `X-Felund-Ts`, `X-Felund-Nonce`, `X-Felund-Signature`
- Server enforces 5-minute timestamp window, deduplicates nonces

**Files to modify:**
- [api/php/rendezvous.php](../api/php/rendezvous.php) — enforce signature verification
- [chat/felundchat/rendezvous_client.py](../chat/felundchat/rendezvous_client.py) — add request signing
- [chat-webclient/src/network/rendezvous.ts](../chat-webclient/src/network/rendezvous.ts) — add request signing

**Effort:** Low–Medium.

**Acceptance tests:**
- [ ] Unsigned → 401
- [ ] Replay nonce → 409
- [ ] Signed request succeeds

---

### Phase 8 — UDP Transport for Python (Optional)

**Goal:** Only pursue if there is a proven need after WebRTC exists.

**When it's worth it:**
- You want Python↔Python P2P without `aiortc` dependency
- You want lower overhead on constrained devices
- You accept: fragmentation, reassembly, NAT coordination, dual transport maintenance

**Otherwise:** Prefer "WebRTC everywhere" (including Python via `aiortc`) to avoid maintaining two NAT traversal stacks. Since gossip is eventually-consistent, missed UDP frames result in a skipped sync round, not data loss — so the reliability bar is manageable.

**Files to create/modify (if pursued):**
- `chat/felundchat/udp_transport.py` — new module
- [chat/felundchat/transport.py](../chat/felundchat/transport.py) — abstract Transport interface
- [chat/felundchat/gossip.py](../chat/felundchat/gossip.py) — transport selection
- [api/php/rendezvous.php](../api/php/rendezvous.php) — hole-punch coordination endpoint

---

## Prioritized Roadmap v2

| # | Phase | Primary Impact | Effort | Why here |
|---|-------|---------------|--------|----------|
| 1 | **Canonical encrypted envelope** | Fixes "secure" claim on all paths | Low–Med | Removes biggest contradiction first |
| 2 | **WebSocket relay** | Makes fallback usable / real-time | Med | NAT reality demands good fallback |
| 2.5 | **Peer anchor system** | Reduces hosted relay dependency | Med | Hybrid P2P relay before full WebRTC |
| 3 | **WebRTC DataChannel** | Web P2P + video foundation | High | Browser constraints + future media |
| 4 | **Call control plane** | Video-ready protocol (no media yet) | Med | Keeps video additive, not invasive |
| 5 | **1:1 audio/video** | Ships live comms | High | Concrete product milestone |
| 6 | **1→many broadcast** | Broadcast mode | Med–High | Fits the constraint cleanly |
| 7 | **API request signing** | Abuse resistance | Low–Med | Harden once flows stabilize |
| 8 | **UDP transport** | Optional perf | High | Only if justified post-WebRTC |

---

## Defined Policies

These three policies must be written into code (docstrings or constants) before the anchor system ships.

### Policy 1 — Anchor Election Rule

```
An anchor is the highest-ranked reachable peer in the circle, where rank is determined by:
  1. public_reachable (stable inbound IP:port observed across connects)
  2. can_anchor capability flag (explicit opt-in)
  3. not is_mobile (desktop/server preferred)
  4. recent_uptime_score (fraction of last 20 sync rounds where reachable)
  5. node_id (lexicographic, stable tie-breaker)

Switch to a new anchor only when:
  - current anchor last_seen_ts is > 20s stale (chat), OR
  - current anchor ranked below the next candidate by >= 2 rank positions
After switching, hold for a minimum 60s cooldown before switching again.
```

### Policy 2 — Routing Fallback Chain

```
For each outbound message or sync attempt:
  1. Direct P2P  — WebRTC DataChannel (browser) or TCP gossip (native)
  2. Peer anchor — current highest-ranked anchor, ciphertext only
  3. Next anchor — next candidate if current anchor unreachable
  4. Hosted WS   — WebSocket relay (Phase 2 service)
  5. Hosted HTTP — Store-and-forward poll fallback (existing)

Never skip levels 1–3 without attempting them first.
Always use ciphertext envelopes (v2) on all paths.
```

### Policy 3 — Anchor Retention Limits

```
Per anchor node, per channel:
  - Max age:      24 hours
  - Max messages: 500 (prune oldest first)
  - Max total:    50 MB across all circles/channels

Anchor storage is opportunistic. Loss on restart is acceptable.
Anchors must never decompress or decrypt stored envelopes.
```

---

## Practical Guardrails

- **Message retention:** Cap per-channel message count and/or prune by age. Sync cost grows linearly with history; define limits before adding more peers.
- **Mesh repeating:** Anti-entropy gossip is sufficient correctness baseline. Treat active repeating as a later optimization.
- **One canonical crypto story:** Phases 1+ enforce this. Never return to "encrypted only on some paths."
- **Signaling is session-based:** Design `/v1/signal` to serve both DataChannel and video call sessions from day one.
- **Anchors are volunteers, not infrastructure:** Mobile clients advertise `can_anchor: false` by default. Never require anchor availability for message delivery — hosted relay is always the guaranteed fallback.

---

## Key Files Reference

| File | Purpose |
|------|---------|
| [chat/felundchat/gossip.py](../chat/felundchat/gossip.py) | TCP gossip server + sync protocol |
| [chat/felundchat/transport.py](../chat/felundchat/transport.py) | TCP framing, IP detection |
| [chat/felundchat/crypto.py](../chat/felundchat/crypto.py) | HMAC-SHA256 + AES-256-GCM |
| [chat/felundchat/rendezvous_client.py](../chat/felundchat/rendezvous_client.py) | Relay API integration |
| [chat/felundchat/models.py](../chat/felundchat/models.py) | Core dataclasses |
| [chat/felundchat/channel_sync.py](../chat/felundchat/channel_sync.py) | Channel + control event handling |
| [api/php/rendezvous.php](../api/php/rendezvous.php) | PHP relay server (production) |
| [chat-webclient/src/network/transport.ts](../chat-webclient/src/network/transport.ts) | Web transport stub (needs WebRTC) |
| [chat-webclient/src/network/relay.ts](../chat-webclient/src/network/relay.ts) | Web relay HTTP client |
| [chat-webclient/src/network/rendezvous.ts](../chat-webclient/src/network/rendezvous.ts) | Web rendezvous client |
| [chat-webclient/src/core/crypto.ts](../chat-webclient/src/core/crypto.ts) | Web Crypto API integration |
| [docs/mvp-api-spec.md](mvp-api-spec.md) | Full relay/rendezvous API spec |
| [docs/webclient-PLAN.md](webclient-PLAN.md) | Web client milestones |
