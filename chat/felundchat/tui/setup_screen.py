"""First-run wizard screen: host a new circle or join an existing one."""
from __future__ import annotations

from typing import Optional

from textual.app import ComposeResult
from textual.containers import Horizontal, Vertical
from textual.screen import Screen
from textual.widgets import Button, Footer, Input, Label

from felundchat.chat import create_circle, ensure_default_channel
from felundchat.crypto import sha256_hex
from felundchat.invite import is_relay_url, make_felund_code, parse_felund_code
from felundchat.models import Circle
from felundchat.persistence import load_state, save_state
from felundchat.transport import detect_local_ip, public_addr_hint


class SetupScreen(Screen):
    """First-run wizard: host a new circle or join an existing one."""

    DEFAULT_CSS = """
    SetupScreen {
        align: center middle;
    }
    #setup-box {
        width: 64;
        height: auto;
        border: solid $primary;
        padding: 1 2;
    }
    #setup-title {
        text-align: center;
        text-style: bold;
        margin-bottom: 1;
    }
    .mode-row {
        height: auto;
        margin-bottom: 1;
    }
    .mode-btn {
        width: 1fr;
    }
    .field-label {
        margin-top: 1;
        color: $text-muted;
    }
    .hidden {
        display: none;
    }
    #input-circle-name {
        margin-bottom: 0;
    }
    #error-msg {
        color: $error;
        height: 1;
        margin-top: 1;
    }
    #btn-start {
        margin-top: 1;
        width: 100%;
    }
    """

    _mode: str = "host"

    def compose(self) -> ComposeResult:
        with Vertical(id="setup-box"):
            yield Label("Welcome to felundchat", id="setup-title")
            yield Label("How would you like to start?")
            with Horizontal(classes="mode-row"):
                yield Button("Host new circle", id="btn-host", classes="mode-btn", variant="primary")
                yield Button("Join with invite code", id="btn-join", classes="mode-btn")
            yield Label("Display name:", classes="field-label")
            yield Input(placeholder="anon", id="input-name")
            yield Label("Listen port:", classes="field-label")
            yield Input(value="9999", id="input-port")
            yield Label("Circle name (optional):", id="label-circle-name", classes="field-label")
            yield Input(placeholder="e.g. family, work, game night", id="input-circle-name")
            yield Label("Invite code:", id="label-code", classes="field-label hidden")
            yield Input(placeholder="felund1....", id="input-code", classes="hidden")
            yield Label("", id="error-msg")
            yield Button("Start", id="btn-start", variant="success")
        yield Footer()

    async def on_button_pressed(self, event: Button.Pressed) -> None:
        bid = event.button.id
        if bid == "btn-host":
            self._mode = "host"
            self.query_one("#btn-host").variant = "primary"
            self.query_one("#btn-join").variant = "default"
            self.query_one("#input-code").add_class("hidden")
            self.query_one("#label-code").add_class("hidden")
            self.query_one("#input-circle-name").remove_class("hidden")
            self.query_one("#label-circle-name").remove_class("hidden")
        elif bid == "btn-join":
            self._mode = "join"
            self.query_one("#btn-join").variant = "primary"
            self.query_one("#btn-host").variant = "default"
            self.query_one("#input-code").remove_class("hidden")
            self.query_one("#label-code").remove_class("hidden")
            self.query_one("#input-circle-name").add_class("hidden")
            self.query_one("#label-circle-name").add_class("hidden")
        elif bid == "btn-start":
            await self._do_start()

    def _show_error(self, msg: str) -> None:
        self.query_one("#error-msg", Label).update(msg)

    async def _do_start(self) -> None:
        # Local import breaks the setup_screen ↔ chat_screen circular dependency.
        from .chat_screen import ChatScreen

        state = load_state()

        name = self.query_one("#input-name", Input).value.strip() or "anon"
        state.node.display_name = name

        port_raw = self.query_one("#input-port", Input).value.strip()
        try:
            port = int(port_raw)
            if not (1 <= port <= 65535):
                raise ValueError("out of range")
        except ValueError:
            self._show_error("Invalid port number (1-65535).")
            return
        state.node.port = port
        state.node.bind = detect_local_ip()

        initial_msg: Optional[str] = None
        initial_invite_code: Optional[str] = None
        bootstrap_peer: Optional[str] = None
        bootstrap_circle: Optional[str] = None

        if self._mode == "host":
            circle = create_circle(state)
            circle_name = self.query_one("#input-circle-name", Input).value.strip()
            if circle_name:
                circle.name = circle_name
            save_state(state)
            addr = public_addr_hint(state.node.bind, state.node.port)
            initial_invite_code = make_felund_code(circle.secret_hex, addr)
            label = f'"{circle_name}"' if circle_name else circle.circle_id[:8]
            initial_msg = f"Circle {label} created! Share the invite code with friends."
        else:
            code_val = self.query_one("#input-code", Input).value.strip()
            try:
                secret_hex, peer_addr = parse_felund_code(code_val)
            except Exception as e:
                self._show_error(f"Invalid invite code: {e}")
                return
            secret = bytes.fromhex(secret_hex)
            circle_id = sha256_hex(secret)[:24]
            state.circles[circle_id] = Circle(circle_id=circle_id, secret_hex=secret_hex)
            state.circle_members.setdefault(circle_id, set()).add(state.node.node_id)
            ensure_default_channel(state, circle_id)
            save_state(state)
            # Web-client codes carry a relay URL instead of a TCP address.
            # In that case skip the direct TCP bootstrap; the relay loop handles it.
            bootstrap_peer = peer_addr if not is_relay_url(peer_addr) else None
            bootstrap_circle = circle_id

        await self.app.push_screen(
            ChatScreen(
                state,
                initial_msg=initial_msg,
                initial_invite_code=initial_invite_code,
                bootstrap_peer=bootstrap_peer,
                bootstrap_circle=bootstrap_circle,
            )
        )
