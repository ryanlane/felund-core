import { openDB } from 'idb'

import { randomHex, sha256Hex, sha256HexFromRawKey } from './crypto'
import type { AccessMode, ChatMessage, Channel, Circle, State } from './models'
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
  const circleId = (await sha256HexFromRawKey(secretHex)).slice(0, 24)
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
  }
  state.messages[message.msgId] = message
  return message
}

const CONTROL_CHANNEL_ID = '__control'

/**
 * Parse and apply CHANNEL_EVT / CIRCLE_NAME_EVT control messages.
 *
 * Callers must pass a state whose `channels` and `circles` dicts are already
 * shallow-copied from the previous state (so this function can safely replace
 * per-circle channel dicts or circle objects with fresh copies).
 *
 * Returns true if any channel or circle name was changed.
 */
export const applyControlEvents = (state: State, msgs: ChatMessage[]): boolean => {
  let changed = false
  for (const msg of msgs) {
    if (msg.channelId !== CONTROL_CHANNEL_ID) continue
    let evt: unknown
    try {
      evt = JSON.parse(msg.text)
    } catch {
      continue
    }
    if (!evt || typeof evt !== 'object') continue
    const e = evt as Record<string, unknown>

    if (e['t'] === 'CHANNEL_EVT' && e['op'] === 'create') {
      const channelId = String(e['channel_id'] ?? '')
        .trim()
        .toLowerCase()
      // Mirror Python _valid_channel_id: no __ prefix, alphanumeric + - _
      if (!channelId || channelId.startsWith('__') || channelId.length > 32) continue
      if (!/^[a-z0-9_-]+$/.test(channelId)) continue
      if (!state.channels[msg.circleId]?.[channelId]) {
        // Shallow-copy the per-circle dict before mutating it
        state.channels[msg.circleId] = { ...(state.channels[msg.circleId] ?? {}) }
        const accessModeRaw = String(e['access_mode'] ?? '')
        const accessMode: AccessMode =
          accessModeRaw === 'key' ? 'key' : accessModeRaw === 'invite' ? 'invite' : 'public'
        state.channels[msg.circleId][channelId] = {
          channelId,
          circleId: msg.circleId,
          accessMode,
          createdBy: String(e['actor_node_id'] ?? e['created_by'] ?? ''),
          createdTs: typeof e['created_ts'] === 'number' ? e['created_ts'] : nowTs(),
        }
        changed = true
      }
    }

    if (e['t'] === 'CIRCLE_NAME_EVT') {
      const name = String(e['name'] ?? '').trim().slice(0, 40)
      const circle = state.circles[msg.circleId]
      if (name && circle && circle.name !== name) {
        // Shallow-copy the circle object before mutating name
        state.circles[msg.circleId] = { ...circle, name }
        changed = true
      }
    }
  }
  return changed
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
