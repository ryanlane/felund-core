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

// ── AES-256-GCM message encryption ────────────────────────────────────────────

/** Base64-encode bytes given as a Uint8Array or ArrayBuffer. */
const toBase64 = (buf: Uint8Array | ArrayBuffer): string => {
  const bytes = buf instanceof Uint8Array ? buf : new Uint8Array(buf)
  let binary = ''
  for (const b of bytes) binary += String.fromCharCode(b)
  return btoa(binary)
}

/**
 * Decode a base64 string to an ArrayBuffer.
 * ArrayBuffer unconditionally satisfies the BufferSource constraint in Web Crypto
 * API typings, avoiding the Uint8Array<ArrayBufferLike> vs Uint8Array<ArrayBuffer>
 * assignability issue in stricter TypeScript versions.
 */
const fromBase64 = (b64: string): ArrayBuffer => {
  const binary = atob(b64)
  const buf = new ArrayBuffer(binary.length)
  const view = new Uint8Array(buf)
  for (let i = 0; i < binary.length; i++) view[i] = binary.charCodeAt(i)
  return buf
}

export interface EncPayload {
  alg: string
  key_id: string
  nonce: string     // base64, 12 bytes
  ciphertext: string // base64, ciphertext + 16-byte GCM tag
}

/** Hex-string secret → non-extractable AES-256-GCM key via HKDF-SHA256. */
export const deriveMessageKey = async (secretHex: string): Promise<CryptoKey> => {
  // Use new Uint8Array(Array<number>) to get an ArrayBuffer-backed view,
  // which satisfies the stricter BufferSource constraints in Web Crypto typings.
  const rawBytes = new Uint8Array(
    (secretHex.match(/.{1,2}/g) ?? []).map((pair) => parseInt(pair, 16)),
  )
  const baseKey = await crypto.subtle.importKey('raw', rawBytes, 'HKDF', false, ['deriveKey'])
  return crypto.subtle.deriveKey(
    {
      name: 'HKDF',
      hash: 'SHA-256',
      salt: new ArrayBuffer(0),
      info: new Uint8Array(encoder.encode('felund-msg-v1')),
    },
    baseKey,
    { name: 'AES-GCM', length: 256 },
    false,
    ['encrypt', 'decrypt'],
  )
}

type ClearFields = {
  msgId: string
  circleId: string
  channelId: string
  authorNodeId: string
  createdTs: number
}

/** Build the AAD bytes as an ArrayBuffer so it satisfies BufferSource directly. */
const buildAad = (c: ClearFields): ArrayBuffer => {
  const src = encoder.encode(
    `${c.msgId}|${c.circleId}|${c.channelId}|${c.authorNodeId}|${c.createdTs}`,
  )
  const buf = new ArrayBuffer(src.byteLength)
  new Uint8Array(buf).set(src)
  return buf
}

/** Encrypt display_name + text with AES-256-GCM; header fields become AAD. */
export const encryptMessageFields = async (
  key: CryptoKey,
  clearFields: ClearFields,
  privateFields: { displayName: string; text: string },
): Promise<EncPayload> => {
  const nonce = new Uint8Array(12)
  crypto.getRandomValues(nonce)
  const plaintext = new Uint8Array(
    encoder.encode(
      JSON.stringify({ display_name: privateFields.displayName, text: privateFields.text }),
    ),
  )
  const ciphertext = await crypto.subtle.encrypt(
    { name: 'AES-GCM', iv: nonce, additionalData: buildAad(clearFields) },
    key,
    plaintext,
  )
  return {
    alg: 'AES-256-GCM',
    key_id: 'epoch-0',
    nonce: toBase64(nonce),
    ciphertext: toBase64(ciphertext),
  }
}

/** Decrypt an EncPayload; throws DOMException on auth failure. */
export const decryptMessageFields = async (
  key: CryptoKey,
  enc: EncPayload,
  clearFields: ClearFields,
): Promise<{ displayName: string; text: string }> => {
  const nonce = fromBase64(enc.nonce)
  const ciphertext = fromBase64(enc.ciphertext)
  const plaintext = await crypto.subtle.decrypt(
    { name: 'AES-GCM', iv: nonce, additionalData: buildAad(clearFields) },
    key,
    ciphertext,
  )
  const data = JSON.parse(new TextDecoder().decode(plaintext)) as Record<string, unknown>
  return {
    displayName: String(data['display_name'] ?? ''),
    text: String(data['text'] ?? ''),
  }
}
