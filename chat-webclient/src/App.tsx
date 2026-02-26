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
import { formatTime, peerColor } from './utils/peerColor'
import { SetupScreen } from './components/SetupScreen'
import { SettingsModal } from './components/SettingsModal'
import { HelpModal } from './components/HelpModal'
import { InviteModal } from './components/InviteModal'
import { CallModal } from './components/CallModal'

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
  const [callLocalStream, setCallLocalStream] = useState<MediaStream | null>(null)
  const callManagerRef = useRef<WebRTCCallManager | null>(null)
  const remoteAudioRefs = useRef<Map<string, HTMLAudioElement>>(new Map())

  const [audioInputs, setAudioInputs] = useState<MediaDeviceInfo[]>([])
  const [audioOutputs, setAudioOutputs] = useState<MediaDeviceInfo[]>([])
  const [selectedInputId, setSelectedInputId] = useState('')
  const [selectedOutputId, setSelectedOutputId] = useState('')

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

  const applySinkId = async (el: HTMLAudioElement, deviceId: string) => {
    const sink = (el as HTMLAudioElement & { setSinkId?: (id: string) => Promise<void> }).setSinkId
    if (!sink || !deviceId) return
    try {
      await sink.call(el, deviceId)
    } catch {
      /* best-effort */
    }
  }

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

  useEffect(() => {
    if (!navigator.mediaDevices?.enumerateDevices) return

    const refresh = async () => {
      try {
        const devices = await navigator.mediaDevices.enumerateDevices()
        const inputs = devices.filter((d) => d.kind === 'audioinput')
        const outputs = devices.filter((d) => d.kind === 'audiooutput')
        setAudioInputs(inputs)
        setAudioOutputs(outputs)
        if (selectedInputId && !inputs.some((d) => d.deviceId === selectedInputId)) {
          setSelectedInputId('')
        }
        if (selectedOutputId && !outputs.some((d) => d.deviceId === selectedOutputId)) {
          setSelectedOutputId('')
        }
      } catch {
        /* best-effort */
      }
    }

    void refresh()
    const handler = () => void refresh()
    navigator.mediaDevices.addEventListener('devicechange', handler)
    return () => navigator.mediaDevices.removeEventListener('devicechange', handler)
  }, [selectedInputId, selectedOutputId])

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
      setCallLocalStream(null)
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
        if (callManagerRef.current === mgr) setCallLocalStream(mgr.localStream)
      })()
    }

    // Connect to any participant we haven't yet connected to
    const mgr = callManagerRef.current
    const otherParticipants = call.participants.filter((id) => id !== s.node.nodeId)
    for (const peerId of otherParticipants) {
      void mgr.connectToPeer(peerId)
    }
  }, [state]) // eslint-disable-line react-hooks/exhaustive-deps

  useEffect(() => {
    if (!selectedOutputId) return
    for (const el of remoteAudioRefs.current.values()) {
      void applySinkId(el, selectedOutputId)
    }
  }, [selectedOutputId])

  // ── Pause Phase 3 signaling while in a call ─────────────────────────────

  useEffect(() => {
    const s = stateRef.current
    if (!s) return
    const circleId = s.currentCircleId
    const channelId = s.currentChannelId ?? 'general'
    const activeCall = circleId
      ? Object.values(s.activeCalls).find(
          (c) => c.circleId === circleId && c.channelId === channelId,
        ) ?? null
      : null
    const amInCall = activeCall?.participants.includes(s.node.nodeId) ?? false
    for (const transport of webrtcRef.current.values()) {
      transport.setSignalingEnabled(!amInCall)
    }
  }, [state])

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
      <SetupScreen
        displayName={displayName}
        setDisplayName={setDisplayName}
        mode={mode}
        setMode={setMode}
        circleName={circleName}
        setCircleName={setCircleName}
        inviteInput={inviteInput}
        setInviteInput={setInviteInput}
        rendezvousInput={rendezvousInput}
        setRendezvousInput={setRendezvousInput}
        status={status}
        onSubmit={(e) => void handleSetup(e)}
      />
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
              if (!el) {
                remoteAudioRefs.current.delete(peerId)
                return
              }
              el.srcObject = stream
              remoteAudioRefs.current.set(peerId, el)
              if (selectedOutputId) void applySinkId(el, selectedOutputId)
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
        <form className="tui-input-form" onSubmit={(e) => void handleSend(e)}>
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

      <SettingsModal
        show={showSettings}
        onClose={() => setShowSettings(false)}
        displayName={displayName}
        setDisplayName={setDisplayName}
        rendezvousInput={rendezvousInput}
        setRendezvousInput={setRendezvousInput}
        turnUrl={turnUrl}
        setTurnUrl={setTurnUrl}
        turnUsername={turnUsername}
        setTurnUsername={setTurnUsername}
        turnCredential={turnCredential}
        setTurnCredential={setTurnCredential}
        nodeId={state.node.nodeId}
        onSave={() => void saveSettings()}
        onTestHealth={() => void testRendezvousHealth()}
      />

      <HelpModal show={showHelp} onClose={() => setShowHelp(false)} />

      <InviteModal
        show={showInvite}
        onClose={() => setShowInvite(false)}
        inviteCode={inviteCode}
        circleName={currentCircle?.name || currentCircleId?.slice(0, 8) || ''}
        inviteCopied={inviteCopied}
        onCopy={(code) => void copyInviteCode(code)}
      />

      <CallModal
        show={showCall}
        onClose={() => setShowCall(false)}
        currentCircleId={currentCircleId}
        amInCall={amInCall}
        amHost={amHost}
        activeCall={activeCall}
        callManagerRef={callManagerRef}
        remoteStreams={remoteStreams}
        callPeerStates={callPeerStates}
        callLocalStream={callLocalStream}
        setCallLocalStream={setCallLocalStream}
        isMuted={isMuted}
        setIsMuted={setIsMuted}
        isVideoOn={isVideoOn}
        setIsVideoOn={setIsVideoOn}
        audioInputs={audioInputs}
        audioOutputs={audioOutputs}
        selectedInputId={selectedInputId}
        setSelectedInputId={setSelectedInputId}
        selectedOutputId={selectedOutputId}
        setSelectedOutputId={setSelectedOutputId}
        setStatus={setStatus}
        nodeId={state.node.nodeId}
        state={state}
        persist={persist}
      />
    </div>
  )
}

export default App
