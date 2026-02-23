/**
 * Relay-based message sync.
 *
 * Because browsers cannot open raw TCP connections, this module uses the
 * rendezvous server's /v1/messages endpoints as a shared message store.
 * Both web and Python clients push their messages here; all clients poll
 * for new messages.  Confidentiality and integrity are guaranteed by
 * AES-256-GCM — the server stores an opaque encrypted blob and cannot
 * read or forge message contents.
 */

import {
  type EncPayload,
  decryptMessageFields,
  deriveMessageKey,
  encryptMessageFields,
  hmacHex,
  sha256Hex,
} from '../core/crypto'
import type { ChatMessage } from '../core/models'

// ── Helpers ──────────────────────────────────────────────────────────────────

const withV1 = (base: string, path: string): string => {
  const normalized = base.trim().replace(/\/+$/, '')
  if (normalized.endsWith('/v1')) return `${normalized}/${path}`
  return `${normalized}/v1/${path}`
}

const circleHintFor = async (circleId: string): Promise<string> =>
  (await sha256Hex(circleId)).slice(0, 16)

// ── Wire formats ──────────────────────────────────────────────────────────────

/** Encrypted wire format (new). */
interface RelayMessage {
  msg_id: string
  circle_id: string
  channel_id: string
  author_node_id: string
  created_ts: number
  enc: EncPayload
}

/** Legacy plaintext wire format for backward-compatible reading. */
interface LegacyRelayMessage {
  msg_id: string
  circle_id: string
  channel_id: string
  author_node_id: string
  display_name: string
  created_ts: number
  text: string
  mac: string
}

type AnyRelayMessage = RelayMessage | LegacyRelayMessage

const isEncrypted = (r: AnyRelayMessage): r is RelayMessage => 'enc' in r

// ── Serialisation helpers ─────────────────────────────────────────────────────

const toWire = async (key: CryptoKey, m: ChatMessage): Promise<RelayMessage> => {
  const clearFields = {
    msgId: m.msgId,
    circleId: m.circleId,
    channelId: m.channelId,
    authorNodeId: m.authorNodeId,
    createdTs: m.createdTs,
  }
  const enc = await encryptMessageFields(key, clearFields, {
    displayName: m.displayName,
    text: m.text,
  })
  return {
    msg_id: m.msgId,
    circle_id: m.circleId,
    channel_id: m.channelId,
    author_node_id: m.authorNodeId,
    created_ts: m.createdTs,
    enc,
  }
}

/**
 * Deserialise a relay message.  Returns null and logs a warning on
 * decryption or MAC failure.
 */
const fromWire = async (
  key: CryptoKey,
  secretHex: string,
  r: AnyRelayMessage,
): Promise<ChatMessage | null> => {
  try {
    if (isEncrypted(r)) {
      // Encrypted path
      const clearFields = {
        msgId: r.msg_id,
        circleId: r.circle_id,
        channelId: r.channel_id,
        authorNodeId: r.author_node_id,
        createdTs: r.created_ts,
      }
      const { displayName, text } = await decryptMessageFields(key, r.enc, clearFields)
      return {
        msgId: r.msg_id,
        circleId: r.circle_id,
        channelId: r.channel_id,
        authorNodeId: r.author_node_id,
        displayName,
        createdTs: r.created_ts,
        text,
      }
    } else {
      // Legacy plaintext path — verify HMAC-SHA256 MAC
      const lg = r as LegacyRelayMessage
      const macPayload = [
        lg.msg_id,
        lg.circle_id,
        lg.channel_id,
        lg.author_node_id,
        lg.display_name,
        String(lg.created_ts),
        lg.text,
      ].join('|')
      const expected = await hmacHex(secretHex, macPayload)
      if (expected !== lg.mac) {
        console.warn('[felund] legacy MAC fail:', lg.msg_id.slice(0, 8))
        return null
      }
      return {
        msgId: lg.msg_id,
        circleId: lg.circle_id,
        channelId: lg.channel_id,
        authorNodeId: lg.author_node_id,
        displayName: lg.display_name,
        createdTs: lg.created_ts,
        text: lg.text,
        mac: lg.mac,
      }
    }
  } catch (err) {
    console.warn('[felund] decrypt fail:', err)
    return null
  }
}

// ── API calls ─────────────────────────────────────────────────────────────────

/**
 * Push a batch of messages to the relay for a given circle.
 * Silently skips __control channel messages.
 */
export const pushMessages = async (
  base: string,
  circleId: string,
  secretHex: string,
  msgs: ChatMessage[],
): Promise<void> => {
  const toSend = msgs.filter((m) => m.channelId !== '__control')
  if (toSend.length === 0) return

  const hint = await circleHintFor(circleId)
  const key = await deriveMessageKey(secretHex)

  // POST in batches of 50 (server limit)
  for (let i = 0; i < toSend.length; i += 50) {
    const batch = toSend.slice(i, i + 50)
    const messages = await Promise.all(batch.map((m) => toWire(key, m)))
    const response = await fetch(withV1(base, 'messages'), {
      method: 'POST',
      headers: { 'content-type': 'application/json' },
      body: JSON.stringify({ circle_hint: hint, messages }),
    })
    if (!response.ok) {
      throw new Error(`Relay push failed (${response.status})`)
    }
  }
}

/**
 * Pull messages from the relay for a circle since a given server timestamp.
 * Returns decrypted ChatMessages (null results from failed decryption are
 * filtered out) and the server time to use as the next `since` cursor.
 */
export const pullMessages = async (
  base: string,
  circleId: string,
  secretHex: string,
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
    messages?: AnyRelayMessage[]
    server_time?: number
  }

  const key = await deriveMessageKey(secretHex)
  const results = await Promise.all(
    (data.messages ?? []).map((r) => fromWire(key, secretHex, r)),
  )
  return {
    messages: results.filter((m): m is ChatMessage => m !== null),
    serverTime: data.server_time ?? 0,
  }
}

// Keep hmacHex re-export so legacy callers that imported it from here still compile.
export { hmacHex }
