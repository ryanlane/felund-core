/**
 * WebRTC Call Media — Phase 5.
 *
 * Manages 1:1 WebRTC media connections (audio + optional video) for active
 * call sessions created by the Phase 4 call control plane.
 *
 * Each WebRTCCallManager is scoped to one CallSession.  It maintains one
 * RTCPeerConnection per remote participant and handles getUserMedia, track
 * management, signaling (via /v1/signal with 'media-*' type prefixes), and
 * renegotiation for video-on/off.
 *
 * Session ID formula: `media:{callSessionId}:{sorted([nodeA,nodeB]).join(':')}`
 * — avoids collision with Phase 3 DataChannel signal IDs.
 *
 * Signal types: 'media-offer', 'media-answer', 'media-candidate'
 *
 * Offerer/answerer role: lexicographically smaller nodeId creates the offer
 * (same rule as Phase 3 DataChannel), so each pair negotiates at most once.
 *
 * ICE timeout: 20 s.  On timeout, session is closed; audio/video for that
 * peer is unavailable but the call control plane (text) is unaffected.
 */

import { sha256Hex } from '../core/crypto'

// ── Types ─────────────────────────────────────────────────────────────────────

export type CallPeerState = 'connecting' | 'connected' | 'failed'

interface CallPeerSession {
  sessionId: string
  peerId: string
  isInitiator: boolean
  pc: RTCPeerConnection
  state: CallPeerState
  remoteStream: MediaStream
  iceTimer: ReturnType<typeof setTimeout> | null
  /**
   * True while a local offer is outstanding (initial or renegotiation).
   * Cleared by the media-answer handler so that the flag covers the full
   * round-trip, not just the createOffer() call.
   */
  negotiating: boolean
}

export interface CallManagerConfig {
  nodeId: string
  callSessionId: string
  circleId: string
  rendezvousBase: string
  iceServers: RTCIceServer[]
  onRemoteStream: (peerId: string, stream: MediaStream) => void
  onRemoteStreamEnd: (peerId: string) => void
  onPeerStateChange: (peerId: string, state: CallPeerState) => void
}

interface SignalData {
  id: number
  session_id: string
  from_node: string
  to_node: string
  type: string
  payload: string
}

const ICE_TIMEOUT_MS = 20_000
const SIGNAL_POLL_MS = 2_000
const SIGNAL_BACKOFF_BASE_MS = 500
const SIGNAL_BACKOFF_MAX_MS = 5_000
const CANDIDATE_FLUSH_MS = 250
const CANDIDATE_BATCH = 6

// ── WebRTCCallManager ─────────────────────────────────────────────────────────

export class WebRTCCallManager {
  /** The local MediaStream (audio + optional video). null until startMedia(). */
  localStream: MediaStream | null = null

  private sessions = new Map<string, CallPeerSession>()
  private config: CallManagerConfig
  private pollTimer: ReturnType<typeof setInterval> | null = null
  private lastSignalId = 0
  private stopped = false
  /** True while a pollSignals() call is in flight — prevents overlapping polls. */
  private pollInFlight = false
  /** Resolves when startMedia() has finished (success or failure). */
  private mediaReady: Promise<void> = Promise.resolve()
  /** Cached circle_hint (16-hex SHA-256 prefix of circleId). */
  private circleHint: string | null = null
  private signalBackoffUntil = 0
  private signalBackoffMs = 0
  private candidateQueue: Array<{ sessionId: string; peerId: string; payload: string }> = []
  private candidateFlushTimer: ReturnType<typeof setTimeout> | null = null
  /**
   * Candidates that arrived before the session existed or before
   * setRemoteDescription() was called.  Flushed after setRemoteDescription().
   */
  private pendingCandidates = new Map<string, RTCIceCandidateInit[]>()

  constructor(config: CallManagerConfig) {
    this.config = config
    this.pollTimer = setInterval(() => void this.pollSignals(), SIGNAL_POLL_MS)
  }

  // ── Public API ─────────────────────────────────────────────────────────────

  /**
   * Acquire getUserMedia and store the local stream.
   * Must be called before connectToPeer so tracks are ready for addTrack().
   */
  async startMedia(video: boolean): Promise<boolean> {
    this.mediaReady = (async () => {
      try {
        this.localStream = await navigator.mediaDevices.getUserMedia({
          audio: true,
          video,
        })
      } catch (err) {
        console.warn('[call] getUserMedia failed:', err)
        // Fallback: try audio-only if video was requested and failed
        if (video) {
          try {
            this.localStream = await navigator.mediaDevices.getUserMedia({ audio: true })
          } catch (err2) {
            console.warn('[call] getUserMedia (audio-only fallback) failed:', err2)
          }
        }
      }
    })()
    await this.mediaReady
    return (this.localStream?.getAudioTracks().length ?? 0) > 0
  }

  /**
   * Initiate a media connection to a call participant.
   * Only the node with the lexicographically smaller nodeId creates the offer.
   * Calling this repeatedly for the same peer is a no-op.
   */
  async connectToPeer(peerId: string): Promise<void> {
    if (this.stopped) return
    const sessionId = this.makeSessionId(peerId)
    if (this.sessions.has(sessionId)) return

    const isInitiator = this.config.nodeId < peerId
    if (!isInitiator) return // Answerer: wait for offer via pollSignals

    // Wait for getUserMedia to complete so tracks are available for the offer.
    await this.mediaReady
    if (this.stopped || this.sessions.has(sessionId)) return

    const session = this.createPeerSession(sessionId, peerId, true)
    this.sessions.set(sessionId, session)

    // Block onnegotiationneeded from firing renegotiate() while the initial
    // offer is in flight.  addTrack() queues onnegotiationneeded as a
    // macrotask; without this guard, the macrotask races our own
    // createOffer(), producing two concurrent setLocalDescription() calls
    // with mismatched SDPs and an immediate 'failed' connection.
    // negotiating stays true until the media-answer handler clears it.
    session.negotiating = true

    // Add all local tracks before creating the offer
    if (this.localStream) {
      for (const track of this.localStream.getTracks()) {
        session.pc.addTrack(track, this.localStream)
      }
    }

    try {
      const offer = await session.pc.createOffer()
      await session.pc.setLocalDescription(offer)
      await this.postSignal(sessionId, peerId, 'media-offer', JSON.stringify(offer))
    } catch (err) {
      console.warn('[call] offer failed:', err)
      this.closeSession(session, 'failed')
    }
  }

  /**
   * Toggle local audio mute/unmute without renegotiating.
   * Pass muted=true to silence outgoing audio.
   */
  muteAudio(muted: boolean): void {
    if (!this.localStream) return
    for (const track of this.localStream.getAudioTracks()) {
      track.enabled = !muted
    }
  }

  /**
   * Enable or disable the local video track.
   * If enabling and no video track exists yet, requests getUserMedia({ video })
   * then adds the track to all existing PeerConnections, triggering renegotiation.
   * If disabling, sets track.enabled = false (no renegotiation needed).
   */
  async enableVideo(enabled: boolean): Promise<void> {
    if (!enabled) {
      // Disable existing video tracks without renegotiation
      this.localStream?.getVideoTracks().forEach((t) => {
        t.enabled = false
      })
      return
    }

    // Check if we already have a video track
    const existingVideo = this.localStream?.getVideoTracks()[0]
    if (existingVideo) {
      existingVideo.enabled = true
      return
    }

    // Acquire video track
    let videoStream: MediaStream | null = null
    try {
      videoStream = await navigator.mediaDevices.getUserMedia({ video: true })
    } catch (err) {
      console.warn('[call] getUserMedia (video) failed:', err)
      return
    }

    const videoTrack = videoStream.getVideoTracks()[0]
    if (!videoTrack) return

    // Merge into localStream
    if (this.localStream) {
      this.localStream.addTrack(videoTrack)
    } else {
      this.localStream = videoStream
    }

    // Add the track to all open PeerConnections — fires onnegotiationneeded
    for (const session of this.sessions.values()) {
      if (session.state !== 'failed') {
        session.pc.addTrack(videoTrack, this.localStream!)
      }
    }
  }

  /** Stop all media tracks, close all PeerConnections, stop signaling poll. */
  destroy(): void {
    this.stopped = true
    if (this.pollTimer !== null) {
      clearInterval(this.pollTimer)
      this.pollTimer = null
    }
    if (this.candidateFlushTimer !== null) {
      clearTimeout(this.candidateFlushTimer)
      this.candidateFlushTimer = null
    }
    for (const session of this.sessions.values()) {
      this.cleanupSession(session)
    }
    this.sessions.clear()
    this.pendingCandidates.clear()
    if (this.localStream) {
      for (const track of this.localStream.getTracks()) track.stop()
      this.localStream = null
    }
  }

  // ── Private: session lifecycle ─────────────────────────────────────────────

  private makeSessionId(peerId: string): string {
    const pair = [this.config.nodeId, peerId].sort().join(':')
    return `media:${this.config.callSessionId}:${pair}`
  }

  private createPeerSession(sessionId: string, peerId: string, isInitiator: boolean): CallPeerSession {
    const pc = new RTCPeerConnection({ iceServers: this.config.iceServers })
    const remoteStream = new MediaStream()

    const session: CallPeerSession = {
      sessionId,
      peerId,
      isInitiator,
      pc,
      state: 'connecting',
      remoteStream,
      iceTimer: null,
      negotiating: false,
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
      if (this.stopped) return
      const cs = pc.connectionState
      if (cs === 'connected') {
        this.updateState(session, 'connected')
      } else if (cs === 'failed' || cs === 'closed') {
        this.closeSession(session, 'failed')
      }
    }

    // addTrack() is idempotent on MediaStream per spec — no duplicate check needed.
    pc.ontrack = (event) => {
      if (this.stopped) return
      const tracks = event.streams[0] ? event.streams[0].getTracks() : [event.track]
      for (const track of tracks) remoteStream.addTrack(track)
      this.config.onRemoteStream(peerId, remoteStream)
    }

    // Renegotiation (e.g., video track added after initial offer).
    // Only the initiator ever sends re-offers; the answerer responds.
    pc.onnegotiationneeded = () => {
      if (!isInitiator || session.negotiating || this.stopped) return
      void this.renegotiate(session)
    }

    // ICE timeout
    session.iceTimer = setTimeout(() => {
      if (session.state === 'connecting') {
        console.warn('[call] ICE timeout for', peerId.slice(0, 8))
        this.closeSession(session, 'failed')
      }
    }, ICE_TIMEOUT_MS)

    return session
  }

  private async renegotiate(session: CallPeerSession): Promise<void> {
    if (session.negotiating || this.stopped) return
    session.negotiating = true
    try {
      const offer = await session.pc.createOffer()
      await session.pc.setLocalDescription(offer)
      await this.postSignal(session.sessionId, session.peerId, 'media-offer', JSON.stringify(offer))
      // negotiating stays true until the media-answer arrives and clears it,
      // preventing a second onnegotiationneeded from starting a concurrent offer.
    } catch (err) {
      console.warn('[call] renegotiation offer failed:', err)
      session.negotiating = false // clear on error so future renegotiations can fire
    }
  }

  private updateState(session: CallPeerSession, state: CallPeerState): void {
    if (session.state === state) return
    session.state = state
    this.config.onPeerStateChange(session.peerId, state)
  }

  private closeSession(session: CallPeerSession, reason: CallPeerState): void {
    if (session.state === 'failed') return
    this.updateState(session, reason)
    this.cleanupSession(session)
    this.sessions.delete(session.sessionId)
    this.pendingCandidates.delete(session.sessionId)
    this.config.onRemoteStreamEnd(session.peerId)
  }

  private cleanupSession(session: CallPeerSession): void {
    if (session.iceTimer !== null) {
      clearTimeout(session.iceTimer)
      session.iceTimer = null
    }
    try {
      session.pc.close()
    } catch {
      /* ignore */
    }
  }

  private async flushPendingCandidates(session: CallPeerSession): Promise<void> {
    const pending = this.pendingCandidates.get(session.sessionId)
    if (!pending) return
    this.pendingCandidates.delete(session.sessionId)
    for (const candidate of pending) {
      try {
        await session.pc.addIceCandidate(candidate)
      } catch {
        /* stale / duplicate — ignore */
      }
    }
  }

  // ── Private: signaling ─────────────────────────────────────────────────────

  private async pollSignals(): Promise<void> {
    if (this.stopped || this.pollInFlight) return
    this.pollInFlight = true
    try {
      const query = new URLSearchParams({
        to_node_id: this.config.nodeId,
        since_id: String(this.lastSignalId),
      })
      const resp = await fetch(`${this.config.rendezvousBase}/v1/signal?${query}`)
      if (!resp.ok) return
      const data = (await resp.json()) as { ok: boolean; signals?: SignalData[] }
      for (const sig of data.signals ?? []) {
        // Advance past ALL signals (including Phase-3 offer/answer/candidate)
        // so Phase-3 churn can't push Phase-5 signals beyond the relay's
        // LIMIT 50 page and make them invisible forever.
        this.lastSignalId = Math.max(this.lastSignalId, sig.id)
        if (!sig.type.startsWith('media-')) continue
        await this.handleSignal(sig)
      }
    } catch {
      /* network error — retry next poll */
    } finally {
      this.pollInFlight = false
    }
  }

  private async handleSignal(sig: SignalData): Promise<void> {
    const { session_id, from_node, type, payload } = sig

    // Only process signals for peer sessions this node owns or can answer
    const expectedSessionId = this.makeSessionId(from_node)
    if (session_id !== expectedSessionId) return

    if (type === 'media-offer') {
      const existingSession = this.sessions.get(session_id)

      if (!existingSession) {
        // First offer: create the answerer session.
        await this.mediaReady
        if (this.sessions.has(session_id)) return // created while awaiting mediaReady
        const session = this.createPeerSession(session_id, from_node, false)
        this.sessions.set(session_id, session)

        if (this.localStream) {
          for (const track of this.localStream.getTracks()) {
            session.pc.addTrack(track, this.localStream)
          }
        }

        try {
          await session.pc.setRemoteDescription(JSON.parse(payload) as RTCSessionDescriptionInit)
          const answer = await session.pc.createAnswer()
          await session.pc.setLocalDescription(answer)
          await this.postSignal(session_id, from_node, 'media-answer', JSON.stringify(answer))
          await this.flushPendingCandidates(session)
        } catch (err) {
          console.warn('[call] answer failed:', err)
          this.closeSession(session, 'failed')
        }
      } else {
        // Re-offer: renegotiation from the initiator (e.g., they added video).
        // Skip if a local negotiation is already in flight to avoid SDP glare.
        if (existingSession.negotiating) return
        try {
          await existingSession.pc.setRemoteDescription(
            JSON.parse(payload) as RTCSessionDescriptionInit,
          )
          const answer = await existingSession.pc.createAnswer()
          await existingSession.pc.setLocalDescription(answer)
          await this.postSignal(session_id, from_node, 'media-answer', JSON.stringify(answer))
          await this.flushPendingCandidates(existingSession)
        } catch (err) {
          console.warn('[call] re-answer failed:', err)
        }
      }
    } else if (type === 'media-answer') {
      const session = this.sessions.get(session_id)
      if (!session) return
      try {
        await session.pc.setRemoteDescription(JSON.parse(payload) as RTCSessionDescriptionInit)
        session.negotiating = false
        await this.flushPendingCandidates(session)
      } catch (err) {
        console.warn('[call] setRemoteDescription (answer) failed:', err)
      }
    } else if (type === 'media-candidate') {
      const candidate = JSON.parse(payload) as RTCIceCandidateInit
      const session = this.sessions.get(session_id)
      if (!session || !session.pc.remoteDescription) {
        // Queue: session not yet created, or setRemoteDescription hasn't run yet
        const queue = this.pendingCandidates.get(session_id) ?? []
        queue.push(candidate)
        this.pendingCandidates.set(session_id, queue)
        return
      }
      try {
        await session.pc.addIceCandidate(candidate)
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
    if (this.stopped) return
    if (type === 'media-candidate' && Date.now() < this.signalBackoffUntil) return
    try {
      if (!this.circleHint) {
        this.circleHint = (await sha256Hex(this.config.circleId)).slice(0, 16)
      }
      const resp = await fetch(`${this.config.rendezvousBase}/v1/signal`, {
        method: 'POST',
        headers: { 'content-type': 'application/json' },
        body: JSON.stringify({
          session_id: sessionId,
          from_node_id: this.config.nodeId,
          to_node_id: toNode,
          circle_hint: this.circleHint,
          type,
          payload,
          ttl_s: type === 'media-candidate' ? 60 : 120,
        }),
      })
      if (!resp.ok) {
        if (resp.status === 429) {
          this.signalBackoffMs = this.signalBackoffMs
            ? Math.min(this.signalBackoffMs * 2, SIGNAL_BACKOFF_MAX_MS)
            : SIGNAL_BACKOFF_BASE_MS
          this.signalBackoffUntil = Date.now() + this.signalBackoffMs
        }
        const body = await resp.text()
        console.warn('[call] postSignal failed', resp.status, type, body.slice(0, 80))
        return
      }
      this.signalBackoffMs = 0
      this.signalBackoffUntil = 0
    } catch (err) {
      console.warn('[call] postSignal error:', err)
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
    if (this.stopped || this.candidateQueue.length === 0) return
    if (Date.now() < this.signalBackoffUntil) {
      this.scheduleCandidateFlush()
      return
    }

    const batch = this.candidateQueue.splice(0, CANDIDATE_BATCH)
    for (const item of batch) {
      await this.postSignal(item.sessionId, item.peerId, 'media-candidate', item.payload)
    }

    if (this.candidateQueue.length > 0) {
      this.scheduleCandidateFlush()
    }
  }
}
