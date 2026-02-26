export type TimeFormat = '24h' | '12h'

export const formatTime = (ts: number, format: TimeFormat = '24h'): string => {
  const d = new Date(ts * 1000)
  if (format === '12h') {
    const h = d.getHours()
    const m = String(d.getMinutes()).padStart(2, '0')
    const ampm = h >= 12 ? 'PM' : 'AM'
    return `${h % 12 || 12}:${m} ${ampm}`
  }
  return `${String(d.getHours()).padStart(2, '0')}:${String(d.getMinutes()).padStart(2, '0')}`
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
