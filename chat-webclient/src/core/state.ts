import { openDB } from 'idb'

import { randomHex, sha256Hex, sha256HexFromRawKey } from './crypto'
import type { AccessMode, CallSession, ChatMessage, Channel, Circle, State } from './models'
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
    activeCalls: {},
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
    // activeCalls is always reset — call sessions are ephemeral.
    activeCalls: {},
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

export const leaveCircle = (state: State, circleId: string): void => {
  delete state.circles[circleId]
  delete state.channels[circleId]
  // Optionally clean up messages too
  for (const msgId of Object.keys(state.messages)) {
    if (state.messages[msgId].circleId === circleId) {
      delete state.messages[msgId]
    }
  }
  if (state.currentCircleId === circleId) {
    state.currentCircleId = undefined
    state.currentChannelId = undefined
    // Switch to first remaining circle if any
    const remaining = Object.keys(state.circles)
    if (remaining.length > 0) {
      state.currentCircleId = remaining[0]
      state.currentChannelId = 'general'
    }
  }
}

export const renameCircle = async (state: State, circleId: string, name: string): Promise<ChatMessage> => {
  const circle = state.circles[circleId]
  if (!circle) throw new Error('Circle not found')
  if (!circle.isOwned) throw new Error('Not the owner of this circle')

  const createdTs = nowTs()
  const msgId = (await sha256Hex(`${state.node.nodeId}|${createdTs}|${randomHex(8)}`)).slice(0, 32)
  const message: ChatMessage = {
    msgId,
    circleId,
    channelId: CONTROL_CHANNEL_ID,
    authorNodeId: state.node.nodeId,
    displayName: state.node.displayName,
    createdTs,
    text: JSON.stringify({ t: 'CIRCLE_NAME_EVT', name }),
  }
  state.messages[message.msgId] = message
  circle.name = name
  return message
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

    if (e['t'] === 'CALL_EVT') {
      const op = String(e['op'] ?? '')
      const sessionId = String(e['session_id'] ?? '').trim()
      const actorNodeId = String(e['actor_node_id'] ?? '').trim()
      if (!sessionId) continue

      if (op === 'create') {
        if (!state.activeCalls[sessionId]) {
          const hostNodeId = String(e['host_node_id'] ?? actorNodeId).trim()
          const channelId = String(e['channel_id'] ?? 'general').trim() || 'general'
          const createdTs = typeof e['created_ts'] === 'number' ? e['created_ts'] : nowTs()
          state.activeCalls[sessionId] = {
            sessionId,
            hostNodeId,
            circleId: msg.circleId,
            channelId,
            createdTs,
            participants: [hostNodeId],
            viewers: [],
            callState: 'pending',
          }
          changed = true
        }
      } else {
        const call = state.activeCalls[sessionId]
        if (!call) continue

        if (op === 'join') {
          const nodeId = String(e['node_id'] ?? actorNodeId).trim()
          if (nodeId && !call.participants.includes(nodeId)) {
            call.participants = [...call.participants, nodeId].sort()
            if (call.callState === 'pending' && call.participants.length > 1) {
              call.callState = 'active'
            }
            changed = true
          }
        } else if (op === 'leave') {
          const nodeId = String(e['node_id'] ?? actorNodeId).trim()
          if (nodeId) {
            const hadIt =
              call.participants.includes(nodeId) || call.viewers.includes(nodeId)
            call.participants = call.participants.filter((id) => id !== nodeId)
            call.viewers = call.viewers.filter((id) => id !== nodeId)
            if (hadIt) changed = true
          }
        } else if (op === 'end') {
          // Only accept end from the host.
          const hostInEvent = String(e['host_node_id'] ?? actorNodeId).trim()
          if (!hostInEvent || hostInEvent === call.hostNodeId) {
            delete state.activeCalls[sessionId]
            changed = true
          }
        }
        // invite and signal.* ops: no tracked state change.
      }
    }
  }
  return changed
}

// ── Call action helpers ───────────────────────────────────────────────────────

/** Build and add a control-channel ChatMessage for a call event. */
const makeCallEventMsg = async (
  state: State,
  circleId: string,
  event: Record<string, unknown>,
): Promise<ChatMessage> => {
  const createdTs = nowTs()
  const msgId = (await sha256Hex(`${state.node.nodeId}|${createdTs}|${randomHex(8)}`)).slice(0, 32)
  return {
    msgId,
    circleId,
    channelId: CONTROL_CHANNEL_ID,
    authorNodeId: state.node.nodeId,
    displayName: state.node.displayName,
    createdTs,
    text: JSON.stringify({ ...event, actor_node_id: state.node.nodeId }),
  }
}

/** Create a new call in the current channel and return the CALL_EVT message. */
export const createCall = async (state: State): Promise<ChatMessage | null> => {
  const circleId = state.currentCircleId
  const channelId = state.currentChannelId
  if (!circleId || !channelId) return null
  const sessionId = randomHex(16)
  const createdTs = nowTs()
  const event = {
    t: 'CALL_EVT',
    op: 'create',
    session_id: sessionId,
    host_node_id: state.node.nodeId,
    circle_id: circleId,
    channel_id: channelId,
    created_ts: createdTs,
  }
  const msg = await makeCallEventMsg(state, circleId, event)
  state.messages[msg.msgId] = msg
  // Apply locally so the UI updates immediately.
  const session: CallSession = {
    sessionId,
    hostNodeId: state.node.nodeId,
    circleId,
    channelId,
    createdTs,
    participants: [state.node.nodeId],
    viewers: [],
    callState: 'pending',
  }
  state.activeCalls[sessionId] = session
  return msg
}

/** Join an existing call and return the CALL_EVT message. */
export const joinCall = async (state: State, sessionId: string): Promise<ChatMessage | null> => {
  const call = state.activeCalls[sessionId]
  if (!call) return null
  const event = {
    t: 'CALL_EVT',
    op: 'join',
    session_id: sessionId,
    node_id: state.node.nodeId,
  }
  const msg = await makeCallEventMsg(state, call.circleId, event)
  state.messages[msg.msgId] = msg
  if (!call.participants.includes(state.node.nodeId)) {
    call.participants = [...call.participants, state.node.nodeId].sort()
    if (call.callState === 'pending' && call.participants.length > 1) {
      call.callState = 'active'
    }
  }
  return msg
}

/** Leave a call and return the CALL_EVT message. */
export const leaveCall = async (state: State, sessionId: string): Promise<ChatMessage | null> => {
  const call = state.activeCalls[sessionId]
  if (!call) return null
  const event = {
    t: 'CALL_EVT',
    op: 'leave',
    session_id: sessionId,
    node_id: state.node.nodeId,
    reason: 'user_left',
  }
  const msg = await makeCallEventMsg(state, call.circleId, event)
  state.messages[msg.msgId] = msg
  call.participants = call.participants.filter((id) => id !== state.node.nodeId)
  call.viewers = call.viewers.filter((id) => id !== state.node.nodeId)
  return msg
}

/** End a call (host only) and return the CALL_EVT message. */
export const endCall = async (state: State, sessionId: string): Promise<ChatMessage | null> => {
  const call = state.activeCalls[sessionId]
  if (!call || call.hostNodeId !== state.node.nodeId) return null
  const event = {
    t: 'CALL_EVT',
    op: 'end',
    session_id: sessionId,
    host_node_id: state.node.nodeId,
  }
  const msg = await makeCallEventMsg(state, call.circleId, event)
  state.messages[msg.msgId] = msg
  delete state.activeCalls[sessionId]
  return msg
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
