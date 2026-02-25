import { readFileSync } from 'fs'
import path from 'path'
import { fileURLToPath } from 'url'

const __dirname = path.dirname(fileURLToPath(import.meta.url))

const RELAY_STATE = path.resolve(__dirname, '..', '.playwright', 'relay.json')

export default async function globalTeardown(): Promise<void> {
  if (process.env.FELUND_RELAY_EXTERNAL === '1') {
    return
  }

  try {
    const raw = readFileSync(RELAY_STATE, 'utf8')
    const data = JSON.parse(raw) as { pid?: number }
    if (data.pid) {
      try {
        process.kill(data.pid)
      } catch {
        // already stopped
      }
    }
  } catch {
    // no relay state to clean up
  }
}
