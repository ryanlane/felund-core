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

/**
 * SHA-256 of the raw bytes represented by a hex string.
 *
 * Used for circle ID derivation to match the Python client, which hashes the
 * raw 32-byte secret rather than its hex-encoded form:
 *   Python: sha256(bytes.fromhex(secret_hex))
 *   Web:    sha256HexFromRawKey(secretHex)
 */
export const sha256HexFromRawKey = async (hexInput: string): Promise<string> => {
  const bytes = Uint8Array.from(
    (hexInput.match(/.{1,2}/g) ?? []).map((pair) => parseInt(pair, 16)),
  )
  const digest = await crypto.subtle.digest('SHA-256', bytes)
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
