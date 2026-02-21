"""Modal dialogs for the felundchat TUI."""
from __future__ import annotations

import asyncio

from textual.app import ComposeResult
from textual.binding import Binding
from textual.containers import Vertical
from textual.screen import ModalScreen
from textual.widgets import Button, Input, Label, RichLog

from ._utils import _try_copy_to_clipboard


class SettingsModal(ModalScreen):
    """F3 / /settings — edit display name and rendezvous URL."""

    BINDINGS = [Binding("escape", "dismiss", "Cancel")]

    DEFAULT_CSS = """
    SettingsModal {
        align: center middle;
    }
    #settings-box {
        width: 74;
        height: auto;
        border: solid $primary;
        padding: 1 2;
        background: $surface;
    }
    #settings-title {
        text-style: bold;
        margin-bottom: 1;
    }
    #settings-label-node {
        color: $text-muted;
        margin-bottom: 1;
    }
    .settings-field-label {
        color: $text-muted;
        margin-top: 1;
    }
    #settings-status {
        height: 1;
        margin-top: 1;
    }
    #settings-buttons {
        layout: horizontal;
        height: auto;
        margin-top: 1;
    }
    #btn-settings-test   { width: 1fr; margin-right: 1; }
    #btn-settings-cancel { width: 1fr; margin-right: 1; }
    #btn-settings-save   { width: 1fr; }
    """

    def __init__(
        self,
        display_name: str,
        rendezvous_base: str,
        node_id: str,
        **kwargs,
    ) -> None:
        super().__init__(**kwargs)
        self._display_name = display_name
        self._rendezvous_base = rendezvous_base
        self._node_id = node_id

    def compose(self) -> ComposeResult:
        from textual.containers import Horizontal
        with Vertical(id="settings-box"):
            yield Label("Settings", id="settings-title")
            yield Label(f"Node ID: {self._node_id}", id="settings-label-node")
            yield Label("Display name", classes="settings-field-label")
            yield Input(value=self._display_name, id="input-display-name", placeholder="anon")
            yield Label("Rendezvous / Relay URL", classes="settings-field-label")
            yield Input(
                value=self._rendezvous_base,
                id="input-rendezvous",
                placeholder="https://felund.com/api  (leave blank to disable)",
            )
            yield Label("", id="settings-status")
            with Horizontal(id="settings-buttons"):
                yield Button("Test", id="btn-settings-test")
                yield Button("Cancel", id="btn-settings-cancel")
                yield Button("Save", id="btn-settings-save", variant="primary")

    async def on_mount(self) -> None:
        self.query_one("#input-display-name", Input).focus()

    async def on_button_pressed(self, event: Button.Pressed) -> None:
        if event.button.id == "btn-settings-save":
            display_name = self.query_one("#input-display-name", Input).value.strip() or "anon"
            rendezvous_base = self.query_one("#input-rendezvous", Input).value.strip().rstrip("/")
            self.dismiss({"display_name": display_name, "rendezvous_base": rendezvous_base})
        elif event.button.id == "btn-settings-cancel":
            self.dismiss(None)
        elif event.button.id == "btn-settings-test":
            await self._test_connection()

    async def _test_connection(self) -> None:
        status = self.query_one("#settings-status", Label)
        url = self.query_one("#input-rendezvous", Input).value.strip().rstrip("/")
        if not url:
            status.update("[red]Enter a URL first.[/red]")
            return
        status.update("[dim]Testing…[/dim]")
        try:
            from felundchat.rendezvous_client import _api_request
            data = await asyncio.to_thread(_api_request, "GET", f"{url}/v1/health")
            version = data.get("version", "?")
            server_time = data.get("time", 0)
            status.update(f"[green]OK — v{version}  (server time {server_time})[/green]")
        except Exception as exc:
            status.update(f"[red]Failed: {exc}[/red]")


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
