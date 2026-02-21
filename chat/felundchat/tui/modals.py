"""Modal dialogs for the felundchat TUI."""
from __future__ import annotations

import asyncio

from textual.app import ComposeResult
from textual.binding import Binding
from textual.containers import Vertical
from textual.screen import ModalScreen
from textual.widgets import Button, Input, Label

from ._utils import _try_copy_to_clipboard


class InviteModal(ModalScreen):
    """Pop-up showing an invite code in a selectable input field."""

    BINDINGS = [Binding("escape", "dismiss", "Close")]

    DEFAULT_CSS = """
    InviteModal {
        align: center middle;
    }
    #invite-box {
        width: 72;
        height: auto;
        border: solid $primary;
        padding: 1 2;
        background: $surface;
    }
    #invite-modal-title {
        text-style: bold;
        margin-bottom: 1;
    }
    #invite-code-input {
        width: 100%;
        margin-bottom: 1;
    }
    #invite-hint {
        color: $text-muted;
        margin-bottom: 1;
    }
    #invite-status {
        height: 1;
        margin-bottom: 1;
    }
    #btn-invite-close {
        width: 100%;
    }
    """

    def __init__(self, code: str, **kwargs) -> None:
        super().__init__(**kwargs)
        self._code = code

    def compose(self) -> ComposeResult:
        with Vertical(id="invite-box"):
            yield Label("Invite Code", id="invite-modal-title")
            yield Input(value=self._code, id="invite-code-input")
            yield Label("Ctrl+A  →  Ctrl+C to copy  |  Esc to close", id="invite-hint")
            yield Label("", id="invite-status")
            yield Button("Close", id="btn-invite-close", variant="primary")

    async def on_mount(self) -> None:
        self.query_one("#invite-code-input", Input).focus()
        copied = await asyncio.to_thread(_try_copy_to_clipboard, self._code)
        status = self.query_one("#invite-status", Label)
        if copied:
            status.update("[green]Copied to clipboard automatically[/green]")
        else:
            status.update("[dim]Auto-copy unavailable — use Ctrl+A, Ctrl+C above[/dim]")

    def on_button_pressed(self, event: Button.Pressed) -> None:
        if event.button.id == "btn-invite-close":
            self.dismiss()
