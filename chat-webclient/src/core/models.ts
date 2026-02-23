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

export interface State {
  node: NodeConfig
  settings: {
    rendezvousBase: string
  }
  circles: Record<string, Circle>
  channels: Record<string, Record<string, Channel>>
  messages: Record<string, ChatMessage>
  currentCircleId?: string
  currentChannelId?: string
}

export const nowTs = (): number => Math.floor(Date.now() / 1000)
