export interface ParsedInvite {
  secretHex: string
  peer: string
}

const PREFIX = 'felund1.'

const toBase64Url = (value: string): string =>
  btoa(value).replace(/\+/g, '-').replace(/\//g, '_').replace(/=+$/g, '')

const fromBase64Url = (token: string): string => {
  const normalized = token.replace(/-/g, '+').replace(/_/g, '/')
  const padding = '='.repeat((4 - (normalized.length % 4)) % 4)
  return atob(normalized + padding)
}

export const makeInviteCode = (secretHex: string, peer: string): string => {
  const payload = JSON.stringify({ v: 1, secret: secretHex, peer })
  return `${PREFIX}${toBase64Url(payload)}`
}

export const parseInviteCode = (code: string): ParsedInvite => {
  if (!code.startsWith(PREFIX)) {
    throw new Error('Invalid invite prefix')
  }

  const payloadRaw = fromBase64Url(code.slice(PREFIX.length))
  const payload = JSON.parse(payloadRaw) as { v?: number; secret?: string; peer?: string }
  if (payload.v !== 1 || !payload.secret || !payload.peer) {
    throw new Error('Invalid invite payload')
  }
  return {
    secretHex: payload.secret.toLowerCase().trim(),
    peer: payload.peer.trim(),
  }
}
