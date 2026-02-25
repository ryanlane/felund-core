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
  /** Guard: prevents re-entrant renegotiation while one is already in flight. */
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

// ── WebRTCCallManager ─────────────────────────────────────────────────────────

export class WebRTCCallManager {
  /** The local MediaStream (audio + optional video). null until startMedia(). */
  localStream: MediaStream | null = null

  private sessions = new Map<string, CallPeerSession>()
  private config: CallManagerConfig
  private pollTimer: ReturnType<typeof setInterval> | null = null
  private lastSignalId = 0
  private stopped = false
  /** Resolves when startMedia() has finished (success or failure). */
  private mediaReady: Promise<void> = Promise.resolve()
  /** Cached circle_hint (16-hex SHA-256 prefix of circleId). */
  private circleHint: string | null = null

  constructor(config: CallManagerConfig) {
    this.config = config
    this.pollTimer = setInterval(() => void this.pollSignals(), SIGNAL_POLL_MS)
  }

  // ── Public API ─────────────────────────────────────────────────────────────

  /**
   * Acquire getUserMedia and store the local stream.
   * Must be called before connectToPeer so tracks are ready for addTrack().
   */
  async startMedia(video: boolean): Promise<void> {
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
    // macrotask; if we didn't guard here, the macrotask could race with our
    // own createOffer(), causing two concurrent setLocalDescription() calls
    // with mismatched SDPs and a 'failed' connection.  negotiating stays true
    // until the media-answer handler clears it.
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
    for (const session of this.sessions.values()) {
      this.cleanupSession(session)
    }
    this.sessions.clear()
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
      void this.postSignal(sessionId, peerId, 'media-candidate', JSON.stringify(event.candidate))
    }

    pc.onconnectionstatechange = () => {
      const cs = pc.connectionState
      if (cs === 'connected') {
        this.updateState(session, 'connected')
      } else if (cs === 'failed' || cs === 'closed') {
        this.closeSession(session, 'failed')
      }
    }

    // Collect incoming tracks into the remote MediaStream
    pc.ontrack = (event) => {
      for (const track of event.streams[0]?.getTracks() ?? []) {
        remoteStream.addTrack(track)
      }
      if (event.streams[0]) {
        // Also add from the primary stream directly
        event.streams[0].getTracks().forEach((t) => {
          if (!remoteStream.getTracks().includes(t)) remoteStream.addTrack(t)
        })
      } else {
        remoteStream.addTrack(event.track)
      }
      this.config.onRemoteStream(peerId, remoteStream)
    }

    // Renegotiation (e.g., video track added after initial offer)
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
    } catch (err) {
      console.warn('[call] renegotiation offer failed:', err)
    } finally {
      session.negotiating = false
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

  // ── Private: signaling ─────────────────────────────────────────────────────

  private async pollSignals(): Promise<void> {
    if (this.stopped) return
    try {
      const query = new URLSearchParams({
        to_node_id: this.config.nodeId,
        since_id: String(this.lastSignalId),
      })
      const resp = await fetch(`${this.config.rendezvousBase}/v1/signal?${query}`)
      if (!resp.ok) return
      const data = (await resp.json()) as { ok: boolean; signals?: SignalData[] }
      for (const sig of data.signals ?? []) {
        if (!sig.type.startsWith('media-')) continue
        this.lastSignalId = Math.max(this.lastSignalId, sig.id)
        await this.handleSignal(sig)
      }
    } catch {
      /* network error — retry next poll */
    }
  }

  private async handleSignal(sig: SignalData): Promise<void> {
    const { session_id, from_node, type, payload } = sig

    // Only process signals intended for sessions we own or can answer
    const expectedSessionId = this.makeSessionId(from_node)
    if (session_id !== expectedSessionId) return

    if (type === 'media-offer') {
      // Answerer path: create session if it doesn't exist
      if (this.sessions.has(session_id)) return
      // Wait for getUserMedia so local tracks are ready for the answer.
      await this.mediaReady
      if (this.sessions.has(session_id)) return
      const session = this.createPeerSession(session_id, from_node, false)
      this.sessions.set(session_id, session)

      // Add local tracks before answer
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
      } catch (err) {
        console.warn('[call] answer failed:', err)
        this.closeSession(session, 'failed')
      }
    } else if (type === 'media-answer') {
      const session = this.sessions.get(session_id)
      if (!session) return
      try {
        await session.pc.setRemoteDescription(JSON.parse(payload) as RTCSessionDescriptionInit)
        session.negotiating = false
      } catch (err) {
        console.warn('[call] setRemoteDescription (answer) failed:', err)
      }
    } else if (type === 'media-candidate') {
      const session = this.sessions.get(session_id)
      if (!session) return
      try {
        await session.pc.addIceCandidate(JSON.parse(payload) as RTCIceCandidateInit)
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
        const body = await resp.text()
        console.warn('[call] postSignal failed', resp.status, type, body.slice(0, 80))
      }
    } catch (err) {
      console.warn('[call] postSignal error:', err)
    }
  }
}
