/**
 * Relay-based message sync.
 *
 * Because browsers cannot open raw TCP connections, this module uses the
 * rendezvous server's /v1/messages endpoints as a shared message store.
 * Both web and Python clients push their messages here; all clients poll
 * for new messages.  Integrity is guaranteed by HMAC-SHA256 — the server
 * stores messages opaquely and cannot forge or tamper with them.
 */

import { hmacHex, sha256Hex } from '../core/crypto'
import type { ChatMessage } from '../core/models'

// ── Helpers ──────────────────────────────────────────────────────────────────

const withV1 = (base: string, path: string): string => {
  const normalized = base.trim().replace(/\/+$/, '')
  if (normalized.endsWith('/v1')) return `${normalized}/${path}`
  return `${normalized}/v1/${path}`
}

const circleHintFor = async (circleId: string): Promise<string> =>
  (await sha256Hex(circleId)).slice(0, 16)

// ── Wire format (server uses snake_case) ──────────────────────────────────────

interface RelayMessage {
  msg_id: string
  circle_id: string
  channel_id: string
  author_node_id: string
  display_name: string
  created_ts: number
  text: string
  mac: string
}

const toWire = (m: ChatMessage): RelayMessage => ({
  msg_id: m.msgId,
  circle_id: m.circleId,
  channel_id: m.channelId,
  author_node_id: m.authorNodeId,
  display_name: m.displayName,
  created_ts: m.createdTs,
  text: m.text,
  mac: m.mac,
})

const fromWire = (r: RelayMessage): ChatMessage => ({
  msgId: r.msg_id,
  circleId: r.circle_id,
  channelId: r.channel_id,
  authorNodeId: r.author_node_id,
  displayName: r.display_name,
  createdTs: r.created_ts,
  text: r.text,
  mac: r.mac,
})

// ── MAC verification (mirrors Python make_message_mac) ────────────────────────

export const verifyMessageMac = async (
  secretHex: string,
  msg: ChatMessage,
): Promise<boolean> => {
  const payload = [
    msg.msgId,
    msg.circleId,
    msg.channelId,
    msg.authorNodeId,
    msg.displayName,
    String(msg.createdTs),
    msg.text,
  ].join('|')
  const expected = await hmacHex(secretHex, payload)
  return expected === msg.mac
}

// ── API calls ─────────────────────────────────────────────────────────────────

/**
 * Push a batch of messages to the relay for a given circle.
 * Silently skips __control channel messages.
 */
export const pushMessages = async (
  base: string,
  circleId: string,
  msgs: ChatMessage[],
): Promise<void> => {
  const toSend = msgs.filter((m) => m.channelId !== '__control')
  if (toSend.length === 0) return

  const hint = await circleHintFor(circleId)

  // POST in batches of 50 (server limit)
  for (let i = 0; i < toSend.length; i += 50) {
    const batch = toSend.slice(i, i + 50)
    const response = await fetch(withV1(base, 'messages'), {
      method: 'POST',
      headers: { 'content-type': 'application/json' },
      body: JSON.stringify({ circle_hint: hint, messages: batch.map(toWire) }),
    })
    if (!response.ok) {
      throw new Error(`Relay push failed (${response.status})`)
    }
  }
}

/**
 * Pull messages from the relay for a circle since a given server timestamp.
 * Returns the raw ChatMessage list (not yet MAC-verified) and the server time
 * to use as the next `since` cursor.
 */
export const pullMessages = async (
  base: string,
  circleId: string,
  since: number,
): Promise<{ messages: ChatMessage[]; serverTime: number }> => {
  const hint = await circleHintFor(circleId)
  const query = new URLSearchParams({
    circle_hint: hint,
    since: String(since),
    limit: '200',
  })
  const response = await fetch(`${withV1(base, 'messages')}?${query}`)
  if (!response.ok) {
    throw new Error(`Relay pull failed (${response.status})`)
  }
  const data = (await response.json()) as {
    ok: boolean
    messages?: RelayMessage[]
    server_time?: number
  }
  return {
    messages: (data.messages ?? []).map(fromWire),
    serverTime: data.server_time ?? 0,
  }
}
