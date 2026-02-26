export type AccessMode = 'public' | 'key' | 'invite'

export interface NodeConfig {
  nodeId: string
  displayName: string
}

export interface Circle {
  circleId: string
  secretHex: string
  name: string
  isOwned?: boolean
}

export interface Channel {
  channelId: string
  circleId: string
  accessMode: AccessMode
  createdBy: string
  createdTs: number
}

export interface ChatMessage {
  msgId: string
  circleId: string
  channelId: string
  authorNodeId: string
  displayName: string
  createdTs: number
  text: string
  mac?: string
}

export type CallState = 'pending' | 'active' | 'ended'

export interface CallSession {
  sessionId: string
  hostNodeId: string
  circleId: string
  channelId: string
  createdTs: number
  /** Sorted list of participant node IDs (includes host). */
  participants: string[]
  /** Sorted list of viewer node IDs (receive-only). */
  viewers: string[]
  callState: CallState
}

export interface State {
  node: NodeConfig
  settings: {
    rendezvousBase: string
    /** Optional TURN server URL, e.g. turn:your-server:3478 */
    turnUrl?: string
    turnUsername?: string
    turnCredential?: string
    timeFormat?: '24h' | '12h'
  }
  circles: Record<string, Circle>
  channels: Record<string, Record<string, Channel>>
  messages: Record<string, ChatMessage>
  /** Ephemeral call sessions — always reset to {} on page load. */
  activeCalls: Record<string, CallSession>
  currentCircleId?: string
  currentChannelId?: string
}

export const nowTs = (): number => Math.floor(Date.now() / 1000)
