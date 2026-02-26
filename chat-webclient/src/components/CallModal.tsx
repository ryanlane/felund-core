import { useState } from 'react'
import type { CallSession, State } from '../core/models'
import { createCall, endCall, joinCall, leaveCall } from '../core/state'
import type { CallPeerState } from '../network/call'
import type { WebRTCCallManager } from '../network/call'
import { VUMeter } from './VUMeter'

interface CallModalProps {
  show: boolean
  onClose: () => void
  currentCircleId: string | undefined
  amInCall: boolean
  amHost: boolean
  activeCall: CallSession | null
  callManagerRef: { current: WebRTCCallManager | null }
  remoteStreams: Record<string, MediaStream>
  callPeerStates: Record<string, CallPeerState>
  callLocalStream: MediaStream | null
  setCallLocalStream: (s: MediaStream | null) => void
  isMuted: boolean
  setIsMuted: (v: boolean) => void
  isVideoOn: boolean
  setIsVideoOn: (v: boolean) => void
  audioInputs: MediaDeviceInfo[]
  audioOutputs: MediaDeviceInfo[]
  selectedInputId: string
  setSelectedInputId: (v: string) => void
  selectedOutputId: string
  setSelectedOutputId: (v: string) => void
  setStatus: (v: string) => void
  nodeId: string
  state: State
  persist: (next: State) => Promise<void>
}

export function CallModal({
  show,
  onClose,
  currentCircleId,
  amInCall,
  amHost,
  activeCall,
  callManagerRef,
  remoteStreams,
  callPeerStates,
  callLocalStream,
  setCallLocalStream,
  isMuted,
  setIsMuted,
  isVideoOn,
  setIsVideoOn,
  audioInputs,
  audioOutputs,
  selectedInputId,
  setSelectedInputId,
  selectedOutputId,
  setSelectedOutputId,
  setStatus,
  nodeId,
  state,
  persist,
}: CallModalProps) {
  const [showDevices, setShowDevices] = useState(false)

  if (!show || !currentCircleId) return null

  const localAudioTracks = callManagerRef.current?.localStream?.getAudioTracks() ?? []
  const localVideoTracks = callManagerRef.current?.localStream?.getVideoTracks() ?? []
  const localAudioEnabled = localAudioTracks.filter((t) => t.enabled).length
  const localVideoEnabled = localVideoTracks.filter((t) => t.enabled).length
  const remoteCount = Object.keys(remoteStreams).length
  const remoteAudio = Object.values(remoteStreams).reduce((s, r) => s + r.getAudioTracks().length, 0)
  const remoteVideo = Object.values(remoteStreams).reduce((s, r) => s + r.getVideoTracks().length, 0)

  return (
    <div className="tui-modal-overlay" onClick={onClose}>
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
                  {activeCall.participants.map((participantId) => {
                    const peerState =
                      participantId !== nodeId ? callPeerStates[participantId] : undefined
                    return (
                      <div key={participantId} className="tui-call-participant">
                        {participantId === activeCall.hostNodeId ? '★' : '·'}{' '}
                        {participantId.slice(0, 8)}
                        {participantId === nodeId ? ' (you)' : ''}
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
              {/* VU meters */}
              <div className="tui-vu-meters">
                <div className="tui-vu-row">
                  <span className="tui-vu-label">you</span>
                  <VUMeter stream={callLocalStream} />
                </div>
                {Object.entries(remoteStreams).map(([peerId, stream]) => (
                  <div key={peerId} className="tui-vu-row">
                    <span className="tui-vu-label">{peerId.slice(0, 6)}</span>
                    <VUMeter stream={stream} />
                  </div>
                ))}
              </div>
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
                    onClose()
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
                      onClose()
                    })()
                  }
                  data-testid="call-end"
                >
                  End
                </button>
              )}
              <button
                className={`tui-btn ${showDevices ? 'primary' : ''}`}
                onClick={() => setShowDevices((v) => !v)}
                data-testid="call-devices"
              >
                Devices
              </button>
              <button className="tui-btn" onClick={onClose} data-testid="call-close">
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
              <button className="tui-btn" onClick={onClose} data-testid="call-cancel">
                Cancel
              </button>
            </>
          )}
        </div>
        {amInCall && showDevices && (
          <div className="tui-call-devices">
            <label>
              Microphone
              <select
                className="tui-call-select"
                value={selectedInputId}
                onChange={(e) => {
                  const nextId = e.target.value
                  setSelectedInputId(nextId)
                  void (async () => {
                    const mgr = callManagerRef.current
                    if (!mgr) return
                    const ok = await mgr.setAudioInput(nextId || null)
                    if (!ok) {
                      setStatus('Microphone switch failed — check permissions or device.')
                    } else if (callManagerRef.current === mgr) {
                      setCallLocalStream(mgr.localStream)
                    }
                  })()
                }}
              >
                <option value="">Default</option>
                {audioInputs.map((d) => (
                  <option key={d.deviceId} value={d.deviceId}>
                    {d.label || `Microphone ${d.deviceId.slice(0, 6)}`}
                  </option>
                ))}
              </select>
            </label>
            <label>
              Speaker
              <select
                className="tui-call-select"
                value={selectedOutputId}
                onChange={(e) => setSelectedOutputId(e.target.value)}
              >
                <option value="">Default</option>
                {audioOutputs.map((d) => (
                  <option key={d.deviceId} value={d.deviceId}>
                    {d.label || `Speaker ${d.deviceId.slice(0, 6)}`}
                  </option>
                ))}
              </select>
            </label>
          </div>
        )}
        {amInCall && (
          <div className="tui-call-status-bar">
            <span className="tui-call-stat-group">
              <span className="tui-call-stat-label">mic</span>
              <span className={localAudioEnabled > 0 ? 'tui-call-stat-val active' : 'tui-call-stat-val'}>
                {localAudioEnabled}/{localAudioTracks.length}
              </span>
            </span>
            <span className="tui-call-stat-sep">·</span>
            <span className="tui-call-stat-group">
              <span className="tui-call-stat-label">cam</span>
              <span className={localVideoEnabled > 0 ? 'tui-call-stat-val active' : 'tui-call-stat-val'}>
                {localVideoEnabled}/{localVideoTracks.length}
              </span>
            </span>
            <span className="tui-call-stat-sep">·</span>
            <span className="tui-call-stat-group">
              <span className="tui-call-stat-label">peers</span>
              <span className={remoteCount > 0 ? 'tui-call-stat-val active' : 'tui-call-stat-val'}>
                {remoteCount}
              </span>
            </span>
            <span className="tui-call-stat-sep">·</span>
            <span className="tui-call-stat-group">
              <span className="tui-call-stat-label">a</span>
              <span className="tui-call-stat-val">{remoteAudio}</span>
              <span className="tui-call-stat-sep"> /</span>
              <span className="tui-call-stat-label"> v</span>
              <span className="tui-call-stat-val">{remoteVideo}</span>
            </span>
          </div>
        )}
      </div>
    </div>
  )
}
