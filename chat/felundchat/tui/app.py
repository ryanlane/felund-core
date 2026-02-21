"""Top-level Textual application."""
from __future__ import annotations

from textual.app import App
from textual.binding import Binding

from felundchat.persistence import load_state

from .chat_screen import ChatScreen
from .setup_screen import SetupScreen


class FelundApp(App):
    """felundchat terminal UI application."""

    TITLE = "felundchat"
    BINDINGS = [Binding("ctrl+q", "quit", "Quit")]

    def on_mount(self) -> None:
        state = load_state()
        if state.circles:
            self.push_screen(ChatScreen(state))
        else:
            self.push_screen(SetupScreen())
