/**
 * SignalingClient — HTTP transport for media signaling.
 *
 * Handles polling /v1/signal for incoming signals, posting outgoing signals
 * with rate-limit backoff and retry, and batching ICE candidates.
 */

import { sha256Hex } from '../../core/crypto'
import type { SignalData } from './types'
import {
  SIGNAL_POLL_MS,
  SIGNAL_BACKOFF_BASE_MS,
  SIGNAL_BACKOFF_MAX_MS,
  CANDIDATE_FLUSH_MS,
  CANDIDATE_BATCH,
} from './types'

export class SignalingClient {
  private nodeId: string
  private circleId: string
  private rendezvousBase: string
  private onSignal: (sig: SignalData) => Promise<void>

  private pollTimer: ReturnType<typeof setInterval> | null = null
  private lastSignalId = 0
  private pollInFlight = false
  private stopped = false
  private circleHint: string | null = null
  private signalBackoffUntil = 0
  private signalBackoffMs = 0
  private candidateQueue: Array<{ sessionId: string; peerId: string; payload: string }> = []
  private candidateFlushTimer: ReturnType<typeof setTimeout> | null = null
  private retrySignals = new Map<
    string,
    { sessionId: string; peerId: string; type: string; payload: string; attempt: number }
  >()
  private retryTimer: ReturnType<typeof setTimeout> | null = null

  constructor(config: {
    nodeId: string
    circleId: string
    rendezvousBase: string
    onSignal: (sig: SignalData) => Promise<void>
  }) {
    this.nodeId = config.nodeId
    this.circleId = config.circleId
    this.rendezvousBase = config.rendezvousBase
    this.onSignal = config.onSignal
    this.pollTimer = setInterval(() => void this.pollSignals(), SIGNAL_POLL_MS)
  }

  stop(): void {
    this.stopped = true
    if (this.pollTimer !== null) {
      clearInterval(this.pollTimer)
      this.pollTimer = null
    }
    if (this.candidateFlushTimer !== null) {
      clearTimeout(this.candidateFlushTimer)
      this.candidateFlushTimer = null
    }
    if (this.retryTimer !== null) {
      clearTimeout(this.retryTimer)
      this.retryTimer = null
    }
    this.retrySignals.clear()
    this.candidateQueue = []
  }

  queueCandidate(sessionId: string, peerId: string, payload: string): void {
    this.candidateQueue.push({ sessionId, peerId, payload })
    this.scheduleCandidateFlush()
  }

  async postSignal(
    sessionId: string,
    toNode: string,
    type: string,
    payload: string,
    allowRetry = true,
  ): Promise<boolean> {
    if (this.stopped) return false
    if (type === 'media-candidate' && Date.now() < this.signalBackoffUntil) return false
    try {
      if (!this.circleHint) {
        this.circleHint = (await sha256Hex(this.circleId)).slice(0, 16)
      }
      const resp = await fetch(`${this.rendezvousBase}/v1/signal`, {
        method: 'POST',
        headers: { 'content-type': 'application/json' },
        body: JSON.stringify({
          session_id: sessionId,
          from_node_id: this.nodeId,
          to_node_id: toNode,
          circle_hint: this.circleHint,
          type,
          payload,
          ttl_s: type === 'media-candidate' ? 60 : 120,
        }),
      })
      if (!resp.ok) {
        if (resp.status === 429) {
          this.signalBackoffMs = this.signalBackoffMs
            ? Math.min(this.signalBackoffMs * 2, SIGNAL_BACKOFF_MAX_MS)
            : SIGNAL_BACKOFF_BASE_MS
          this.signalBackoffUntil = Date.now() + this.signalBackoffMs
        }
        const body = await resp.text()
        console.warn('[call] postSignal failed', resp.status, type, body.slice(0, 80))
        if (allowRetry && type !== 'media-candidate') {
          this.queueSignalRetry(sessionId, toNode, type, payload)
        }
        return false
      }
      this.signalBackoffMs = 0
      this.signalBackoffUntil = 0
      return true
    } catch (err) {
      console.warn('[call] postSignal error:', err)
    }
    return false
  }

  // ── Private ────────────────────────────────────────────────────────────────

  private async pollSignals(): Promise<void> {
    if (this.stopped || this.pollInFlight) return
    this.pollInFlight = true
    try {
      const query = new URLSearchParams({
        to_node_id: this.nodeId,
        since_id: String(this.lastSignalId),
      })
      const resp = await fetch(`${this.rendezvousBase}/v1/signal?${query}`)
      if (!resp.ok) return
      const data = (await resp.json()) as { ok: boolean; signals?: SignalData[] }
      for (const sig of data.signals ?? []) {
        // Advance past ALL signals (including Phase-3 offer/answer/candidate)
        // so Phase-3 churn can't push Phase-5 signals beyond the relay's
        // LIMIT 50 page and make them invisible forever.
        this.lastSignalId = Math.max(this.lastSignalId, sig.id)
        if (!sig.type.startsWith('media-')) continue
        await this.onSignal(sig)
      }
    } catch {
      /* network error — retry next poll */
    } finally {
      this.pollInFlight = false
    }
  }

  private scheduleCandidateFlush(): void {
    if (this.candidateFlushTimer !== null) return
    this.candidateFlushTimer = setTimeout(() => {
      this.candidateFlushTimer = null
      void this.flushCandidateQueue()
    }, CANDIDATE_FLUSH_MS)
  }

  private async flushCandidateQueue(): Promise<void> {
    if (this.stopped || this.candidateQueue.length === 0) return
    if (Date.now() < this.signalBackoffUntil) {
      this.scheduleCandidateFlush()
      return
    }

    const batch = this.candidateQueue.splice(0, CANDIDATE_BATCH)
    for (const item of batch) {
      await this.postSignal(item.sessionId, item.peerId, 'media-candidate', item.payload)
    }

    if (this.candidateQueue.length > 0) {
      this.scheduleCandidateFlush()
    }
  }

  private queueSignalRetry(
    sessionId: string,
    peerId: string,
    type: string,
    payload: string,
  ): void {
    const key = `${sessionId}:${peerId}:${type}`
    const existing = this.retrySignals.get(key)
    const attempt = existing ? existing.attempt + 1 : 1
    if (attempt > 5) return
    this.retrySignals.set(key, { sessionId, peerId, type, payload, attempt })
    this.scheduleRetryFlush()
  }

  private scheduleRetryFlush(): void {
    if (this.retryTimer !== null) return
    const maxAttempt = Math.max(
      1,
      ...Array.from(this.retrySignals.values()).map((entry) => entry.attempt),
    )
    const delay = Math.min(
      SIGNAL_BACKOFF_BASE_MS * 2 ** (maxAttempt - 1),
      SIGNAL_BACKOFF_MAX_MS,
    )
    this.retryTimer = setTimeout(() => {
      this.retryTimer = null
      void this.flushRetrySignals()
    }, delay)
  }

  private async flushRetrySignals(): Promise<void> {
    if (this.stopped || this.retrySignals.size === 0) return
    const entries = Array.from(this.retrySignals.entries())
    for (const [key, entry] of entries) {
      const ok = await this.postSignal(
        entry.sessionId,
        entry.peerId,
        entry.type,
        entry.payload,
        false,
      )
      if (ok) {
        this.retrySignals.delete(key)
      } else if (entry.attempt + 1 > 5) {
        this.retrySignals.delete(key)
      } else {
        this.retrySignals.set(key, { ...entry, attempt: entry.attempt + 1 })
      }
    }

    if (this.retrySignals.size > 0) {
      this.scheduleRetryFlush()
    }
  }
}
