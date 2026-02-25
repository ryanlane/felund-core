import { useEffect, useRef, useState } from 'react'
import type { FormEvent } from 'react'
import './App.css'
import { parseInviteCode, makeInviteCode } from './core/invite'
import type { ChatMessage, State } from './core/models'
import {
  applyControlEvents,
  createCall,
  createChannel,
  createCircle,
  endCall,
  joinCall,
  joinCircle,
  leaveCall,
  loadState,
  saveState,
  sendMessage,
  visibleMessages,
} from './core/state'
import { sha256HexFromRawKey } from './core/crypto'
import {
  healthCheck,
  lookupPeers,
  normalizeRendezvousBase,
  registerPresence,
  unregisterPresence,
} from './network/rendezvous'
import { pushMessages, pullMessages, openRelayWS } from './network/relay'
import type { WsStatus } from './network/relay'
import { WebRTCTransport } from './network/transport'
import { WebRTCCallManager } from './network/call'
import type { CallPeerState } from './network/call'

const formatTime = (ts: number): string => {
  const d = new Date(ts * 1000)
  return `${String(d.getHours()).padStart(2, '0')}:${String(d.getMinutes()).padStart(2, '0')}`
}

// Deterministic per-peer color palette — mirrors the Python TUI _peer_color palette.
const PEER_COLORS = [
  '#00c8c0', '#e8c44a', '#c060a0', '#00e0d8',
  '#ffe04a', '#ff70c8', '#e07820', '#ff5080',
  '#78c830', '#6090e0', '#e07868', '#60b0e0',
]

const peerColor = (nodeId: string): string => {
  let h = 5381
  for (let i = 0; i < nodeId.length; i++) {
    h = ((h << 5) + h + nodeId.charCodeAt(i)) | 0
  }
  return PEER_COLORS[Math.abs(h) % PEER_COLORS.length]
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
  const [showHelp, setShowHelp] = useState(false)
  const [showCall, setShowCall] = useState(false)
  const [inviteCopied, setInviteCopied] = useState(false)
  const [wsLive, setWsLive] = useState(false)
  const wsLiveCountRef = useRef(0)
  const [p2pCount, setP2pCount] = useState(0)
  // Map from circleId → WebRTCTransport (one per circle)
  const webrtcRef = useRef<Map<string, WebRTCTransport>>(new Map())

  // ── Call media state ───────────────────────────────────────────────────────
  const [isMuted, setIsMuted] = useState(false)
  const [isVideoOn, setIsVideoOn] = useState(false)
  const [remoteStreams, setRemoteStreams] = useState<Record<string, MediaStream>>({})
  const [callPeerStates, setCallPeerStates] = useState<Record<string, CallPeerState>>({})
  const callManagerRef = useRef<WebRTCCallManager | null>(null)

  // ── TURN server settings form state ───────────────────────────────────────
  const [turnUrl, setTurnUrl] = useState('')
  const [turnUsername, setTurnUsername] = useState('')
  const [turnCredential, setTurnCredential] = useState('')

  // Keep a ref to the latest state so polling closures always see fresh data
  const stateRef = useRef<State | null>(null)
  useEffect(() => {
    stateRef.current = state
  }, [state])

  // Ref for auto-scrolling the message list
  const messagesEndRef = useRef<HTMLDivElement>(null)

  // Ref for focusing the composer input (used by Escape shortcut)
  const inputRef = useRef<HTMLInputElement>(null)

  // ── Auto-scroll on new messages or channel switch ─────────────────────────

  useEffect(() => {
    messagesEndRef.current?.scrollIntoView({ behavior: 'instant' })
  }, [state?.messages, state?.currentCircleId, state?.currentChannelId])

  // ── Keyboard shortcuts: F1=Help, F2=Invite, F3=Settings, Escape=close+focus ─

  useEffect(() => {
    const handler = (e: globalThis.KeyboardEvent) => {
      if (e.key === 'F1') {
        e.preventDefault()
        setShowHelp((v) => !v)
      }
      if (e.key === 'F2') {
        e.preventDefault()
        setShowInvite((v) => !v)
      }
      if (e.key === 'F3') {
        e.preventDefault()
        setShowSettings((v) => !v)
      }
      if (e.key === 'F4') {
        e.preventDefault()
        setShowCall((v) => !v)
      }
      if (e.key === 'Escape') {
        setShowHelp(false)
        setShowSettings(false)
        setShowInvite(false)
        setShowCall(false)
        setStatus('')
        inputRef.current?.focus()
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
      setTurnUrl(loaded.settings.turnUrl ?? '')
      setTurnUsername(loaded.settings.turnUsername ?? '')
      setTurnCredential(loaded.settings.turnCredential ?? '')
    })()
  }, [])

  // ── Relay sync: register presence, push/pull messages, peer count ────────────
  // All relay operations run in a single loop so that presence registration
  // always completes before the peer lookup in every cycle.  Registration is
  // throttled to once per 60 s; message sync runs every 5 s.

  const rendezvousBase = state?.settings.rendezvousBase ?? ''
  useEffect(() => {
    const base = normalizeRendezvousBase(rendezvousBase)
    if (!base) return

    let stopped = false

    // Cursors local to this effect instance — Strict Mode safe (see note above).
    const cursors: Record<string, number> = {}
    // Only push our own messages not yet confirmed sent this session.
    const pushedMsgIds = new Set<string>()
    // Throttle presence registration to once per 60 s per circle.
    const lastRegisteredAt: Record<string, number> = {}
    const REGISTER_INTERVAL_MS = 60_000

    const unregisterAll = async (s: { node: { nodeId: string }; circles: Record<string, unknown> }) => {
      for (const circleId of Object.keys(s.circles)) {
        try {
          await unregisterPresence(base, { nodeId: s.node.nodeId, circleId })
        } catch {
          /* best-effort */
        }
      }
    }

    const syncAll = async () => {
      const s = stateRef.current
      if (!s || stopped) return

      const now = Date.now()

      // ── 1. Register presence (before peer lookup) ──────────────────────────
      for (const circleId of Object.keys(s.circles)) {
        if (!lastRegisteredAt[circleId] || now - lastRegisteredAt[circleId] > REGISTER_INTERVAL_MS) {
          try {
            await registerPresence(base, { nodeId: s.node.nodeId, circleId, ttlS: 120 })
            lastRegisteredAt[circleId] = now
          } catch {
            /* non-fatal */
          }
        }
      }

      // ── 2. Push + pull messages ────────────────────────────────────────────
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
              await pushMessages(base, circleId, circle.secretHex, outgoing)
              for (const m of outgoing) pushedMsgIds.add(m.msgId)
            } catch {
              /* push failure is non-fatal — will retry next cycle */
            }
          }

          const since = cursors[circleId] ?? 0
          const { messages: incoming, serverTime } = await pullMessages(
            base,
            circleId,
            circle.secretHex,
            since,
          )

          const currentMessages = stateRef.current?.messages ?? {}
          const newMsgs: ChatMessage[] = []
          for (const msg of incoming) {
            if (currentMessages[msg.msgId]) continue
            if (msg.circleId !== circleId) continue
            newMsgs.push(msg)
          }

          if (newMsgs.length > 0 && !stopped) {
            setState((prev) => {
              if (!prev) return prev
              const next = {
                ...prev,
                messages: { ...prev.messages },
                // Shallow-copy so applyControlEvents can safely replace per-circle dicts
                channels: { ...prev.channels },
                circles: { ...prev.circles },
              }
              for (const msg of newMsgs) {
                next.messages[msg.msgId] = msg
              }
              applyControlEvents(next, newMsgs)
              void saveState(next)
              return next
            })
            setSyncStatus(`↓ ${newMsgs.length} new`)
            setTimeout(() => setSyncStatus(''), 3_000)
          }

          // Use serverTime - 1 so messages stored in the same server-second as
          // the pull are still visible next cycle.  msgId dedup prevents doubles.
          if (serverTime > 0) {
            cursors[circleId] = Math.max(since, serverTime - 1)
          }
        } catch (err) {
          console.warn(`[felund] sync error circle=${circleId.slice(0, 8)}:`, err)
        }
      }

      // ── 3. Peer count + WebRTC connections ────────────────────────────────
      const s2 = stateRef.current
      if (s2?.currentCircleId && !stopped) {
        try {
          const peers = await lookupPeers(base, s2.node.nodeId, s2.currentCircleId, 50)
          if (!stopped) setPeerCount(peers.length)
          // Attempt direct WebRTC DataChannel connections to discovered peers.
          const transport = webrtcRef.current.get(s2.currentCircleId)
          if (transport && !stopped) {
            for (const peer of peers) {
              void transport.connectToPeer(peer.node_id)
            }
          }
        } catch {
          /* non-fatal */
        }
      }
    }

    const onBeforeUnload = () => {
      const s = stateRef.current
      if (s) void unregisterAll(s)
    }
    window.addEventListener('beforeunload', onBeforeUnload)

    void syncAll()
    const timerId = window.setInterval(() => void syncAll(), 5_000)
    return () => {
      stopped = true
      window.clearInterval(timerId)
      window.removeEventListener('beforeunload', onBeforeUnload)
      const s = stateRef.current
      if (s) void unregisterAll(s)
    }
  }, [rendezvousBase])

  // ── WebSocket relay — real-time push ──────────────────────────────────────
  // One WS connection per circle; auto-reconnects on close.
  // On connect the relay immediately delivers messages buffered in the last 2
  // minutes so the client doesn't need to HTTP-poll to catch up.

  const circlesKey = Object.keys(state?.circles ?? {}).sort().join(',')

  useEffect(() => {
    const base = normalizeRendezvousBase(rendezvousBase)
    if (!base) return
    const s = stateRef.current
    if (!s || circlesKey === '') return

    const cleanups: (() => void)[] = []
    wsLiveCountRef.current = 0

    const handleStatus = (status: WsStatus) => {
      if (status === 'live') {
        wsLiveCountRef.current += 1
        setWsLive(true)
      } else if (status === 'closed') {
        wsLiveCountRef.current = Math.max(0, wsLiveCountRef.current - 1)
        if (wsLiveCountRef.current === 0) setWsLive(false)
      }
    }

    const handleMessages = (msgs: ChatMessage[]) => {
      setState((prev) => {
        if (!prev) return prev
        const newMsgs = msgs.filter((m) => !prev.messages[m.msgId])
        if (newMsgs.length === 0) return prev
        const next = {
          ...prev,
          messages: { ...prev.messages },
          channels: { ...prev.channels },
          circles: { ...prev.circles },
        }
        for (const msg of newMsgs) next.messages[msg.msgId] = msg
        applyControlEvents(next, newMsgs)
        void saveState(next)
        setSyncStatus(`↓ ${newMsgs.length} new`)
        setTimeout(() => setSyncStatus(''), 3_000)
        return next
      })
    }

    for (const [circleId, circle] of Object.entries(s.circles)) {
      cleanups.push(
        openRelayWS(base, circleId, circle.secretHex, s.node.nodeId, handleMessages, handleStatus),
      )
    }

    return () => {
      wsLiveCountRef.current = 0
      setWsLive(false)
      cleanups.forEach((f) => f())
    }
  }, [rendezvousBase, circlesKey]) // eslint-disable-line react-hooks/exhaustive-deps

  // ── WebRTC DataChannel — direct peer-to-peer transport ────────────────────
  // One WebRTCTransport per circle.  Signal polling runs inside the transport.
  // connectToPeer() is called from the relay sync loop whenever peers are
  // discovered; here we only manage the transport lifecycle.

  useEffect(() => {
    const base = normalizeRendezvousBase(rendezvousBase)
    const s = stateRef.current
    if (!base || !s || circlesKey === '') return

    // Tear down transports for circles that no longer exist.
    for (const [cid, t] of webrtcRef.current) {
      if (!(cid in s.circles)) {
        t.destroy()
        webrtcRef.current.delete(cid)
      }
    }

    // Create transports for newly joined circles.
    for (const [circleId, circle] of Object.entries(s.circles)) {
      if (webrtcRef.current.has(circleId)) continue
      const transport = new WebRTCTransport({
        nodeId: s.node.nodeId,
        circleId,
        secretHex: circle.secretHex,
        rendezvousBase: base,
        getLocalMessages: () => Object.values(stateRef.current?.messages ?? {}),
        onMessages: (msgs) => {
          setState((prev) => {
            if (!prev) return prev
            const newMsgs = msgs.filter((m) => !prev.messages[m.msgId])
            if (newMsgs.length === 0) return prev
            const next = {
              ...prev,
              messages: { ...prev.messages },
              channels: { ...prev.channels },
              circles: { ...prev.circles },
            }
            for (const msg of newMsgs) next.messages[msg.msgId] = msg
            applyControlEvents(next, newMsgs)
            void saveState(next)
            return next
          })
        },
        onPeerCountChange: () => {
          const total = [...webrtcRef.current.values()].reduce(
            (sum, t) => sum + t.openCount,
            0,
          )
          setP2pCount(total)
        },
      })
      webrtcRef.current.set(circleId, transport)
    }

    return () => {
      for (const t of webrtcRef.current.values()) t.destroy()
      webrtcRef.current.clear()
      setP2pCount(0)
    }
  }, [rendezvousBase, circlesKey]) // eslint-disable-line react-hooks/exhaustive-deps

  // ── Call media manager lifecycle ──────────────────────────────────────────
  // Runs whenever activeCalls or the current circle/channel changes.
  // Everything is computed from stateRef.current to avoid stale closures.

  useEffect(() => {
    const s = stateRef.current
    if (!s) return

    // Compute active call from state (mirrors render body derivation).
    const circleId = s.currentCircleId
    const channelId = s.currentChannelId ?? 'general'
    const call = circleId
      ? Object.values(s.activeCalls).find(
          (c) => c.circleId === circleId && c.channelId === channelId,
        ) ?? null
      : null
    const isInCall = call?.participants.includes(s.node.nodeId) ?? false

    if (!isInCall || !call) {
      callManagerRef.current?.destroy()
      callManagerRef.current = null
      setRemoteStreams({})
      setCallPeerStates({})
      return
    }

    if (!circleId) return
    const base = normalizeRendezvousBase(s.settings.rendezvousBase)
    const circle = s.circles[circleId]
    if (!base || !circle) return

    // Build ICE server list (STUN + optional TURN)
    const iceServers: RTCIceServer[] = [
      { urls: 'stun:stun.l.google.com:19302' },
      { urls: 'stun:stun1.l.google.com:19302' },
    ]
    if (s.settings.turnUrl) {
      iceServers.push({
        urls: s.settings.turnUrl,
        username: s.settings.turnUsername,
        credential: s.settings.turnCredential,
      })
    }

    // Create manager if entering call for the first time
    if (!callManagerRef.current) {
      const mgr = new WebRTCCallManager({
        nodeId: s.node.nodeId,
        callSessionId: call.sessionId,
        circleId,
        rendezvousBase: base,
        iceServers,
        onRemoteStream: (peerId, stream) => {
          setRemoteStreams((prev) => ({ ...prev, [peerId]: stream }))
        },
        onRemoteStreamEnd: (peerId) => {
          setRemoteStreams((prev) => {
            const next = { ...prev }
            delete next[peerId]
            return next
          })
        },
        onPeerStateChange: (peerId, peerState) => {
          setCallPeerStates((prev) => ({ ...prev, [peerId]: peerState }))
        },
      })
      callManagerRef.current = mgr
      void (async () => {
        const ok = await mgr.startMedia(isVideoOn)
        if (!ok) {
          setStatus('Microphone access failed — check permissions or input device.')
        }
      })()
    }

    // Connect to any participant we haven't yet connected to
    const mgr = callManagerRef.current
    const otherParticipants = call.participants.filter((id) => id !== s.node.nodeId)
    for (const peerId of otherParticipants) {
      void mgr.connectToPeer(peerId)
    }
  }, [state]) // eslint-disable-line react-hooks/exhaustive-deps

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
        const circleId = (await sha256HexFromRawKey(parsed.secretHex)).slice(0, 24)
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
        const prevMsgIds = new Set(Object.keys(state.messages))
        await sendMessage(next, input)
        // Broadcast newly added messages to WebRTC peers for real-time delivery.
        const circleId = next.currentCircleId
        const transport = circleId ? webrtcRef.current.get(circleId) : undefined
        if (transport) {
          for (const msg of Object.values(next.messages)) {
            if (!prevMsgIds.has(msg.msgId)) transport.broadcastMessage(msg)
          }
        }
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
      setShowHelp(true)
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
      const circleId = (await sha256HexFromRawKey(parsed.secretHex)).slice(0, 24)
      joinCircle(next, { circleId, secretHex: parsed.secretHex, name: '', isOwned: false })
      setStatus(`Joined circle ${circleId.slice(0, 8)}. Sync will start shortly.`)
      return
    }

    if (cmd === '/call') {
      const sub = parts[1]?.toLowerCase()
      if (!sub || sub === 'start') {
        const msg = await createCall(next)
        if (!msg) throw new Error('No active circle/channel')
        setStatus(`Call started. Others can /call join to join.`)
        return
      }
      if (sub === 'join') {
        const circleId = next.currentCircleId
        if (!circleId) throw new Error('No active circle')
        // Find the first active call in the current channel.
        const channelId = next.currentChannelId ?? 'general'
        const call = Object.values(next.activeCalls).find(
          (c) => c.circleId === circleId && c.channelId === channelId,
        )
        if (!call) throw new Error('No active call in this channel')
        const msg = await joinCall(next, call.sessionId)
        if (!msg) throw new Error('Failed to join call')
        setStatus(`Joined call (${call.participants.length} participants)`)
        return
      }
      if (sub === 'leave') {
        const circleId = next.currentCircleId
        if (!circleId) throw new Error('No active circle')
        const channelId = next.currentChannelId ?? 'general'
        const call = Object.values(next.activeCalls).find(
          (c) =>
            c.circleId === circleId &&
            c.channelId === channelId &&
            c.participants.includes(next.node.nodeId),
        )
        if (!call) throw new Error('Not in a call in this channel')
        await leaveCall(next, call.sessionId)
        setStatus('Left the call.')
        return
      }
      if (sub === 'end') {
        const circleId = next.currentCircleId
        if (!circleId) throw new Error('No active circle')
        const channelId = next.currentChannelId ?? 'general'
        const call = Object.values(next.activeCalls).find(
          (c) =>
            c.circleId === circleId &&
            c.channelId === channelId &&
            c.hostNodeId === next.node.nodeId,
        )
        if (!call) throw new Error('No call to end (you are not the host)')
        await endCall(next, call.sessionId)
        setStatus('Call ended.')
        return
      }
      throw new Error('Usage: /call start|join|leave|end')
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
        turnUrl: turnUrl.trim(),
        turnUsername: turnUsername.trim(),
        turnCredential: turnCredential.trim(),
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

  // Active call in the current channel (if any).
  const activeCall = currentCircleId
    ? Object.values(state.activeCalls).find(
        (c) => c.circleId === currentCircleId && c.channelId === currentChannelId,
      ) ?? null
    : null
  const amInCall = activeCall?.participants.includes(state.node.nodeId) ?? false
  const amHost = activeCall?.hostNodeId === state.node.nodeId

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
                  data-testid="setup-display-name"
                />
              </label>
              <div className="tui-tab-row">
                <button
                  type="button"
                  className={`tui-tab ${mode === 'host' ? 'active' : ''}`}
                  onClick={() => setMode('host')}
                  data-testid="setup-tab-host"
                >
                  Host
                </button>
                <button
                  type="button"
                  className={`tui-tab ${mode === 'join' ? 'active' : ''}`}
                  onClick={() => setMode('join')}
                  data-testid="setup-tab-join"
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
                    data-testid="setup-circle-name"
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
                    data-testid="setup-invite-code"
                  />
                </label>
              )}
              <label>
                Rendezvous server
                <input
                  value={rendezvousInput}
                  onChange={(e) => setRendezvousInput(e.target.value)}
                  placeholder="https://your-relay-server/api"
                  data-testid="setup-rendezvous"
                />
              </label>
              {status && <p className="tui-error">{status}</p>}
            </div>
            <div className="tui-modal-actions">
              <button type="submit" className="tui-btn primary" data-testid="setup-submit">
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
            {activeCall && (
              <>
                <span className="tui-header-sep"> │ </span>
                <span className="tui-sync-status">
                  {amInCall ? '◈' : '◇'} call({activeCall.participants.length})
                </span>
              </>
            )}
            {rendezvousBase && (
              <>
                <span className="tui-header-sep"> │ </span>
                <span className={p2pCount > 0 || wsLive ? 'tui-sync-status' : 'tui-dim'}>
                  {p2pCount > 0 ? `◦ p2p(${p2pCount})` : wsLive ? '◦ live' : '○ poll'}
                </span>
              </>
            )}
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
          {/* Call panel — status only; F4 opens the call modal for actions */}
          {currentCircleId && (
            <div className="tui-call-panel">
              <div
                className="tui-sidebar-section tui-sidebar-section-clickable"
                onClick={() => setShowCall(true)}
              >
                {activeCall ? (amInCall ? '◈ Call' : '◇ Call (pending)') : '◇ Call'}
              </div>
              {activeCall &&
                activeCall.participants.map((nodeId) => {
                  const peerState =
                    nodeId !== state.node.nodeId ? callPeerStates[nodeId] : undefined
                  return (
                    <div key={nodeId} className="tui-call-participant">
                      {nodeId === activeCall.hostNodeId ? '★' : '·'}{' '}
                      {nodeId.slice(0, 8)}
                      {nodeId === state.node.nodeId ? ' (you)' : ''}
                      {peerState && (
                        <span className={`tui-peer-state ${peerState}`}>
                          {peerState === 'connected' ? ' ○' : peerState === 'connecting' ? ' ◌' : ' ✕'}
                        </span>
                      )}
                    </div>
                  )
                })}
            </div>
          )}
        </aside>

        {/* Message log */}
        <div className="tui-main">
          <div className="tui-messages">
            {messages.length === 0 && (
              <div className="tui-empty">No messages yet — type below to start chatting.</div>
            )}
            {messages.map((msg) => {
              const isSelf = msg.authorNodeId === state.node.nodeId
              return (
                <div key={msg.msgId} className="tui-message">
                  <span className="tui-ts">[{formatTime(msg.createdTs)}]</span>{' '}
                  <span
                    className={`tui-author${isSelf ? ' is-self' : ''}`}
                    style={isSelf ? undefined : { color: peerColor(msg.authorNodeId) }}
                  >
                    {msg.displayName}
                  </span>
                  <span className="tui-colon">:</span>{' '}
                  <span className="tui-text">{msg.text}</span>
                </div>
              )
            })}
            <div ref={messagesEndRef} />
          </div>
        </div>
      </div>

      {/* Hidden audio elements for remote call streams */}
      {amInCall &&
        Object.entries(remoteStreams).map(([peerId, stream]) => (
          <audio
            key={peerId}
            autoPlay
            data-testid="call-remote-audio"
            ref={(el) => {
              if (el) el.srcObject = stream
            }}
          />
        ))}

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
            ref={inputRef}
            value={composer}
            onChange={(e) => setComposer(e.target.value)}
            placeholder="Type a message or /command…"
            autoFocus
          />
        </form>
      </div>

      {/* Footer — tappable on mobile (iOS tab bar style) */}
      <div className="tui-footer">
        <button className={`tui-footer-btn${showHelp ? ' is-active' : ''}`} onClick={() => setShowHelp((v) => !v)}>
          <kbd>F1</kbd><span>Help</span>
        </button>
        <button className={`tui-footer-btn${showInvite ? ' is-active' : ''}`} onClick={() => setShowInvite((v) => !v)} data-testid="footer-invite">
          <kbd>F2</kbd><span>Invite</span>
        </button>
        <button className={`tui-footer-btn${showSettings ? ' is-active' : ''}`} onClick={() => setShowSettings((v) => !v)}>
          <kbd>F3</kbd><span>Settings</span>
        </button>
        <button className={`tui-footer-btn${amInCall || showCall ? ' is-active' : activeCall && !amInCall ? ' has-call' : ''}`} onClick={() => setShowCall((v) => !v)} data-testid="footer-call">
          <kbd>F4</kbd><span>Call</span>
        </button>
        <button className="tui-footer-btn" onClick={() => inputRef.current?.focus()}>
          <kbd>Esc</kbd><span>Focus</span>
        </button>
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
              <label>
                TURN server <span className="tui-dim">(optional, for calls behind strict NAT)</span>
                <input
                  value={turnUrl}
                  onChange={(e) => setTurnUrl(e.target.value)}
                  placeholder="turn:your-turn-server:3478"
                />
              </label>
              <label>
                TURN username
                <input
                  value={turnUsername}
                  onChange={(e) => setTurnUsername(e.target.value)}
                />
              </label>
              <label>
                TURN credential
                <input
                  type="password"
                  value={turnCredential}
                  onChange={(e) => setTurnCredential(e.target.value)}
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

      {/* Help Modal */}
      {showHelp && (
        <div className="tui-modal-overlay" onClick={() => setShowHelp(false)}>
          <div className="tui-modal" onClick={(e) => e.stopPropagation()}>
            <div className="tui-modal-header">felundchat — commands</div>
            <div className="tui-modal-body">
              <div className="tui-help-content">
                <div className="tui-help-section">General</div>
                <div className="tui-help-row">
                  <span className="tui-help-cmd">/help</span>
                  <span className="tui-help-desc">Show this screen</span>
                </div>
                <div className="tui-help-row">
                  <span className="tui-help-cmd">/name &lt;name&gt;</span>
                  <span className="tui-help-desc">Change your display name</span>
                </div>
                <div className="tui-help-row">
                  <span className="tui-help-cmd">/invite</span>
                  <span className="tui-help-desc">Show invite code for the active circle</span>
                </div>
                <div className="tui-help-row">
                  <span className="tui-help-cmd">/settings</span>
                  <span className="tui-help-desc">Open settings (display name, relay URL)</span>
                </div>
                <div className="tui-help-row">
                  <span className="tui-help-cmd">/join &lt;code&gt;</span>
                  <span className="tui-help-desc">Join a circle using an invite code</span>
                </div>
                <div className="tui-help-section">Channels</div>
                <div className="tui-help-row">
                  <span className="tui-help-cmd">/channels</span>
                  <span className="tui-help-desc">List channels in the active circle</span>
                </div>
                <div className="tui-help-row">
                  <span className="tui-help-cmd">/channel create &lt;name&gt;</span>
                  <span className="tui-help-desc">Create a new channel</span>
                </div>
                <div className="tui-help-row">
                  <span className="tui-help-cmd">/channel switch &lt;name&gt;</span>
                  <span className="tui-help-desc">Switch to another channel</span>
                </div>
                <div className="tui-help-section">Calls</div>
                <div className="tui-help-row">
                  <span className="tui-help-cmd">/call start</span>
                  <span className="tui-help-desc">Start a call in the current channel</span>
                </div>
                <div className="tui-help-row">
                  <span className="tui-help-cmd">/call join</span>
                  <span className="tui-help-desc">Join an active call</span>
                </div>
                <div className="tui-help-row">
                  <span className="tui-help-cmd">/call leave</span>
                  <span className="tui-help-desc">Leave the current call</span>
                </div>
                <div className="tui-help-row">
                  <span className="tui-help-cmd">/call end</span>
                  <span className="tui-help-desc">End the call (host only)</span>
                </div>
                <div className="tui-help-section">Keyboard shortcuts</div>
                <div className="tui-help-row">
                  <span className="tui-help-cmd"><kbd>F1</kbd></span>
                  <span className="tui-help-desc">Help</span>
                </div>
                <div className="tui-help-row">
                  <span className="tui-help-cmd"><kbd>F2</kbd></span>
                  <span className="tui-help-desc">Invite code</span>
                </div>
                <div className="tui-help-row">
                  <span className="tui-help-cmd"><kbd>F3</kbd></span>
                  <span className="tui-help-desc">Settings</span>
                </div>
                <div className="tui-help-row">
                  <span className="tui-help-cmd"><kbd>Escape</kbd></span>
                  <span className="tui-help-desc">Close modals · focus input</span>
                </div>
              </div>
            </div>
            <div className="tui-modal-actions">
              <button className="tui-btn primary" onClick={() => setShowHelp(false)}>
                Close
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
              <pre className="tui-invite-code" data-testid="invite-code">{inviteCode}</pre>
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

      {/* Call Modal — F4 */}
      {showCall && currentCircleId && (
        <div className="tui-modal-overlay" onClick={() => setShowCall(false)}>
          <div className="tui-modal tui-call-modal" onClick={(e) => e.stopPropagation()} data-testid="call-modal">
            <div className="tui-modal-header">
              {amInCall ? '◈ Call' : '◇ Call'}
              {activeCall && (
                <span style={{ fontWeight: 'normal', fontSize: '0.78rem', marginLeft: '0.6rem', opacity: 0.75 }}>
                  {activeCall.participants.length} participant{activeCall.participants.length !== 1 ? 's' : ''}
                </span>
              )}
            </div>
            <div className="tui-modal-body">
              {amInCall ? (
                <>
                  {/* Video grid — shown when camera is on */}
                  {isVideoOn && (
                    <div className="tui-call-video-grid">
                      {callManagerRef.current?.localStream && (
                        <video
                          autoPlay
                          playsInline
                          muted
                          data-testid="call-local-video"
                          ref={(el) => {
                            if (el && callManagerRef.current?.localStream)
                              el.srcObject = callManagerRef.current.localStream
                          }}
                          className="tui-call-video tui-call-video-local"
                        />
                      )}
                      {Object.entries(remoteStreams).map(([peerId, stream]) => (
                        <video
                          key={peerId}
                          autoPlay
                          playsInline
                          data-testid="call-remote-video"
                          ref={(el) => {
                            if (el) el.srcObject = stream
                          }}
                          className="tui-call-video"
                        />
                      ))}
                    </div>
                  )}
                  {/* Participant list */}
                  {activeCall && (
                    <div className="tui-call-modal-participants">
                      {activeCall.participants.map((nodeId) => {
                        const peerState =
                          nodeId !== state.node.nodeId ? callPeerStates[nodeId] : undefined
                        return (
                          <div key={nodeId} className="tui-call-participant">
                            {nodeId === activeCall.hostNodeId ? '★' : '·'}{' '}
                            {nodeId.slice(0, 8)}
                            {nodeId === state.node.nodeId ? ' (you)' : ''}
                            {peerState && (
                              <span className={`tui-peer-state ${peerState}`}>
                                {peerState === 'connected'
                                  ? ' ○'
                                  : peerState === 'connecting'
                                    ? ' ◌'
                                    : ' ✕'}
                              </span>
                            )}
                          </div>
                        )
                      })}
                    </div>
                  )}
                </>
              ) : (
                <p className="tui-dim" style={{ margin: 0 }}>
                  {activeCall
                    ? 'A call is active in this channel. Join to connect your audio/video.'
                    : 'No active call in this channel.'}
                </p>
              )}
            </div>
            <div className="tui-modal-actions">
              {amInCall ? (
                <>
                  <button
                    className={`tui-btn ${isMuted ? '' : 'primary'}`}
                    onClick={() => {
                      const m = !isMuted
                      setIsMuted(m)
                      callManagerRef.current?.muteAudio(m)
                    }}
                    data-testid="call-mute"
                  >
                    {isMuted ? '⊗ Muted' : '◎ Mic'}
                  </button>
                  <button
                    className={`tui-btn ${isVideoOn ? 'primary' : ''}`}
                    onClick={() => {
                      const v = !isVideoOn
                      setIsVideoOn(v)
                      void callManagerRef.current?.enableVideo(v)
                    }}
                    data-testid="call-cam"
                  >
                    {isVideoOn ? '⊡ Cam' : '⊞ Cam'}
                  </button>
                  <button
                    className="tui-btn"
                    onClick={() =>
                      void (async () => {
                        const next = { ...state, activeCalls: { ...state.activeCalls } }
                        await leaveCall(next, activeCall!.sessionId)
                        await persist(next)
                        setShowCall(false)
                      })()
                    }
                    data-testid="call-leave"
                  >
                    Leave
                  </button>
                  {amHost && (
                    <button
                      className="tui-btn"
                      onClick={() =>
                        void (async () => {
                          const next = { ...state, activeCalls: { ...state.activeCalls } }
                          await endCall(next, activeCall!.sessionId)
                          await persist(next)
                          setShowCall(false)
                        })()
                      }
                      data-testid="call-end"
                    >
                      End
                    </button>
                  )}
                  <button className="tui-btn" onClick={() => setShowCall(false)} data-testid="call-close">
                    Close
                  </button>
                </>
              ) : (
                <>
                  {!activeCall ? (
                    <button
                      className="tui-btn primary"
                      onClick={() =>
                        void (async () => {
                          const next = { ...state, activeCalls: { ...state.activeCalls } }
                          await createCall(next)
                          await persist(next)
                        })()
                      }
                      data-testid="call-start"
                    >
                      Start call
                    </button>
                  ) : (
                    <button
                      className="tui-btn primary"
                      onClick={() =>
                        void (async () => {
                          const next = { ...state, activeCalls: { ...state.activeCalls } }
                          await joinCall(next, activeCall.sessionId)
                          await persist(next)
                        })()
                      }
                      data-testid="call-join"
                    >
                      Join call
                    </button>
                  )}
                  <button className="tui-btn" onClick={() => setShowCall(false)} data-testid="call-cancel">
                    Cancel
                  </button>
                </>
              )}
            </div>
          </div>
        </div>
      )}
    </div>
  )
}

export default App
