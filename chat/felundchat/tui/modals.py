"""Modal dialogs for the felundchat TUI."""
from __future__ import annotations

import asyncio

from textual.app import ComposeResult
from textual.binding import Binding
from textual.containers import Vertical
from textual.screen import ModalScreen
from textual.widgets import Button, Input, Label, RichLog

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
    #invite-buttons {
        layout: horizontal;
        height: auto;
        margin-top: 1;
    }
    #btn-invite-copy {
        width: 1fr;
        margin-right: 1;
    }
    #btn-invite-close {
        width: 1fr;
    }
    """

    def __init__(self, code: str, **kwargs) -> None:
        super().__init__(**kwargs)
        self._code = code

    def compose(self) -> ComposeResult:
        from textual.containers import Horizontal
        with Vertical(id="invite-box"):
            yield Label("Invite Code", id="invite-modal-title")
            yield Input(value=self._code, id="invite-code-input")
            yield Label("Esc to close", id="invite-hint")
            yield Label("", id="invite-status")
            with Horizontal(id="invite-buttons"):
                yield Button("Copy to Clipboard", id="btn-invite-copy", variant="success")
                yield Button("Close", id="btn-invite-close", variant="primary")

    async def on_mount(self) -> None:
        self.query_one("#invite-code-input", Input).focus()
        await self._do_copy()

    async def _do_copy(self) -> None:
        status = self.query_one("#invite-status", Label)
        # Primary: OSC 52 — built into Textual, works in GNOME Terminal,
        # Windows Terminal, kitty, iTerm2, and most modern terminal emulators
        # without any external tools.
        try:
            self.app.copy_to_clipboard(self._code)
            status.update("[green]Copied to clipboard[/green]")
            return
        except Exception:
            pass
        # Fallback: try system clipboard tools (xclip, wl-copy, clip.exe, …)
        copied = await asyncio.to_thread(_try_copy_to_clipboard, self._code)
        if copied:
            status.update("[green]Copied to clipboard[/green]")
        else:
            status.update("[dim]Auto-copy unavailable — use Ctrl+A, Ctrl+C above[/dim]")

    async def on_button_pressed(self, event: Button.Pressed) -> None:
        if event.button.id == "btn-invite-copy":
            await self._do_copy()
        elif event.button.id == "btn-invite-close":
            self.dismiss()


class HelpModal(ModalScreen):
    """Scrollable overlay displaying slash-command reference."""

    BINDINGS = [Binding("escape", "dismiss", "Close")]

    DEFAULT_CSS = """
    HelpModal {
        align: center middle;
    }
    #help-box {
        width: 88;
        height: 80vh;
        max-height: 38;
        border: solid $primary;
        background: $surface;
        padding: 1 0 0 0;
    }
    #help-title {
        text-style: bold;
        padding: 0 2;
        height: 2;
        color: $text;
        background: $primary-darken-2;
    }
    #help-log {
        height: 1fr;
        padding: 0 1;
    }
    #btn-help-close {
        width: 100%;
        height: 3;
        margin-top: 0;
    }
    """

    def __init__(self, lines: list, title: str = "felundchat — commands", **kwargs) -> None:
        super().__init__(**kwargs)
        self._lines = lines
        self._title = title

    def compose(self) -> ComposeResult:
        with Vertical(id="help-box"):
            yield Label(self._title, id="help-title")
            yield RichLog(id="help-log", markup=True, highlight=False, auto_scroll=False)
            yield Button("Close  [dim](Esc)[/dim]", id="btn-help-close", variant="primary")

    async def on_mount(self) -> None:
        log = self.query_one("#help-log", RichLog)
        for line in self._lines:
            log.write(line)
        log.scroll_home(animate=False)
        self.query_one("#btn-help-close", Button).focus()

    def on_button_pressed(self, event: Button.Pressed) -> None:
        if event.button.id == "btn-help-close":
            self.dismiss()
