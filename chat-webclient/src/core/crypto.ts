const encoder = new TextEncoder()

const toHex = (bytes: Uint8Array): string =>
  Array.from(bytes)
    .map((value) => value.toString(16).padStart(2, '0'))
    .join('')

export const randomHex = (size: number): string => {
  const bytes = new Uint8Array(size)
  crypto.getRandomValues(bytes)
  return toHex(bytes)
}

export const sha256Hex = async (input: string): Promise<string> => {
  const digest = await crypto.subtle.digest('SHA-256', encoder.encode(input))
  return toHex(new Uint8Array(digest))
}

export const hmacHex = async (keyHex: string, input: string): Promise<string> => {
  const keyRaw = Uint8Array.from(keyHex.match(/.{1,2}/g)?.map((pair) => parseInt(pair, 16)) ?? [])
  const key = await crypto.subtle.importKey(
    'raw',
    keyRaw,
    { name: 'HMAC', hash: 'SHA-256' },
    false,
    ['sign'],
  )
  const sig = await crypto.subtle.sign('HMAC', key, encoder.encode(input))
  return toHex(new Uint8Array(sig))
}
