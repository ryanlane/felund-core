// ── Types & constants ─────────────────────────────────────────────────────────

export type CallPeerState = 'connecting' | 'connected' | 'failed'

export interface CallPeerSession {
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

export interface SignalData {
  id: number
  session_id: string
  from_node: string
  to_node: string
  type: string
  payload: string
}

export const ICE_TIMEOUT_MS = 20_000
export const SIGNAL_POLL_MS = 2_000
export const SIGNAL_BACKOFF_BASE_MS = 500
export const SIGNAL_BACKOFF_MAX_MS = 5_000
export const CANDIDATE_FLUSH_MS = 250
export const CANDIDATE_BATCH = 6
