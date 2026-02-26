interface InviteModalProps {
  show: boolean
  onClose: () => void
  inviteCode: string
  circleName: string
  inviteCopied: boolean
  onCopy: (code: string) => void
}

export function InviteModal({ show, onClose, inviteCode, circleName, inviteCopied, onCopy }: InviteModalProps) {
  if (!show || !inviteCode) return null

  return (
    <div className="tui-modal-overlay" onClick={onClose}>
      <div className="tui-modal" onClick={(e) => e.stopPropagation()}>
        <div className="tui-modal-header">
          Invite Code — {circleName}
        </div>
        <div className="tui-modal-body">
          <p className="tui-dim" style={{ margin: 0, fontSize: '0.78rem' }}>
            Share this code with others to join{' '}
            <strong>{circleName || 'this circle'}</strong>.
          </p>
          <pre className="tui-invite-code" data-testid="invite-code">{inviteCode}</pre>
        </div>
        <div className="tui-modal-actions">
          <button className="tui-btn" onClick={onClose}>
            Close
          </button>
          <button className="tui-btn primary" onClick={() => onCopy(inviteCode)}>
            {inviteCopied ? '✓ Copied!' : 'Copy to clipboard'}
          </button>
        </div>
      </div>
    </div>
  )
}
