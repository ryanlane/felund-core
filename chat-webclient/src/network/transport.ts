/**
 * WebRTC DataChannel transport — Phase 3.
 *
 * Provides direct peer-to-peer message sync between browser nodes using
 * WebRTC DataChannels.  Signaling is done via the relay server's
 * /v1/signal endpoints.
 *
 * Connection lifecycle per peer pair:
 *   signaling → connecting → open → closed | failed
 *
 * Gossip protocol over DataChannel (JSON text messages, no framing needed):
 *   1. Both sides send HELLO with their node_id and a random nonce.
 *   2. On receiving HELLO, each side sends AUTH = HMAC(secret, "dc-auth:" + myNonce + ":" + peerNonce).
 *   3. On receiving AUTH, each side verifies HMAC(secret, "dc-auth:" + peerNonce + ":" + myNonce).
 *   4. After mutual auth, both sides send MSGS_SEND with their recent messages.
 *   5. New messages sent in real-time as MSG_NEW.
 *
 * Offerer/answerer roles: the node with the lexicographically smaller nodeId
 * always creates the WebRTC offer; the other creates the answer.  This ensures
 * at most one connection per pair without a separate negotiation phase.
 *
 * Fallback: if ICE fails or times out the relay WS / HTTP poll continues
 * providing message delivery (they run independently in App.tsx).
 */

import {
  type EncPayload,
  decryptMessageFields,
  deriveMessageKey,
  encryptMessageFields,
  hmacHex,
  randomHex,
  sha256Hex,
} from '../core/crypto'
import type { ChatMessage } from '../core/models'

// ── Constants ─────────────────────────────────────────────────────────────────

const STUN_SERVERS: RTCIceServer[] = [
  { urls: 'stun:stun.l.google.com:19302' },
  { urls: 'stun:stun1.l.google.com:19302' },
]

const ICE_TIMEOUT_MS = 15_000
const SIGNAL_POLL_MS = 2_000
const MAX_SYNC_MSGS = 100
const SIGNAL_BACKOFF_BASE_MS = 500
const SIGNAL_BACKOFF_MAX_MS = 5_000
const CANDIDATE_FLUSH_MS = 250
const CANDIDATE_BATCH = 6

// ── Types ─────────────────────────────────────────────────────────────────────

export type PeerConnectionState = 'signaling' | 'connecting' | 'open' | 'closed' | 'failed'

export interface WebRTCTransportConfig {
  nodeId: string
  circleId: string
  secretHex: string
  /** Already-normalized rendezvous base URL (no trailing slash). */
  rendezvousBase: string
  /** Returns the current local message store for MSGS_SEND sync. */
  getLocalMessages: () => ChatMessage[]
  /** Called with newly received messages from DataChannel peers. */
  onMessages: (msgs: ChatMessage[]) => void
  /** Called whenever the number of open DataChannel connections changes. */
  onPeerCountChange: (count: number) => void
}

// ── Internal wire / frame types ───────────────────────────────────────────────

interface WireMessage {
  msg_id: string
  circle_id: string
  channel_id: string
  author_node_id: string
  created_ts: number
  enc: EncPayload
}

interface HelloFrame {
  type: 'HELLO'
  node_id: string
  circle_hint: string
  nonce: string
}
interface AuthFrame {
  type: 'AUTH'
  auth: string
}
interface MsgsSendFrame {
  type: 'MSGS_SEND'
  messages: WireMessage[]
}
interface MsgNewFrame {
  type: 'MSG_NEW'
  message: WireMessage
}
type GossipFrame = HelloFrame | AuthFrame | MsgsSendFrame | MsgNewFrame

interface SignalData {
  id: number
  session_id: string
  from_node: string
  to_node: string
  type: string
  payload: string
  created_at: number
}

// ── Per-peer session ──────────────────────────────────────────────────────────

interface PeerSession {
  sessionId: string
  peerId: string
  isInitiator: boolean
  state: PeerConnectionState
  pc: RTCPeerConnection
  dc: RTCDataChannel | null
  myNonce: string
  peerNonce: string | null
  authSent: boolean
  authVerified: boolean
  /** Frames buffered before peerNonce is known (AUTH received before HELLO). */
  pendingFrames: GossipFrame[]
  iceTimer: ReturnType<typeof setTimeout> | null
}

// ── Helpers ───────────────────────────────────────────────────────────────────

const circleHintFor = async (circleId: string): Promise<string> =>
  (await sha256Hex(circleId)).slice(0, 16)

const makeSessionId = (a: string, b: string): string => [a, b].sort().join(':')

const toWire = async (key: CryptoKey, m: ChatMessage): Promise<WireMessage> => {
  const clearFields = {
    msgId: m.msgId,
    circleId: m.circleId,
    channelId: m.channelId,
    authorNodeId: m.authorNodeId,
    createdTs: m.createdTs,
  }
  const enc = await encryptMessageFields(key, clearFields, {
    displayName: m.displayName,
    text: m.text,
  })
  return {
    msg_id: m.msgId,
    circle_id: m.circleId,
    channel_id: m.channelId,
    author_node_id: m.authorNodeId,
    created_ts: m.createdTs,
    enc,
  }
}

const fromWire = async (key: CryptoKey, w: WireMessage): Promise<ChatMessage | null> => {
  try {
    const clearFields = {
      msgId: w.msg_id,
      circleId: w.circle_id,
      channelId: w.channel_id,
      authorNodeId: w.author_node_id,
      createdTs: w.created_ts,
    }
    const { displayName, text } = await decryptMessageFields(key, w.enc, clearFields)
    return {
      msgId: w.msg_id,
      circleId: w.circle_id,
      channelId: w.channel_id,
      authorNodeId: w.author_node_id,
      displayName,
      createdTs: w.created_ts,
      text,
    }
  } catch {
    return null
  }
}

// ── WebRTCTransport ───────────────────────────────────────────────────────────

export class WebRTCTransport {
  private config: WebRTCTransportConfig
  private sessions = new Map<string, PeerSession>()
  private lastSignalId = 0
  private pollTimer: ReturnType<typeof setInterval> | null = null
  private stopped = false
  private cachedHint = ''
  private signalingEnabled = true
  private signalBackoffUntil = 0
  private signalBackoffMs = 0
  private candidateQueue: Array<{ sessionId: string; peerId: string; payload: string }> = []
  private candidateFlushTimer: ReturnType<typeof setTimeout> | null = null

  constructor(config: WebRTCTransportConfig) {
    this.config = config
    void circleHintFor(config.circleId).then((h) => {
      this.cachedHint = h
    })
    this.startPolling()
  }

  setSignalingEnabled(enabled: boolean): void {
    if (this.signalingEnabled === enabled) return
    this.signalingEnabled = enabled
    if (enabled) {
      this.startPolling()
    } else {
      this.stopPolling()
    }
  }

  /** Number of currently open DataChannel connections. */
  get openCount(): number {
    return [...this.sessions.values()].filter((s) => s.state === 'open').length
  }

  /**
   * Attempt a WebRTC connection to a peer discovered from rendezvous.
   * Only the node with the lexicographically smaller nodeId creates the offer.
   * Calling this repeatedly for the same peer is safe — it's a no-op if a
   * session already exists.
   */
  async connectToPeer(peerId: string): Promise<void> {
    if (this.stopped) return
    if (!this.signalingEnabled) return
    if (this.config.nodeId >= peerId) return // Other side will initiate

    const sessionId = makeSessionId(this.config.nodeId, peerId)
    if (this.sessions.has(sessionId)) return

    const session = this.createSession(sessionId, peerId, true)
    this.sessions.set(sessionId, session)

    session.dc = session.pc.createDataChannel('felund-gossip', { ordered: true })
    this.wireDataChannel(session)

    try {
      const offer = await session.pc.createOffer()
      await session.pc.setLocalDescription(offer)
      await this.postSignal(sessionId, peerId, 'offer', JSON.stringify(offer))
    } catch (err) {
      console.warn('[webrtc] offer failed:', err)
      this.closeSession(session, 'failed')
    }
  }

  /**
   * Send a single message to all currently-open DataChannel peers.
   * Used for real-time delivery of newly composed messages.
   */
  broadcastMessage(msg: ChatMessage): void {
    if (this.stopped) return
    void this.broadcastAsync(msg)
  }

  /** Tear down all connections and stop polling. */
  destroy(): void {
    this.stopped = true
    this.stopPolling()
    for (const session of this.sessions.values()) {
      if (session.iceTimer !== null) clearTimeout(session.iceTimer)
      try {
        session.pc.close()
      } catch {
        /* ignore */
      }
    }
    this.sessions.clear()
  }

  // ── Private: session creation ───────────────────────────────────────────────

  private createSession(sessionId: string, peerId: string, isInitiator: boolean): PeerSession {
    const pc = new RTCPeerConnection({ iceServers: STUN_SERVERS })
    const session: PeerSession = {
      sessionId,
      peerId,
      isInitiator,
      state: 'signaling',
      pc,
      dc: null,
      myNonce: randomHex(16),
      peerNonce: null,
      authSent: false,
      authVerified: false,
      pendingFrames: [],
      iceTimer: null,
    }

    pc.onicecandidate = (event) => {
      if (this.stopped || !event.candidate) return
      this.candidateQueue.push({
        sessionId,
        peerId,
        payload: JSON.stringify(event.candidate),
      })
      this.scheduleCandidateFlush()
    }

    pc.onconnectionstatechange = () => {
      const cs = pc.connectionState
      if (cs === 'failed' || cs === 'closed') {
        this.closeSession(session, 'failed')
      } else if (cs === 'connected') {
        this.updateState(session, 'open')
      }
    }

    // Answerer receives the DataChannel via this event.
    pc.ondatachannel = (event) => {
      session.dc = event.channel
      this.wireDataChannel(session)
    }

    // ICE timeout — bail out if ICE hasn't completed.
    session.iceTimer = setTimeout(() => {
      if (session.state === 'signaling' || session.state === 'connecting') {
        console.warn('[webrtc] ICE timeout for', peerId.slice(0, 8))
        this.closeSession(session, 'failed')
      }
    }, ICE_TIMEOUT_MS)

    return session
  }

  private wireDataChannel(session: PeerSession): void {
    const dc = session.dc!

    dc.onopen = () => {
      this.updateState(session, 'connecting')
      void this.startHandshake(session)
    }

    dc.onclose = () => this.closeSession(session, 'closed')
    dc.onerror = () => this.closeSession(session, 'failed')

    dc.onmessage = (event: MessageEvent) => {
      void this.handleFrame(session, event.data as string)
    }
  }

  // ── Private: gossip protocol ────────────────────────────────────────────────

  private async startHandshake(session: PeerSession): Promise<void> {
    const hint = this.cachedHint || (await circleHintFor(this.config.circleId))
    this.sendFrame(session, {
      type: 'HELLO',
      node_id: this.config.nodeId,
      circle_hint: hint,
      nonce: session.myNonce,
    })
  }

  private async handleFrame(session: PeerSession, raw: string): Promise<void> {
    let frame: GossipFrame
    try {
      frame = JSON.parse(raw) as GossipFrame
    } catch {
      return
    }

    if (frame.type === 'HELLO') {
      await this.handleHello(session, frame)
    } else if (frame.type === 'AUTH') {
      await this.handleAuth(session, frame)
    } else if (frame.type === 'MSGS_SEND') {
      await this.receiveMsgsSend(frame.messages)
    } else if (frame.type === 'MSG_NEW') {
      await this.receiveSingleMsg(frame.message)
    }
  }

  private async handleHello(session: PeerSession, frame: HelloFrame): Promise<void> {
    session.peerNonce = frame.nonce

    // Send AUTH: HMAC(secret, "dc-auth:" + myNonce + ":" + peerNonce)
    const auth = await hmacHex(
      this.config.secretHex,
      `dc-auth:${session.myNonce}:${frame.nonce}`,
    )
    this.sendFrame(session, { type: 'AUTH', auth })
    session.authSent = true

    // Drain any AUTH frames that arrived before our HELLO set peerNonce.
    if (session.pendingFrames.length > 0) {
      const pending = session.pendingFrames.splice(0)
      for (const pf of pending) {
        await this.handleFrame(session, JSON.stringify(pf))
      }
    }

    if (session.authVerified) {
      await this.onHandshakeComplete(session)
    }
  }

  private async handleAuth(session: PeerSession, frame: AuthFrame): Promise<void> {
    // We can't verify AUTH until we know peerNonce (from their HELLO).
    if (session.peerNonce === null) {
      session.pendingFrames.push(frame)
      return
    }

    // Verify: expected = HMAC(secret, "dc-auth:" + peerNonce + ":" + myNonce)
    const expected = await hmacHex(
      this.config.secretHex,
      `dc-auth:${session.peerNonce}:${session.myNonce}`,
    )
    if (frame.auth !== expected) {
      console.warn('[webrtc] auth failed from', session.peerId.slice(0, 8))
      this.closeSession(session, 'failed')
      return
    }

    session.authVerified = true
    if (session.authSent) {
      await this.onHandshakeComplete(session)
    }
  }

  private async onHandshakeComplete(session: PeerSession): Promise<void> {
    this.updateState(session, 'open')
    await this.syncMessages(session)
  }

  private async syncMessages(session: PeerSession): Promise<void> {
    const msgs = this.config
      .getLocalMessages()
      .filter((m) => m.circleId === this.config.circleId)
      .sort((a, b) => a.createdTs - b.createdTs)
      .slice(-MAX_SYNC_MSGS)

    if (msgs.length === 0) {
      this.sendFrame(session, { type: 'MSGS_SEND', messages: [] })
      return
    }

    const key = await deriveMessageKey(this.config.secretHex)
    const wire = await Promise.all(msgs.map((m) => toWire(key, m)))
    this.sendFrame(session, { type: 'MSGS_SEND', messages: wire })
  }

  private async receiveMsgsSend(messages: WireMessage[]): Promise<void> {
    if (messages.length === 0) return
    const key = await deriveMessageKey(this.config.secretHex)
    const results = await Promise.all(messages.map((w) => fromWire(key, w)))
    const valid = results.filter(
      (m): m is ChatMessage => m !== null && m.circleId === this.config.circleId,
    )
    if (valid.length > 0) this.config.onMessages(valid)
  }

  private async receiveSingleMsg(wire: WireMessage): Promise<void> {
    const key = await deriveMessageKey(this.config.secretHex)
    const msg = await fromWire(key, wire)
    if (msg && msg.circleId === this.config.circleId) {
      this.config.onMessages([msg])
    }
  }

  private async broadcastAsync(msg: ChatMessage): Promise<void> {
    const key = await deriveMessageKey(this.config.secretHex)
    const wire = await toWire(key, msg)
    const frame = JSON.stringify({ type: 'MSG_NEW', message: wire })
    for (const session of this.sessions.values()) {
      if (session.state === 'open' && session.dc?.readyState === 'open') {
        try {
          session.dc.send(frame)
        } catch {
          /* ignore transient send errors */
        }
      }
    }
  }

  private sendFrame(session: PeerSession, frame: object): void {
    if (session.dc?.readyState !== 'open') return
    try {
      session.dc.send(JSON.stringify(frame))
    } catch (err) {
      console.warn('[webrtc] send error:', err)
    }
  }

  // ── Private: state management ───────────────────────────────────────────────

  private updateState(session: PeerSession, state: PeerConnectionState): void {
    if (session.state === state) return
    session.state = state
    this.emitPeerCount()
  }

  private closeSession(session: PeerSession, reason: PeerConnectionState): void {
    if (session.state === 'closed' || session.state === 'failed') return
    session.state = reason
    if (session.iceTimer !== null) {
      clearTimeout(session.iceTimer)
      session.iceTimer = null
    }
    try {
      session.pc.close()
    } catch {
      /* ignore */
    }
    this.sessions.delete(session.sessionId)
    this.emitPeerCount()
  }

  private emitPeerCount(): void {
    this.config.onPeerCountChange(this.openCount)
  }

  // ── Private: signaling ──────────────────────────────────────────────────────

  private async pollSignals(): Promise<void> {
    if (this.stopped || !this.signalingEnabled) return
    try {
      const query = new URLSearchParams({
        to_node_id: this.config.nodeId,
        since_id: String(this.lastSignalId),
      })
      const resp = await fetch(`${this.config.rendezvousBase}/v1/signal?${query}`)
      if (!resp.ok) return
      const data = (await resp.json()) as { ok: boolean; signals?: SignalData[] }
      for (const sig of data.signals ?? []) {
        this.lastSignalId = Math.max(this.lastSignalId, sig.id)
        await this.handleSignal(sig)
      }
    } catch {
      /* network error — retry next poll */
    }
  }

  private async handleSignal(sig: SignalData): Promise<void> {
    const { session_id, from_node, type, payload } = sig

    if (type === 'offer') {
      if (this.sessions.has(session_id)) return
      const session = this.createSession(session_id, from_node, false)
      this.sessions.set(session_id, session)
      try {
        const offer = JSON.parse(payload) as RTCSessionDescriptionInit
        await session.pc.setRemoteDescription(offer)
        const answer = await session.pc.createAnswer()
        await session.pc.setLocalDescription(answer)
        await this.postSignal(session_id, from_node, 'answer', JSON.stringify(answer))
      } catch (err) {
        console.warn('[webrtc] answer error:', err)
        this.closeSession(session, 'failed')
      }
    } else if (type === 'answer') {
      const session = this.sessions.get(session_id)
      if (!session) return
      if (session.pc.signalingState !== 'have-local-offer') {
        console.warn('[webrtc] ignoring answer in state', session.pc.signalingState)
        return
      }
      try {
        await session.pc.setRemoteDescription(
          JSON.parse(payload) as RTCSessionDescriptionInit,
        )
      } catch (err) {
        console.warn('[webrtc] setRemoteDescription (answer) error:', err)
      }
    } else if (type === 'candidate') {
      const session = this.sessions.get(session_id)
      if (!session) return
      try {
        await session.pc.addIceCandidate(
          JSON.parse(payload) as RTCIceCandidateInit,
        )
      } catch {
        /* stale / duplicate candidate — ignore */
      }
    }
  }

  private async postSignal(
    sessionId: string,
    toNode: string,
    type: string,
    payload: string,
  ): Promise<void> {
    if (this.stopped || !this.signalingEnabled) return
    if (type === 'candidate' && Date.now() < this.signalBackoffUntil) return
    const hint = this.cachedHint || (await circleHintFor(this.config.circleId))
    try {
      const resp = await fetch(`${this.config.rendezvousBase}/v1/signal`, {
        method: 'POST',
        headers: { 'content-type': 'application/json' },
        body: JSON.stringify({
          session_id: sessionId,
          from_node_id: this.config.nodeId,
          to_node_id: toNode,
          circle_hint: hint,
          type,
          payload,
          ttl_s: type === 'candidate' ? 60 : 120,
        }),
      })
      if (!resp.ok) {
        if (resp.status === 429) {
          this.signalBackoffMs = this.signalBackoffMs
            ? Math.min(this.signalBackoffMs * 2, SIGNAL_BACKOFF_MAX_MS)
            : SIGNAL_BACKOFF_BASE_MS
          this.signalBackoffUntil = Date.now() + this.signalBackoffMs
        }
        return
      }
      this.signalBackoffMs = 0
      this.signalBackoffUntil = 0
    } catch {
      /* network error — signals are best-effort */
    }
  }

  private scheduleCandidateFlush(): void {
    if (this.candidateFlushTimer !== null) return
    this.candidateFlushTimer = setTimeout(() => {
      this.candidateFlushTimer = null
      void this.flushCandidateQueue()
    }, CANDIDATE_FLUSH_MS)
  }

  private async flushCandidateQueue(): Promise<void> {
    if (this.stopped || !this.signalingEnabled || this.candidateQueue.length === 0) return
    if (Date.now() < this.signalBackoffUntil) {
      this.scheduleCandidateFlush()
      return
    }

    const batch = this.candidateQueue.splice(0, CANDIDATE_BATCH)
    for (const item of batch) {
      await this.postSignal(item.sessionId, item.peerId, 'candidate', item.payload)
    }

    if (this.candidateQueue.length > 0) {
      this.scheduleCandidateFlush()
    }
  }

  private startPolling(): void {
    if (this.pollTimer !== null) return
    this.pollTimer = setInterval(() => void this.pollSignals(), SIGNAL_POLL_MS)
  }

  private stopPolling(): void {
    if (this.pollTimer === null) return
    clearInterval(this.pollTimer)
    this.pollTimer = null
  }
}
