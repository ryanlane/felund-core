interface SettingsModalProps {
  show: boolean
  onClose: () => void
  displayName: string
  setDisplayName: (v: string) => void
  rendezvousInput: string
  setRendezvousInput: (v: string) => void
  turnUrl: string
  setTurnUrl: (v: string) => void
  turnUsername: string
  setTurnUsername: (v: string) => void
  turnCredential: string
  setTurnCredential: (v: string) => void
  nodeId: string
  onSave: () => void
  onTestHealth: () => void
}

export function SettingsModal({
  show,
  onClose,
  displayName,
  setDisplayName,
  rendezvousInput,
  setRendezvousInput,
  turnUrl,
  setTurnUrl,
  turnUsername,
  setTurnUsername,
  turnCredential,
  setTurnCredential,
  nodeId,
  onSave,
  onTestHealth,
}: SettingsModalProps) {
  if (!show) return null

  return (
    <div className="tui-modal-overlay" onClick={onClose}>
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
            node: {nodeId}
          </p>
        </div>
        <div className="tui-modal-actions">
          <button className="tui-btn" onClick={onTestHealth}>
            Test relay
          </button>
          <button className="tui-btn" onClick={onClose}>
            Cancel
          </button>
          <button className="tui-btn primary" onClick={onSave}>
            Save
          </button>
        </div>
      </div>
    </div>
  )
}
