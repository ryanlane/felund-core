export const formatTime = (ts: number): string => {
  const d = new Date(ts * 1000)
  return `${String(d.getHours()).padStart(2, '0')}:${String(d.getMinutes()).padStart(2, '0')}`
}

// Deterministic per-peer color palette — mirrors the Python TUI _peer_color palette.
const PEER_COLORS = [
  '#00c8c0', '#e8c44a', '#c060a0', '#00e0d8',
  '#ffe04a', '#ff70c8', '#e07820', '#ff5080',
  '#78c830', '#6090e0', '#e07868', '#60b0e0',
]

export const peerColor = (nodeId: string): string => {
  let h = 5381
  for (let i = 0; i < nodeId.length; i++) {
    h = ((h << 5) + h + nodeId.charCodeAt(i)) | 0
  }
  return PEER_COLORS[Math.abs(h) % PEER_COLORS.length]
}
