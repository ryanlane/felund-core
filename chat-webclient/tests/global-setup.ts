import { spawn } from 'child_process'
import { mkdirSync, writeFileSync } from 'fs'
import path from 'path'
import { fileURLToPath } from 'url'

const __dirname = path.dirname(fileURLToPath(import.meta.url))

const RELAY_BASE = process.env.FELUND_RELAY_BASE || 'http://127.0.0.1:8765'
const RELAY_STATE = path.resolve(__dirname, '..', '.playwright', 'relay.json')

const waitForHealth = async (base: string): Promise<void> => {
  const url = `${base.replace(/\/+$/, '')}/v1/health`
  const deadline = Date.now() + 20_000

  while (Date.now() < deadline) {
    try {
      const resp = await fetch(url)
      if (resp.ok) return
    } catch {
      // retry
    }
    await new Promise((resolve) => setTimeout(resolve, 300))
  }

  throw new Error(`Relay health check timed out (${url})`)
}

export default async function globalSetup(): Promise<void> {
  if (process.env.FELUND_RELAY_EXTERNAL === '1') {
    return
  }

  const relayUrl = new URL(RELAY_BASE)
  const host = relayUrl.hostname || '127.0.0.1'
  const port = relayUrl.port || '8765'
  const relayPath = path.resolve(__dirname, '..', '..', 'api', 'relay_ws.py')
  const dbPath = path.resolve(__dirname, '..', '.playwright', 'relay.sqlite')

  mkdirSync(path.dirname(dbPath), { recursive: true })

  const python = process.env.FELUND_RELAY_PYTHON || 'python3'
  const relayProc = spawn(
    python,
    [relayPath, '--host', host, '--port', port, '--db', dbPath],
    {
      stdio: 'ignore',
    },
  )

  if (!relayProc.pid) {
    throw new Error('Failed to start relay server process')
  }

  writeFileSync(
    RELAY_STATE,
    JSON.stringify({ pid: relayProc.pid, host, port }, null, 2),
  )

  await waitForHealth(RELAY_BASE)
}
