import { useEffect, useRef, useState } from 'react'
import type { FormEvent } from 'react'
import './App.css'
import { parseInviteCode, makeInviteCode } from './core/invite'
import type { ChatMessage, State } from './core/models'
import {
  createChannel,
  createCircle,
  joinCircle,
  loadState,
  saveState,
  sendMessage,
  visibleMessages,
} from './core/state'
import { sha256Hex } from './core/crypto'
import {
  healthCheck,
  lookupPeers,
  normalizeRendezvousBase,
  registerPresence,
  unregisterPresence,
} from './network/rendezvous'
import { pushMessages, pullMessages, verifyMessageMac } from './network/relay'

const formatTime = (ts: number): string => {
  const d = new Date(ts * 1000)
  return `${String(d.getHours()).padStart(2, '0')}:${String(d.getMinutes()).padStart(2, '0')}`
}

function App() {
  const [state, setState] = useState<State | null>(null)
  const [mode, setMode] = useState<'host' | 'join'>('host')
  const [displayName, setDisplayName] = useState('anon')
  const [circleName, setCircleName] = useState('')
  const [inviteInput, setInviteInput] = useState('')
  const [rendezvousInput, setRendezvousInput] = useState('')
  const [composer, setComposer] = useState('')
  const [syncStatus, setSyncStatus] = useState('')
  const [status, setStatus] = useState('')
  const [peerCount, setPeerCount] = useState(0)
  const [showSettings, setShowSettings] = useState(false)
  const [showInvite, setShowInvite] = useState(false)
  const [inviteCopied, setInviteCopied] = useState(false)

  // Keep a ref to the latest state so polling closures always see fresh data
  const stateRef = useRef<State | null>(null)
  useEffect(() => {
    stateRef.current = state
  }, [state])

  // Ref for auto-scrolling the message list
  const messagesEndRef = useRef<HTMLDivElement>(null)

  // ── Auto-scroll on new messages or channel switch ─────────────────────────

  useEffect(() => {
    messagesEndRef.current?.scrollIntoView({ behavior: 'instant' })
  }, [state?.messages, state?.currentCircleId, state?.currentChannelId])

  // ── Keyboard shortcuts: F1=Settings, F2=Invite, Escape=close modals ───────

  useEffect(() => {
    const handler = (e: globalThis.KeyboardEvent) => {
      if (e.key === 'F1') {
        e.preventDefault()
        setShowSettings((v) => !v)
      }
      if (e.key === 'F2') {
        e.preventDefault()
        setShowInvite((v) => !v)
      }
      if (e.key === 'Escape') {
        setShowSettings(false)
        setShowInvite(false)
        setStatus('')
      }
    }
    window.addEventListener('keydown', handler)
    return () => window.removeEventListener('keydown', handler)
  }, [])

  // ── Initial load ──────────────────────────────────────────────────────────

  useEffect(() => {
    void (async () => {
      const loaded = await loadState()
      setState(loaded)
      setDisplayName(loaded.node.displayName || 'anon')
      setRendezvousInput(loaded.settings.rendezvousBase || '')
    })()
  }, [])

  // ── Presence registration (every 60 s) ────────────────────────────────────

  useEffect(() => {
    if (!state) return

    const base = normalizeRendezvousBase(state.settings.rendezvousBase)
    const circleIds = Object.keys(state.circles)
    if (!base || circleIds.length === 0) return

    let active = true
    const nodeId = state.node.nodeId

    const registerAll = async () => {
      for (const circleId of circleIds) {
        if (!active) return
        try {
          await registerPresence(base, { nodeId, circleId, ttlS: 120 })
        } catch {
          /* non-fatal */
        }
      }
    }

    const unregisterAll = async () => {
      for (const circleId of circleIds) {
        try {
          await unregisterPresence(base, { nodeId, circleId })
        } catch {
          /* best-effort */
        }
      }
    }

    void registerAll()
    const timerId = window.setInterval(() => void registerAll(), 60_000)
    window.addEventListener('beforeunload', () => void unregisterAll())
    return () => {
      active = false
      window.clearInterval(timerId)
      void unregisterAll()
    }
  }, [state?.settings.rendezvousBase, state?.node.nodeId, state?.circles])

  // ── Relay sync + peer count (every 5 s) ───────────────────────────────────

  const rendezvousBase = state?.settings.rendezvousBase ?? ''
  useEffect(() => {
    const base = normalizeRendezvousBase(rendezvousBase)
    if (!base) return

    let stopped = false

    // Cursors are local to this effect instance so React Strict Mode's
    // mount→cleanup→remount cycle starts each instance with since=0,
    // guaranteeing the full message history is fetched on every fresh mount.
    const cursors: Record<string, number> = {}

    // Track which of OUR OWN messages have been confirmed pushed this session.
    // We only push own messages (others are already in the relay) and skip
    // already-pushed ones to avoid hammering the server every 5 s.
    const pushedMsgIds = new Set<string>()

    const syncAll = async () => {
      const s = stateRef.current
      if (!s || stopped) return

      for (const [circleId, circle] of Object.entries(s.circles)) {
        try {
          // Push only OUR messages not yet confirmed sent this session.
          const outgoing = Object.values(s.messages).filter(
            (m) =>
              m.circleId === circleId &&
              m.authorNodeId === s.node.nodeId &&
              !pushedMsgIds.has(m.msgId),
          )
          if (outgoing.length > 0) {
            try {
              await pushMessages(base, circleId, outgoing)
              for (const m of outgoing) pushedMsgIds.add(m.msgId)
            } catch {
              /* push failure is non-fatal — will retry next cycle */
            }
          }

          const since = cursors[circleId] ?? 0
          const { messages: incoming, serverTime } = await pullMessages(base, circleId, since)

          const currentMessages = stateRef.current?.messages ?? {}
          const newMsgs: ChatMessage[] = []
          for (const msg of incoming) {
            if (currentMessages[msg.msgId]) continue
            if (msg.circleId !== circleId) continue
            const valid = await verifyMessageMac(circle.secretHex, msg)
            if (!valid) {
              console.warn(
                `[felund] MAC fail: msg=${msg.msgId.slice(0, 8)} from=${msg.displayName}`,
              )
              continue
            }
            newMsgs.push(msg)
          }

          if (newMsgs.length > 0 && !stopped) {
            setState((prev) => {
              if (!prev) return prev
              const next = { ...prev, messages: { ...prev.messages } }
              for (const msg of newMsgs) {
                next.messages[msg.msgId] = msg
              }
              void saveState(next)
              return next
            })
            setSyncStatus(`↓ ${newMsgs.length} new`)
            setTimeout(() => setSyncStatus(''), 3_000)
          }

          // Use serverTime - 1 so messages stored in the same server-second as
          // the pull are still visible next cycle (caught by stored_at > since).
          // msgId dedup above prevents showing them twice.
          if (serverTime > 0) {
            cursors[circleId] = Math.max(since, serverTime - 1)
          }
        } catch (err) {
          console.warn(`[felund] sync error circle=${circleId.slice(0, 8)}:`, err)
        }
      }

      // Update peer count for the active circle
      const s2 = stateRef.current
      if (s2?.currentCircleId && !stopped) {
        try {
          const peers = await lookupPeers(base, s2.node.nodeId, s2.currentCircleId, 50)
          if (!stopped) setPeerCount(peers.length)
        } catch {
          /* non-fatal */
        }
      }
    }

    void syncAll()
    const timerId = window.setInterval(() => void syncAll(), 5_000)
    return () => {
      stopped = true
      window.clearInterval(timerId)
    }
  }, [rendezvousBase])

  // ── State persistence helper ──────────────────────────────────────────────

  const persist = async (next: State) => {
    setState({ ...next })
    await saveState(next)
  }

  // ── Setup form ────────────────────────────────────────────────────────────

  const handleSetup = async (event: FormEvent) => {
    event.preventDefault()
    if (!state) return

    const next = { ...state }
    next.node.displayName = displayName.trim() || 'anon'
    next.settings = {
      ...next.settings,
      rendezvousBase: normalizeRendezvousBase(rendezvousInput),
    }

    try {
      if (mode === 'host') {
        await createCircle(next, circleName)
      } else {
        const parsed = parseInviteCode(inviteInput.trim())
        const circleId = (await sha256Hex(parsed.secretHex)).slice(0, 24)
        joinCircle(next, {
          circleId,
          secretHex: parsed.secretHex,
          name: '',
          isOwned: false,
        })
      }
      await persist(next)
    } catch (error) {
      setStatus(`Setup failed: ${error instanceof Error ? error.message : String(error)}`)
    }
  }

  // ── Message send ──────────────────────────────────────────────────────────

  const handleSend = async (event: FormEvent) => {
    event.preventDefault()
    if (!state || !composer.trim()) return

    const next = { ...state }
    const input = composer.trim()
    setComposer('')

    try {
      if (input.startsWith('/')) {
        await handleCommand(next, input)
      } else {
        await sendMessage(next, input)
      }
      await persist(next)
    } catch (error) {
      setStatus(`Error: ${error instanceof Error ? error.message : String(error)}`)
    }
  }

  // ── Slash commands ────────────────────────────────────────────────────────

  const handleCommand = async (next: State, commandText: string) => {
    const parts = commandText.split(/\s+/)
    const cmd = parts[0]?.toLowerCase()

    if (cmd === '/help') {
      setStatus(
        'Commands: /help  /name <name>  /invite  /settings  /channels  ' +
          '/channel create|switch <name>  /join <code>',
      )
      return
    }

    if (cmd === '/name') {
      const newName = parts.slice(1).join(' ').trim()
      if (!newName) {
        setStatus(`Current name: ${next.node.displayName}`)
        return
      }
      next.node.displayName = newName.slice(0, 40)
      setStatus('Display name updated.')
      return
    }

    if (cmd === '/invite') {
      setShowInvite(true)
      return
    }

    if (cmd === '/settings') {
      setShowSettings(true)
      return
    }

    if (cmd === '/channels') {
      const currentCircleId = next.currentCircleId
      if (!currentCircleId) throw new Error('No active circle')
      const ids = Object.keys(next.channels[currentCircleId] ?? {})
      setStatus(`Channels: ${ids.join(', ')}`)
      return
    }

    if (cmd === '/channel' && parts[1] === 'create') {
      const channelId = parts[2]
      if (!channelId) throw new Error('Usage: /channel create <name>')
      if (!next.currentCircleId) throw new Error('No active circle')
      createChannel(next, next.currentCircleId, channelId)
      setStatus(`Created #${channelId}`)
      return
    }

    if (cmd === '/channel' && parts[1] === 'switch') {
      const channelId = parts[2]?.replace(/^#/, '')
      if (!channelId) throw new Error('Usage: /channel switch <name>')
      if (!next.currentCircleId) throw new Error('No active circle')
      const channel = next.channels[next.currentCircleId]?.[channelId]
      if (!channel) throw new Error(`Unknown channel #${channelId}`)
      next.currentChannelId = channel.channelId
      setStatus(`Switched to #${channel.channelId}`)
      return
    }

    if (cmd === '/join') {
      const code = parts[1]
      if (!code) throw new Error('Usage: /join <invite-code>')
      const parsed = parseInviteCode(code)
      const circleId = (await sha256Hex(parsed.secretHex)).slice(0, 24)
      joinCircle(next, { circleId, secretHex: parsed.secretHex, name: '', isOwned: false })
      setStatus(`Joined circle ${circleId.slice(0, 8)}. Sync will start shortly.`)
      return
    }

    throw new Error('Unknown command. Try /help')
  }

  // ── Settings helpers ──────────────────────────────────────────────────────

  const saveSettings = async () => {
    if (!state) return
    const next = {
      ...state,
      node: { ...state.node, displayName: displayName.trim() || 'anon' },
      settings: {
        ...state.settings,
        rendezvousBase: normalizeRendezvousBase(rendezvousInput),
      },
    }
    await persist(next)
    setShowSettings(false)
    setStatus('Settings saved.')
  }

  const testRendezvousHealth = async () => {
    const base = normalizeRendezvousBase(rendezvousInput || state?.settings.rendezvousBase || '')
    if (!base) {
      setStatus('Set a rendezvous server first.')
      return
    }
    try {
      const info = await healthCheck(base)
      setStatus(`Rendezvous OK (${info.version ?? 'unknown version'})`)
    } catch (error) {
      setStatus(`Rendezvous check failed: ${error instanceof Error ? error.message : String(error)}`)
    }
  }

  // ── Navigation ────────────────────────────────────────────────────────────

  const selectCircleAndChannel = async (circleId: string, channelId: string) => {
    const next = { ...state!, currentCircleId: circleId, currentChannelId: channelId }
    await persist(next)
  }

  // ── Invite code copy ──────────────────────────────────────────────────────

  const copyInviteCode = async (code: string) => {
    try {
      await navigator.clipboard.writeText(code)
      setInviteCopied(true)
      setTimeout(() => setInviteCopied(false), 2_000)
    } catch {
      setStatus('Copy failed — select the code manually.')
    }
  }

  // ── Render ────────────────────────────────────────────────────────────────

  if (!state) {
    return (
      <div className="tui-app">
        <div className="tui-header">
          <span className="tui-title">felundchat</span>
        </div>
        <div className="tui-setup-overlay">
          <p className="tui-dim">Loading…</p>
        </div>
      </div>
    )
  }

  const circles = Object.values(state.circles)
  const hasCircles = circles.length > 0
  const currentCircleId = state.currentCircleId
  const currentCircle = currentCircleId ? state.circles[currentCircleId] : undefined
  const currentChannelId = state.currentChannelId ?? 'general'
  const messages = visibleMessages(state)
  const inviteCode = currentCircle
    ? makeInviteCode(
        currentCircle.secretHex,
        normalizeRendezvousBase(state.settings.rendezvousBase) || 'relay',
      )
    : ''

  // ── Setup Screen ──────────────────────────────────────────────────────────

  if (!hasCircles) {
    return (
      <div className="tui-app">
        <div className="tui-header">
          <span className="tui-title">felundchat</span>
          <span className="tui-header-dim"> — setup</span>
        </div>
        <div className="tui-setup-overlay">
          <form className="tui-setup-form" onSubmit={handleSetup}>
            <div className="tui-modal-header">Setup</div>
            <div className="tui-modal-body">
              <label>
                Display name
                <input
                  value={displayName}
                  onChange={(e) => setDisplayName(e.target.value)}
                  placeholder="anon"
                  autoFocus
                />
              </label>
              <div className="tui-tab-row">
                <button
                  type="button"
                  className={`tui-tab ${mode === 'host' ? 'active' : ''}`}
                  onClick={() => setMode('host')}
                >
                  Host
                </button>
                <button
                  type="button"
                  className={`tui-tab ${mode === 'join' ? 'active' : ''}`}
                  onClick={() => setMode('join')}
                >
                  Join
                </button>
              </div>
              {mode === 'host' ? (
                <label>
                  Circle name (optional)
                  <input
                    value={circleName}
                    onChange={(e) => setCircleName(e.target.value)}
                    placeholder="my-group"
                  />
                </label>
              ) : (
                <label>
                  Invite code
                  <textarea
                    value={inviteInput}
                    onChange={(e) => setInviteInput(e.target.value)}
                    rows={4}
                    placeholder="Paste invite code here…"
                  />
                </label>
              )}
              <label>
                Rendezvous server
                <input
                  value={rendezvousInput}
                  onChange={(e) => setRendezvousInput(e.target.value)}
                  placeholder="https://your-relay-server/api"
                />
              </label>
              {status && <p className="tui-error">{status}</p>}
            </div>
            <div className="tui-modal-actions">
              <button type="submit" className="tui-btn primary">
                {mode === 'host' ? 'Create circle' : 'Join circle'}
              </button>
            </div>
          </form>
        </div>
        <div className="tui-footer">
          <span>Enter to submit · Tab to switch fields</span>
        </div>
      </div>
    )
  }

  // ── Chat Screen ───────────────────────────────────────────────────────────

  return (
    <div className="tui-app">
      {/* Header */}
      <div className="tui-header">
        <span className="tui-title">felundchat</span>
        {currentCircle && (
          <>
            <span className="tui-header-sep"> │ </span>
            <span className="tui-header-circle">
              {currentCircle.name || currentCircleId?.slice(0, 8)}
            </span>
            <span className="tui-header-sep"> │ </span>
            <span className="tui-header-channel">#{currentChannelId}</span>
            <span className="tui-header-sep"> │ </span>
            <span className="tui-header-peers">
              {peerCount} peer{peerCount !== 1 ? 's' : ''}
            </span>
          </>
        )}
        {syncStatus && <span className="tui-sync-status">{syncStatus}</span>}
      </div>

      {/* Body: sidebar + messages */}
      <div className="tui-body">
        <aside className="tui-sidebar">
          <div className="tui-sidebar-section">Circles</div>
          {circles.map((circle) => {
            const channels = Object.keys(state.channels[circle.circleId] ?? {})
            const isCurrentCircle = circle.circleId === currentCircleId
            return (
              <div key={circle.circleId}>
                <div
                  className={`tui-circle-item ${isCurrentCircle ? 'active' : ''}`}
                  onClick={() => {
                    const firstChan = channels[0] ?? 'general'
                    void selectCircleAndChannel(circle.circleId, firstChan)
                  }}
                >
                  {isCurrentCircle ? '●' : '○'}{' '}
                  {circle.name || circle.circleId.slice(0, 8)}
                </div>
                {channels.map((channelId) => {
                  const isActive = isCurrentCircle && channelId === currentChannelId
                  return (
                    <div
                      key={channelId}
                      className={`tui-channel-item ${isActive ? 'active' : ''}`}
                      onClick={() => void selectCircleAndChannel(circle.circleId, channelId)}
                    >
                      {isActive ? '▶' : ' '} #{channelId}
                    </div>
                  )
                })}
              </div>
            )
          })}
        </aside>

        {/* Message log */}
        <div className="tui-main">
          <div className="tui-messages">
            {messages.length === 0 && (
              <div className="tui-empty">No messages yet — type below to start chatting.</div>
            )}
            {messages.map((msg) => (
              <div key={msg.msgId} className="tui-message">
                <span className="tui-ts">[{formatTime(msg.createdTs)}]</span>{' '}
                <span
                  className={`tui-author${msg.authorNodeId === state.node.nodeId ? ' is-self' : ''}`}
                >
                  {msg.displayName}
                </span>
                <span className="tui-colon">:</span>{' '}
                <span className="tui-text">{msg.text}</span>
              </div>
            ))}
            <div ref={messagesEndRef} />
          </div>
        </div>
      </div>

      {/* Status bar — click to dismiss */}
      {status && (
        <div className="tui-status-bar" onClick={() => setStatus('')}>
          {status} <span className="tui-dim">[click to dismiss]</span>
        </div>
      )}

      {/* Input bar */}
      <div className="tui-input-row">
        <span className="tui-prompt">&gt;</span>
        <form className="tui-input-form" onSubmit={handleSend}>
          <input
            value={composer}
            onChange={(e) => setComposer(e.target.value)}
            placeholder="Type a message or /command…"
            autoFocus
          />
        </form>
      </div>

      {/* Footer */}
      <div className="tui-footer">
        <span>
          <kbd>F1</kbd> Settings
        </span>
        <span>
          <kbd>F2</kbd> Invite
        </span>
        <span>/help</span>
        <span className="tui-footer-node">node: {state.node.nodeId.slice(0, 8)}</span>
      </div>

      {/* Settings Modal */}
      {showSettings && (
        <div className="tui-modal-overlay" onClick={() => setShowSettings(false)}>
          <div className="tui-modal" onClick={(e) => e.stopPropagation()}>
            <div className="tui-modal-header">Settings</div>
            <div className="tui-modal-body">
              <label>
                Display name
                <input
                  value={displayName}
                  onChange={(e) => setDisplayName(e.target.value)}
                  autoFocus
                />
              </label>
              <label>
                Rendezvous server
                <input
                  value={rendezvousInput}
                  onChange={(e) => setRendezvousInput(e.target.value)}
                  placeholder="https://your-relay-server/api"
                />
              </label>
              <p className="tui-dim" style={{ margin: 0, fontSize: '0.78rem' }}>
                node: {state.node.nodeId}
              </p>
            </div>
            <div className="tui-modal-actions">
              <button className="tui-btn" onClick={() => void testRendezvousHealth()}>
                Test relay
              </button>
              <button className="tui-btn" onClick={() => setShowSettings(false)}>
                Cancel
              </button>
              <button className="tui-btn primary" onClick={() => void saveSettings()}>
                Save
              </button>
            </div>
          </div>
        </div>
      )}

      {/* Invite Modal */}
      {showInvite && inviteCode && (
        <div className="tui-modal-overlay" onClick={() => setShowInvite(false)}>
          <div className="tui-modal" onClick={(e) => e.stopPropagation()}>
            <div className="tui-modal-header">
              Invite Code — {currentCircle?.name || currentCircleId?.slice(0, 8)}
            </div>
            <div className="tui-modal-body">
              <p className="tui-dim" style={{ margin: 0, fontSize: '0.78rem' }}>
                Share this code with others to join{' '}
                <strong>{currentCircle?.name || 'this circle'}</strong>.
              </p>
              <pre className="tui-invite-code">{inviteCode}</pre>
            </div>
            <div className="tui-modal-actions">
              <button className="tui-btn" onClick={() => setShowInvite(false)}>
                Close
              </button>
              <button className="tui-btn primary" onClick={() => void copyInviteCode(inviteCode)}>
                {inviteCopied ? '✓ Copied!' : 'Copy to clipboard'}
              </button>
            </div>
          </div>
        </div>
      )}
    </div>
  )
}

export default App
