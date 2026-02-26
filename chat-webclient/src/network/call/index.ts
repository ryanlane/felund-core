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

import { SignalingClient } from './signaling'
import type { CallManagerConfig, CallPeerSession, CallPeerState, SignalData } from './types'
import { ICE_TIMEOUT_MS } from './types'

export type { CallPeerState, CallManagerConfig } from './types'

// ── WebRTCCallManager ─────────────────────────────────────────────────────────

export class WebRTCCallManager {
  /** The local MediaStream (audio + optional video). null until startMedia(). */
  localStream: MediaStream | null = null

  private sessions = new Map<string, CallPeerSession>()
  private config: CallManagerConfig
  private stopped = false
  /** Resolves when startMedia() has finished (success or failure). */
  private mediaReady: Promise<void> = Promise.resolve()
  /**
   * Candidates that arrived before the session existed or before
   * setRemoteDescription() was called.  Flushed after setRemoteDescription().
   */
  private pendingCandidates = new Map<string, RTCIceCandidateInit[]>()
  private signaling: SignalingClient

  constructor(config: CallManagerConfig) {
    this.config = config
    this.signaling = new SignalingClient({
      nodeId: config.nodeId,
      circleId: config.circleId,
      rendezvousBase: config.rendezvousBase,
      onSignal: (sig) => this.handleSignal(sig),
    })
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
    for (const track of this.localStream?.getAudioTracks() ?? []) {
      track.onended = () => {
        console.warn('[call] local audio track ended')
      }
    }
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

    const hasAudio = await this.ensureAudioTrack()
    if (!hasAudio) {
      console.warn('[call] no local audio track; skipping media offer')
      return
    }

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
      await this.signaling.postSignal(sessionId, peerId, 'media-offer', JSON.stringify(offer))
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

  async setAudioInput(deviceId: string | null): Promise<boolean> {
    try {
      const constraints: MediaStreamConstraints = {
        audio: deviceId ? { deviceId: { exact: deviceId } } : true,
      }
      const stream = await navigator.mediaDevices.getUserMedia(constraints)
      const newTrack = stream.getAudioTracks()[0]
      if (!newTrack) return false
      newTrack.onended = () => {
        console.warn('[call] local audio track ended')
      }

      if (this.localStream) {
        for (const oldTrack of this.localStream.getAudioTracks()) {
          this.localStream.removeTrack(oldTrack)
          oldTrack.stop()
        }
        this.localStream.addTrack(newTrack)
      } else {
        this.localStream = new MediaStream([newTrack])
      }

      for (const session of this.sessions.values()) {
        if (session.state === 'failed') continue
        const sender = session.pc.getSenders().find((s) => s.track?.kind === 'audio')
        if (sender) {
          await sender.replaceTrack(newTrack)
        } else {
          session.pc.addTrack(newTrack, this.localStream)
        }
      }

      return true
    } catch (err) {
      console.warn('[call] getUserMedia (audio switch) failed:', err)
      return false
    }
  }

  /** Stop all media tracks, close all PeerConnections, stop signaling poll. */
  destroy(): void {
    this.stopped = true
    this.signaling.stop()
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

  // ── Private: media ─────────────────────────────────────────────────────────

  private hasLiveAudio(): boolean {
    return (
      this.localStream?.getAudioTracks().some((track) => track.readyState === 'live') ??
      false
    )
  }

  private async ensureAudioTrack(): Promise<boolean> {
    if (this.hasLiveAudio()) return true
    try {
      const audioStream = await navigator.mediaDevices.getUserMedia({ audio: true })
      const audioTrack = audioStream.getAudioTracks()[0]
      if (!audioTrack) return false
      audioTrack.onended = () => {
        console.warn('[call] local audio track ended')
      }
      if (this.localStream) {
        this.localStream.addTrack(audioTrack)
      } else {
        this.localStream = audioStream
      }
      for (const session of this.sessions.values()) {
        if (session.state !== 'failed') {
          session.pc.addTrack(audioTrack, this.localStream)
        }
      }
      return true
    } catch (err) {
      console.warn('[call] getUserMedia (audio refresh) failed:', err)
      return false
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
      this.signaling.queueCandidate(sessionId, peerId, JSON.stringify(event.candidate))
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

    // Perfect negotiation: either side may initiate renegotiation (e.g., when
    // the answerer enables their camera).  Glare is resolved in handleSignal
    // using nodeId as the polite/impolite tie-breaker.
    pc.onnegotiationneeded = () => {
      if (session.negotiating || this.stopped) return
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
      const sent = await this.signaling.postSignal(
        session.sessionId,
        session.peerId,
        'media-offer',
        JSON.stringify(offer),
      )
      if (sent) {
        // Keep negotiating=true until the media-answer arrives and clears it,
        // preventing a second onnegotiationneeded from starting a concurrent offer.
        return
      }
      // postSignal returns false without throwing when the send fails (it
      // catches internally).  The SignalingClient will retry in the background,
      // but if all retries are eventually exhausted, no answer will ever arrive
      // and negotiating would be permanently stuck.  Reset now so that
      // onnegotiationneeded can fire again once the connection recovers.
      console.warn('[call] renegotiation offer send failed; resetting negotiating flag')
    } catch (err) {
      console.warn('[call] renegotiation offer failed:', err)
    }
    session.negotiating = false
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

  // ── Private: signal handling ───────────────────────────────────────────────

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
        await this.ensureAudioTrack()
        if (this.sessions.has(session_id)) return // created while awaiting mediaReady
        const session = this.createPeerSession(session_id, from_node, false)
        this.sessions.set(session_id, session)

        // Guard: addTrack() queues an onnegotiationneeded macrotask.  Hold
        // negotiating=true so that macrotask cannot start a spurious re-offer
        // while the initial answer exchange is still in flight.  Cleared
        // below once the answer is sent, opening the door for future
        // renegotiations by either side.
        session.negotiating = true
        if (this.localStream) {
          for (const track of this.localStream.getTracks()) {
            session.pc.addTrack(track, this.localStream)
          }
        }

        try {
          await session.pc.setRemoteDescription(JSON.parse(payload) as RTCSessionDescriptionInit)
          const answer = await session.pc.createAnswer()
          await session.pc.setLocalDescription(answer)
          await this.signaling.postSignal(session_id, from_node, 'media-answer', JSON.stringify(answer))
          session.negotiating = false
          await this.flushPendingCandidates(session)
        } catch (err) {
          console.warn('[call] answer failed:', err)
          this.closeSession(session, 'failed')
        }
      } else {
        // Re-offer: renegotiation from either side (perfect negotiation).
        //
        // Glare occurs when both sides simultaneously call createOffer().
        // Tie-break by nodeId: the initiator (smaller nodeId) is "impolite"
        // and ignores the incoming offer; the answerer (larger nodeId) is
        // "polite" and rolls back its pending offer to accept the remote one.
        // After rollback, onnegotiationneeded will re-fire so the polite side
        // can still send its own tracks in a fresh offer.
        const isPolite = !existingSession.isInitiator
        const offerCollision =
          existingSession.negotiating ||
          existingSession.pc.signalingState !== 'stable'

        if (offerCollision && !isPolite) return // impolite: ignore glare

        try {
          if (offerCollision) {
            // Polite side: roll back our pending offer before accepting theirs.
            await existingSession.pc.setLocalDescription({ type: 'rollback' })
            existingSession.negotiating = false
          }
          await existingSession.pc.setRemoteDescription(
            JSON.parse(payload) as RTCSessionDescriptionInit,
          )
          const answer = await existingSession.pc.createAnswer()
          await existingSession.pc.setLocalDescription(answer)
          await this.signaling.postSignal(session_id, from_node, 'media-answer', JSON.stringify(answer))
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
        await this.flushPendingCandidates(session)
      } catch (err) {
        console.warn('[call] setRemoteDescription (answer) failed:', err)
      } finally {
        // Always clear the flag — even if setRemoteDescription threw — so that
        // onnegotiationneeded can fire again and retry the renegotiation.
        session.negotiating = false
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
}
