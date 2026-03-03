import { hmacHex, sha256Hex } from '../core/crypto'

export interface RendezvousHealth {
  ok: boolean
  version?: string
  time?: number
}

export interface RendezvousPeer {
  node_id: string
  circle_hint: string
  endpoints: Array<{
    transport: 'tcp' | 'ws'
    host: string
    port: number
    family: 'ipv4' | 'ipv6'
    nat: 'unknown' | 'open' | 'restricted' | 'symmetric'
  }>
  capabilities: {
    relay: boolean
    transport: Array<'tcp' | 'ws'>
  }
  observed_at: number
  expires_at: number
}

export const normalizeRendezvousBase = (raw: string): string => raw.trim().replace(/\/+$/, '')

const withV1 = (base: string, path: string): string => {
  const normalized = normalizeRendezvousBase(base)
  if (normalized.endsWith('/v1')) {
    return `${normalized}/${path}`
  }
  return `${normalized}/v1/${path}`
}

const circleHint = async (circleId: string): Promise<string> => {
  const digest = await sha256Hex(circleId)
  return digest.slice(0, 16)
}

// ── Request signing ───────────────────────────────────────────────────────────

/**
 * Derive the API signing key: HMAC-SHA256(circle_secret_bytes, "api-v1").
 *
 * The signing key is a one-way transform of the circle secret. It is safe to
 * send to the server during registration — the circle secret itself is never
 * transmitted.
 */
const signingKeyHex = async (circleSecretHex: string): Promise<string> =>
  hmacHex(circleSecretHex, 'api-v1')

/**
 * Return a hex HMAC-SHA256 signature for a canonical API request.
 *
 * Canonical string: METHOD + path + sha256(bodyStr) + ts + nonce
 * Signing key:      HMAC-SHA256(circle_secret_bytes, "api-v1")
 */
const signRequest = async (
  circleSecretHex: string,
  method: string,
  path: string,
  bodyStr: string,
  ts: number,
  nonce: string,
): Promise<string> => {
  const key = await signingKeyHex(circleSecretHex)
  const bodyHash = await sha256Hex(bodyStr)
  const canonical = `${method.toUpperCase()}${path}${bodyHash}${ts}${nonce}`
  return hmacHex(key, canonical)
}

const buildAuthHeaders = async (
  circleSecretHex: string,
  nodeId: string,
  method: string,
  path: string,
  bodyStr: string,
): Promise<Record<string, string>> => {
  const ts = Math.floor(Date.now() / 1000)
  const nonce = Array.from(crypto.getRandomValues(new Uint8Array(16)))
    .map((b) => b.toString(16).padStart(2, '0'))
    .join('')
  const sig = await signRequest(circleSecretHex, method, path, bodyStr, ts, nonce)
  return {
    'X-Felund-Node': nodeId,
    'X-Felund-Ts': String(ts),
    'X-Felund-Nonce': nonce,
    'X-Felund-Signature': sig,
  }
}

// ── Public API ────────────────────────────────────────────────────────────────

export const healthCheck = async (base: string): Promise<RendezvousHealth> => {
  const url = withV1(base, 'health')
  const response = await fetch(url)
  if (!response.ok) {
    throw new Error(`Health check failed (${response.status})`)
  }
  return (await response.json()) as RendezvousHealth
}

export const lookupPeers = async (
  base: string,
  nodeId: string,
  circleId: string,
  limit = 20,
  circleSecretHex = '',
): Promise<RendezvousPeer[]> => {
  const hint = await circleHint(circleId)
  const query = new URLSearchParams({ circle_hint: hint, limit: String(limit) })
  const url = `${withV1(base, 'peers')}?${query.toString()}`

  const baseHeaders: Record<string, string> = { 'X-Felund-Node': nodeId }
  const authHeaders = circleSecretHex
    ? await buildAuthHeaders(circleSecretHex, nodeId, 'GET', '/peers', '')
    : baseHeaders

  const response = await fetch(url, { headers: authHeaders })
  if (!response.ok) {
    throw new Error(`Peer lookup failed (${response.status})`)
  }

  const payload = (await response.json()) as { ok: boolean; peers?: RendezvousPeer[] }
  return payload.peers ?? []
}

const defaultEndpoint = () => {
  const host = window.location.hostname || 'localhost'
  const inferredPort = window.location.protocol === 'https:' ? 443 : 80
  const port = Number.parseInt(window.location.port || '', 10) || inferredPort
  return {
    transport: 'ws' as const,
    host,
    port,
    family: host.includes(':') ? ('ipv6' as const) : ('ipv4' as const),
    nat: 'unknown' as const,
  }
}

export const registerPresence = async (
  base: string,
  input: { nodeId: string; circleId: string; ttlS?: number; circleSecretHex?: string },
): Promise<void> => {
  const hint = await circleHint(input.circleId)
  const body: Record<string, unknown> = {
    node_id: input.nodeId,
    circle_hint: hint,
    endpoints: [defaultEndpoint()],
    capabilities: { relay: false, transport: ['ws'], can_anchor: false },
    ttl_s: input.ttlS ?? 120,
  }

  let authHeaders: Record<string, string> = {
    'content-type': 'application/json',
    'X-Felund-Node': input.nodeId,
  }

  if (input.circleSecretHex) {
    // Include the signing key so the server can store and verify future requests.
    body.signing_key = await signingKeyHex(input.circleSecretHex)
    const bodyStr = JSON.stringify(body)
    const signed = await buildAuthHeaders(
      input.circleSecretHex,
      input.nodeId,
      'POST',
      '/register',
      bodyStr,
    )
    authHeaders = { 'content-type': 'application/json', ...signed }
  }

  const response = await fetch(withV1(base, 'register'), {
    method: 'POST',
    headers: authHeaders,
    body: JSON.stringify(body),
  })

  if (!response.ok) {
    throw new Error(`Register failed (${response.status})`)
  }
}

export const unregisterPresence = async (
  base: string,
  input: { nodeId: string; circleId: string; circleSecretHex?: string },
): Promise<void> => {
  const hint = await circleHint(input.circleId)
  const body = {
    node_id: input.nodeId,
    circle_hint: hint,
  }

  let authHeaders: Record<string, string> = { 'content-type': 'application/json' }

  if (input.circleSecretHex) {
    const bodyStr = JSON.stringify(body)
    const signed = await buildAuthHeaders(
      input.circleSecretHex,
      input.nodeId,
      'DELETE',
      '/register',
      bodyStr,
    )
    authHeaders = { 'content-type': 'application/json', ...signed }
  }

  const response = await fetch(withV1(base, 'register'), {
    method: 'DELETE',
    headers: authHeaders,
    keepalive: true,
    body: JSON.stringify(body),
  })
  if (!response.ok) {
    throw new Error(`Unregister failed (${response.status})`)
  }
}
