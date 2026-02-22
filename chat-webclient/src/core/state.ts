import { openDB } from 'idb'

import { hmacHex, randomHex, sha256Hex } from './crypto'
import type { ChatMessage, Channel, Circle, State } from './models'
import { nowTs } from './models'

const DB_NAME = 'felundchat-web'
const STORE_NAME = 'kv'
const STATE_KEY = 'state-v1'

const defaultState = (): State => {
  const nodeId = randomHex(16)
  return {
    node: {
      nodeId,
      displayName: 'anon',
    },
    settings: {
      rendezvousBase: import.meta.env.VITE_FELUND_API_BASE || '',
    },
    circles: {},
    channels: {},
    messages: {},
  }
}

const sanitizeLoadedState = (raw: unknown): State => {
  const fallback = defaultState()
  if (!raw || typeof raw !== 'object') {
    return fallback
  }

  const data = raw as Partial<State>
  return {
    ...fallback,
    ...data,
    node: {
      ...fallback.node,
      ...(data.node ?? {}),
    },
    settings: {
      ...fallback.settings,
      ...(data.settings ?? {}),
    },
    circles: data.circles ?? {},
    channels: data.channels ?? {},
    messages: data.messages ?? {},
  }
}

export const loadState = async (): Promise<State> => {
  const db = await openDB(DB_NAME, 1, {
    upgrade(upgradeDb) {
      if (!upgradeDb.objectStoreNames.contains(STORE_NAME)) {
        upgradeDb.createObjectStore(STORE_NAME)
      }
    },
  })
  const state = await db.get(STORE_NAME, STATE_KEY)
  return sanitizeLoadedState(state)
}

export const saveState = async (state: State): Promise<void> => {
  const db = await openDB(DB_NAME, 1)
  await db.put(STORE_NAME, state, STATE_KEY)
}

export const ensureGeneralChannel = (state: State, circleId: string): void => {
  const byCircle = (state.channels[circleId] ??= {})
  if (!byCircle.general) {
    byCircle.general = {
      channelId: 'general',
      circleId,
      accessMode: 'public',
      createdBy: state.node.nodeId,
      createdTs: nowTs(),
    }
  }
}

export const createCircle = async (state: State, name: string): Promise<Circle> => {
  const secretHex = randomHex(32)
  const circleId = (await sha256Hex(secretHex)).slice(0, 24)
  const circle: Circle = { circleId, secretHex, name: name.trim(), isOwned: true }
  state.circles[circleId] = circle
  ensureGeneralChannel(state, circleId)
  state.currentCircleId = circleId
  state.currentChannelId = 'general'
  return circle
}

export const joinCircle = (state: State, circle: Circle): void => {
  state.circles[circle.circleId] = {
    ...circle,
    isOwned: circle.isOwned ?? false,
  }
  ensureGeneralChannel(state, circle.circleId)
  state.currentCircleId = circle.circleId
  state.currentChannelId = 'general'
}

export const createChannel = (state: State, circleId: string, channelIdRaw: string): Channel => {
  const channelId = channelIdRaw.trim().toLowerCase().replace(/^#/, '')
  if (!channelId) {
    throw new Error('Channel name required')
  }
  const byCircle = (state.channels[circleId] ??= {})
  if (byCircle[channelId]) {
    return byCircle[channelId]
  }
  const channel: Channel = {
    channelId,
    circleId,
    accessMode: 'public',
    createdBy: state.node.nodeId,
    createdTs: nowTs(),
  }
  byCircle[channelId] = channel
  return channel
}

export const sendMessage = async (state: State, text: string): Promise<ChatMessage> => {
  if (!state.currentCircleId || !state.currentChannelId) {
    throw new Error('No active circle/channel')
  }
  const circle = state.circles[state.currentCircleId]
  if (!circle) {
    throw new Error('Active circle not found')
  }

  const createdTs = nowTs()
  const msgId = (await sha256Hex(`${state.node.nodeId}|${createdTs}|${randomHex(8)}`)).slice(0, 32)
  const message: ChatMessage = {
    msgId,
    circleId: state.currentCircleId,
    channelId: state.currentChannelId,
    authorNodeId: state.node.nodeId,
    displayName: state.node.displayName,
    createdTs,
    text,
    mac: '',
  }

  const macPayload = [
    message.msgId,
    message.circleId,
    message.channelId,
    message.authorNodeId,
    message.displayName,
    String(message.createdTs),
    message.text,
  ].join('|')
  message.mac = await hmacHex(circle.secretHex, macPayload)
  state.messages[message.msgId] = message
  return message
}

export const visibleMessages = (state: State): ChatMessage[] => {
  const circleId = state.currentCircleId
  const channelId = state.currentChannelId
  if (!circleId || !channelId) {
    return []
  }

  return Object.values(state.messages)
    .filter((message) => message.circleId === circleId && message.channelId === channelId)
    .sort((left, right) => left.createdTs - right.createdTs || left.msgId.localeCompare(right.msgId))
}
