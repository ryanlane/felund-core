# Felund MVP API Spec (Rendezvous + Relay)

## Purpose

Define the lightest-weight internet-capable service layer for Felund chat, while keeping peer-to-peer first.

Goals:
- Allow peers on different networks (NAT, mobile, CGNAT) to find each other
- Keep direct peer connectivity as first choice
- Use relay only when direct connectivity fails
- Keep service stateless or near-stateless where practical

Non-goals (MVP):
- Full WebRTC ICE implementation
- Anonymous networking
- Long-term message storage on service

## Architecture (MVP)

Components:
- Rendezvous API: peer registration and lookup
- Relay API: temporary frame forwarding fallback
- Existing Felund protocol: unchanged message sync frames over direct socket or relay tunnel

Client connection strategy:
1. Register presence with Rendezvous
2. Lookup peers for circle
3. Attempt direct connect to candidates
4. If direct fails, open relay session and continue sync via relay

## Data Model

Common terms:
- node_id: stable local node identity
- circle_id: chat circle identifier
- endpoint: host, port, transport tuple
- ttl_s: registration lifetime in seconds

Presence record:
- node_id: string
- circle_hint: string (sha256(circle_id) or circle hash hint)
- endpoints: list of endpoint objects
- capabilities: object
- observed_at: unix timestamp
- expires_at: unix timestamp
- sig: signature/HMAC for integrity

Endpoint object:
- transport: tcp or ws
- host: string
- port: integer
- family: ipv4 or ipv6
- nat: unknown, open, restricted, symmetric

## Security Model (MVP)

- All API traffic over HTTPS
- Registration requests signed by client using circle secret derived key
- Service stores only circle hint, not plaintext circle secret
- Relay is blind transport only; payload remains end-to-end authenticated by existing Felund message MAC
- Replay prevention with nonce + timestamp windows on API requests

Request signing (simple):
- key = HMAC-SHA256(circle_secret, "api-v1")
- canonical_payload = method + path + body + ts + nonce
- auth = HMAC-SHA256(key, canonical_payload)

Headers:
- X-Felund-Node
- X-Felund-Ts
- X-Felund-Nonce
- X-Felund-Signature

## API Endpoints

Base path:
- /v1

### 1) POST /v1/register

Purpose:
- Register or refresh a node presence record

Request body:
- node_id: string
- circle_hint: string
- endpoints: endpoint[]
- capabilities:
  - relay: boolean
  - transport: ["tcp", "ws"]
- ttl_s: integer (recommended 60 to 180)

Response 200:
- ok: true
- server_time: unix timestamp
- expires_at: unix timestamp
- observed_endpoint:
  - host: string
  - port: integer

Behavior:
- Upsert by node_id + circle_hint
- Clamp ttl_s to server max
- Return observed endpoint so clients can improve address advertisement

### 2) GET /v1/peers?circle_hint=...&limit=...

Purpose:
- Return currently live peers for a circle

Response 200:
- ok: true
- peers: [
  - node_id: string
  - endpoints: endpoint[]
  - capabilities: object
  - observed_at: unix timestamp
]

Behavior:
- Exclude expired records
- Exclude caller node_id if provided via header
- Default limit 50

### 3) DELETE /v1/register

Purpose:
- Explicitly unregister on shutdown

Request body:
- node_id: string
- circle_hint: string

Response 200:
- ok: true

### 4) POST /v1/relay/session

Purpose:
- Request relay fallback when direct path unavailable

Request body:
- node_id: string
- target_node_id: string
- circle_hint: string
- ttl_s: integer

Response 200:
- ok: true
- relay_url: string
- session_id: string
- token: string
- expires_at: unix timestamp

Behavior:
- Short-lived session (30 to 120 seconds)
- Token scoped to session_id + node_id

### 5) WebSocket /v1/relay/ws

Purpose:
- Bi-directional frame relay between two authorized peers

Auth:
- Bearer token from relay/session response

Envelope:
- t: "FRAME"
- session_id: string
- seq: integer
- payload_b64: string

Ack envelope:
- t: "ACK"
- seq: integer

Behavior:
- Relay does not parse Felund payload
- Enforce max frame size and idle timeout

### 6) GET /v1/health

Purpose:
- Operational health check

Response 200:
- ok: true
- version: string
- time: unix timestamp

## Error Model

Error body:
- ok: false
- code: string
- message: string
- retryable: boolean

Common codes:
- INVALID_SIGNATURE
- NONCE_REPLAY
- EXPIRED_TIMESTAMP
- NOT_FOUND
- RATE_LIMITED
- SESSION_EXPIRED
- PAYLOAD_TOO_LARGE

HTTP status guidance:
- 400 validation
- 401 auth/signature
- 404 missing registration/session
- 409 nonce replay
- 429 rate limit
- 500 internal

## Timing and Retry Guidance

- Register heartbeat every ttl_s / 2
- Peer lookup every 5 to 15 seconds while active
- Relay session renew at 70% of ttl
- Exponential backoff with jitter for API failures

Suggested defaults:
- register ttl_s: 120
- lookup interval: 8
- relay idle timeout: 30
- max relay frame: 16 KB

## Minimal Implementation Notes

Service stack candidates:
- FastAPI + Uvicorn
- In-memory store for MVP (with optional Redis later)

State needed for MVP:
- presence map keyed by circle_hint -> node_id
- nonce cache for replay protection (short TTL)
- relay session map with expiry

Rate limiting:
- Per IP + per node_id token bucket

Logging:
- request_id, node_id, circle_hint prefix, status, latency
- avoid logging secrets or message payloads

## Client Integration Plan

Phase 1 (rendezvous only):
- Add periodic register + peer lookup
- Keep direct TCP connect as now

Phase 2 (relay fallback):
- If direct connect fails N times, request relay session
- Run existing sync frames over relay websocket tunnel

Phase 3 (hardening):
- Multi-region rendezvous endpoints
- Signed server responses
- Optional persistence and metrics dashboard

## Open Decisions

- circle_hint format (hash algorithm and truncation length)
- whether relay should support store-and-forward for briefly offline peers
- auth key derivation exact format and rotation strategy
- single service vs federated rendezvous nodes in MVP

## Acceptance Criteria for MVP

- Two peers on different consumer networks can discover each other through API
- At least one connectivity path works: direct or relay
- Message sync works unchanged over selected path
- No plaintext circle secret or message body stored on service
- Basic replay protection and rate limiting are active
