import { sha256Hex } from '../core/crypto'

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
): Promise<RendezvousPeer[]> => {
  const hint = await circleHint(circleId)
  const query = new URLSearchParams({ circle_hint: hint, limit: String(limit) })
  const url = `${withV1(base, 'peers')}?${query.toString()}`

  const response = await fetch(url, {
    headers: {
      'X-Felund-Node': nodeId,
    },
  })
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
  input: { nodeId: string; circleId: string; ttlS?: number },
): Promise<void> => {
  const hint = await circleHint(input.circleId)
  const response = await fetch(withV1(base, 'register'), {
    method: 'POST',
    headers: {
      'content-type': 'application/json',
      'X-Felund-Node': input.nodeId,
    },
    body: JSON.stringify({
      node_id: input.nodeId,
      circle_hint: hint,
      endpoints: [defaultEndpoint()],
      capabilities: { relay: false, transport: ['ws'] },
      ttl_s: input.ttlS ?? 120,
    }),
  })

  if (!response.ok) {
    throw new Error(`Register failed (${response.status})`)
  }
}

export const unregisterPresence = async (
  base: string,
  input: { nodeId: string; circleId: string },
): Promise<void> => {
  const hint = await circleHint(input.circleId)
  const response = await fetch(withV1(base, 'register'), {
    method: 'DELETE',
    headers: {
      'content-type': 'application/json',
    },
    keepalive: true,
    body: JSON.stringify({
      node_id: input.nodeId,
      circle_hint: hint,
    }),
  })
  if (!response.ok) {
    throw new Error(`Unregister failed (${response.status})`)
  }
}
