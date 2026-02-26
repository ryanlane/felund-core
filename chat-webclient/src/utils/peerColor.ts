export type TimeFormat = '24h' | '12h'

export const formatTime = (ts: number, format: TimeFormat = '24h'): string => {
  const d = new Date(ts * 1000)
  const m = String(d.getMinutes()).padStart(2, '0')
  if (format === '12h') {
    const h = d.getHours()
    const ampm = h >= 12 ? 'PM' : 'AM'
    const hDisplay = String(h % 12 || 12).padStart(2, ' ')
    return `${hDisplay}:${m} ${ampm}`
  }
  const hDisplay = String(d.getHours()).padStart(2, ' ')
  return `${hDisplay}:${m}`
}

export const formatFullTimestamp = (ts: number, format: TimeFormat = '24h'): string => {
  const d = new Date(ts * 1000)
  const month = d.toLocaleString('en-US', { month: 'long' })
  const date = `${month} ${d.getDate()}, ${d.getFullYear()}`
  return `${date}  ${formatTime(ts, format)}`
}

export const formatDayHeader = (ts: number): string => {
  const d = new Date(ts * 1000)
  const now = new Date()
  const yesterday = new Date(now)
  yesterday.setDate(now.getDate() - 1)
  if (d.toDateString() === now.toDateString()) return 'Today'
  if (d.toDateString() === yesterday.toDateString()) return 'Yesterday'
  const day = d.toLocaleString('en-US', { weekday: 'long' })
  const month = d.toLocaleString('en-US', { month: 'long' })
  return `${day}, ${month} ${d.getDate()}, ${d.getFullYear()}`
}

export const isSameDay = (ts1: number, ts2: number): boolean =>
  new Date(ts1 * 1000).toDateString() === new Date(ts2 * 1000).toDateString()

// Deterministic per-peer color palette — mirrors the Python TUI _peer_color palette.
const PEER_COLORS = [
  '#00c8c0', '#cda31a', '#c060a0', '#00e0d8',
  '#ffe04a', '#ff70c8', '#e66700', '#ff5080',
  '#9c9c9c', '#2669dd', '#c36f62', '#60b0e0',
]

export const peerColor = (nodeId: string, allNodeIds?: string[]): string => {
  if (allNodeIds && allNodeIds.length > 0) {
    const idx = allNodeIds.indexOf(nodeId)
    if (idx !== -1) {
      return PEER_COLORS[idx % PEER_COLORS.length]
    }
  }

  let h = 5381
  for (let i = 0; i < nodeId.length; i++) {
    h = ((h << 5) + h + nodeId.charCodeAt(i)) | 0
  }
  return PEER_COLORS[Math.abs(h) % PEER_COLORS.length]
}
