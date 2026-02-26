interface HelpModalProps {
  show: boolean
  onClose: () => void
}

export function HelpModal({ show, onClose }: HelpModalProps) {
  if (!show) return null

  return (
    <div className="tui-modal-overlay" onClick={onClose}>
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
          <button className="tui-btn primary" onClick={onClose}>
            Close
          </button>
        </div>
      </div>
    </div>
  )
}
