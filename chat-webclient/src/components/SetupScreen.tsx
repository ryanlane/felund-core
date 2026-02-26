import type { FormEvent } from 'react'

interface SetupScreenProps {
  displayName: string
  setDisplayName: (v: string) => void
  mode: 'host' | 'join'
  setMode: (v: 'host' | 'join') => void
  circleName: string
  setCircleName: (v: string) => void
  inviteInput: string
  setInviteInput: (v: string) => void
  rendezvousInput: string
  setRendezvousInput: (v: string) => void
  status: string
  onSubmit: (e: FormEvent) => void
}

export function SetupScreen({
  displayName,
  setDisplayName,
  mode,
  setMode,
  circleName,
  setCircleName,
  inviteInput,
  setInviteInput,
  rendezvousInput,
  setRendezvousInput,
  status,
  onSubmit,
}: SetupScreenProps) {
  return (
    <div className="tui-app">
      <div className="tui-header">
        <span className="tui-title">felundchat</span>
        <span className="tui-header-dim"> — setup</span>
      </div>
      <div className="tui-setup-overlay">
        <form className="tui-setup-form" onSubmit={onSubmit}>
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
