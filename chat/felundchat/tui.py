#!/usr/bin/env python3
"""felundchat Textual TUI — panel-based terminal chat UI.

Install dependency:  pip install textual
Launch:              python felundchat.py tui
                     python -m felundchat tui
"""
from __future__ import annotations

import asyncio
import contextlib
import re
import secrets
import subprocess
import sys
import time
from typing import List, Optional, Set

from rich.markup import escape as markup_escape

from textual.app import App, ComposeResult
from textual.binding import Binding
from textual.containers import Horizontal, Vertical
from textual.screen import ModalScreen, Screen
from textual.widgets import Button, Footer, Header, Input, Label, RichLog, Tree

from felundchat.channel_sync import (
    CONTROL_CHANNEL_ID,
    apply_channel_event,
    apply_circle_name_event,
    make_channel_event_message,
    make_circle_name_message,
    parse_channel_event,
    parse_circle_name_event,
)
from felundchat.chat import create_circle, ensure_default_channel
from felundchat.crypto import make_message_mac, sha256_hex
from felundchat.gossip import GossipNode
from felundchat.invite import make_felund_code, parse_felund_code
from felundchat.models import Channel, ChatMessage, Circle, State, now_ts
from felundchat.persistence import load_state, save_state
from felundchat.rendezvous_client import (
    is_network_error,
    lookup_peer_addrs,
    merge_discovered_peers,
    register_presence,
    safe_api_base_from_env,
    unregister_presence,
)
from felundchat.transport import detect_local_ip, public_addr_hint


# ---------------------------------------------------------------------------
# Clipboard helper
# ---------------------------------------------------------------------------

def _try_copy_to_clipboard(text: str) -> bool:
    """Try to copy *text* to the system clipboard. Returns True on success."""
    if sys.platform == "win32":
        cmds = [["clip"]]
    elif sys.platform == "darwin":
        cmds = [["pbcopy"]]
    else:
        cmds = [
            ["xclip", "-selection", "clipboard"],
            ["xsel", "--clipboard", "--input"],
            ["clip.exe"],    # WSL
            ["wl-copy"],     # Wayland
        ]
    for cmd in cmds:
        try:
            result = subprocess.run(cmd, input=text.encode(), capture_output=True, timeout=2)
            if result.returncode == 0:
                return True
        except (FileNotFoundError, subprocess.TimeoutExpired, OSError):
            continue
    return False


# ---------------------------------------------------------------------------
# Inline Markdown → Rich markup renderer
# ---------------------------------------------------------------------------

def _render_text(text: str) -> str:
    """Escape Rich markup in *text*, then convert common inline Markdown to Rich markup.

    Supported syntax:
      **bold**   *italic*   `code`   ~~strikethrough~~
    """
    # Escape any Rich markup the user typed (prevents injection)
    out = markup_escape(text)

    # Code spans — processed first so their content isn't altered by later rules.
    # markup_escape already escaped the content; we just add styling around it.
    out = re.sub(
        r"`([^`\n]+)`",
        r"[bold bright_black on grey23] \1 [/bold bright_black on grey23]",
        out,
    )

    # Bold  **text**
    out = re.sub(r"\*\*(.+?)\*\*", r"[bold]\1[/bold]", out)

    # Italic  *text*  (single asterisk; bold already consumed the doubles)
    out = re.sub(r"\*([^*\n]+)\*", r"[italic]\1[/italic]", out)

    # Italic  _text_  (only at non-word boundaries to avoid breaking snake_case)
    out = re.sub(r"(?<!\w)_([^_\n]+)_(?!\w)", r"[italic]\1[/italic]", out)

    # Strikethrough  ~~text~~
    out = re.sub(r"~~(.+?)~~", r"[strike]\1[/strike]", out)

    return out


# ---------------------------------------------------------------------------
# Invite code modal
# ---------------------------------------------------------------------------

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


# ---------------------------------------------------------------------------
# Setup screen
# ---------------------------------------------------------------------------

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
            bootstrap_peer = peer_addr
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


# ---------------------------------------------------------------------------
# Chat screen
# ---------------------------------------------------------------------------

class ChatScreen(Screen):
    """Main panel-based chat interface."""

    BINDINGS = [
        Binding("ctrl+q", "app.quit", "Quit"),
        Binding("ctrl+i", "show_invite", "Invite code"),
        Binding("escape", "focus_input", "Focus input"),
    ]

    DEFAULT_CSS = """
    ChatScreen {
        layout: vertical;
    }
    #chat-body {
        height: 1fr;
    }
    #circle-tree {
        width: 22;
        border-right: solid $primary-darken-2;
        padding: 0 1;
    }
    #message-log {
        width: 1fr;
        padding: 0 1;
    }
    #message-input {
        dock: bottom;
        height: 3;
        border-top: solid $primary-darken-2;
    }
    """

    def __init__(
        self,
        state: State,
        initial_msg: Optional[str] = None,
        initial_invite_code: Optional[str] = None,
        bootstrap_peer: Optional[str] = None,
        bootstrap_circle: Optional[str] = None,
        **kwargs,
    ) -> None:
        super().__init__(**kwargs)
        self.state = state
        self._initial_msg = initial_msg
        self._initial_invite_code = initial_invite_code
        self._bootstrap_peer = bootstrap_peer
        self._bootstrap_circle = bootstrap_circle
        self.node: Optional[GossipNode] = None
        self._current_circle_id: Optional[str] = None
        self._current_channel: str = "general"
        self._seen: Set[str] = set()
        self._gossip_task: Optional[asyncio.Task] = None
        self._rendezvous_task: Optional[asyncio.Task] = None

    def compose(self) -> ComposeResult:
        yield Header()
        with Horizontal(id="chat-body"):
            yield Tree("Circles", id="circle-tree")
            yield RichLog(id="message-log", markup=True, auto_scroll=True, highlight=False)
        yield Input(placeholder="Type a message... (/help for commands)", id="message-input")
        yield Footer()

    async def on_mount(self) -> None:
        for cid in self.state.circles:
            ensure_default_channel(self.state, cid)

        circles = sorted(self.state.circles.keys())
        if circles:
            self._current_circle_id = self._bootstrap_circle or circles[0]

        if not self.state.node.bind or self.state.node.bind == "0.0.0.0":
            self.state.node.bind = detect_local_ip()

        self.node = GossipNode(self.state)
        await self.node.start_server()
        self._gossip_task = asyncio.create_task(self.node.gossip_loop())

        api_base = safe_api_base_from_env()
        if api_base:
            self._rendezvous_task = asyncio.create_task(self._rendezvous_loop(api_base))
            self._log_system(f"Rendezvous enabled: {api_base}")

        if self._bootstrap_peer and self._bootstrap_circle:
            asyncio.create_task(
                self.node.connect_and_sync(self._bootstrap_peer, self._bootstrap_circle)
            )

        self._refresh_sidebar()
        self._load_history()

        if self._initial_msg:
            self._log_system(self._initial_msg)

        if self._initial_invite_code:
            self.call_after_refresh(self.app.push_screen, InviteModal(self._initial_invite_code))

        self.set_interval(1.0, self._poll_new_messages)
        self.set_interval(10.0, self._refresh_sidebar)

        self.query_one("#message-input", Input).focus()

    async def on_unmount(self) -> None:
        if self.node:
            self.node.stop()

        for task in (self._gossip_task, self._rendezvous_task):
            if task:
                task.cancel()
                with contextlib.suppress(asyncio.CancelledError):
                    await task

        api_base = safe_api_base_from_env()
        if api_base and self.node:
            for cid in list(self.state.circles.keys()):
                with contextlib.suppress(Exception):
                    await asyncio.to_thread(unregister_presence, api_base, self.state, cid)

        if self.node:
            await self.node.stop_server()

    # ── Sidebar ──────────────────────────────────────────────────────────────

    def _circle_label(self, cid: str) -> str:
        circle = self.state.circles.get(cid)
        return circle.name if circle and circle.name else cid[:8]

    def _refresh_sidebar(self) -> None:
        tree = self.query_one("#circle-tree", Tree)
        tree.clear()
        for cid in sorted(self.state.circles.keys()):
            circle_node = tree.root.add(f"* {self._circle_label(cid)}", data={"type": "circle", "cid": cid})
            ensure_default_channel(self.state, cid)
            for ch_id in sorted(self.state.channels.get(cid, {}).keys()):
                active = (cid == self._current_circle_id and ch_id == self._current_channel)
                label = f"#{ch_id} <" if active else f"#{ch_id}"
                circle_node.add_leaf(label, data={"type": "channel", "cid": cid, "channel": ch_id})
            circle_node.expand()
        self._update_title()

    def _update_title(self) -> None:
        peers = 0
        if self._current_circle_id:
            peers = max(0, len(self.state.circle_members.get(self._current_circle_id, set())) - 1)
        label = self._circle_label(self._current_circle_id) if self._current_circle_id else "none"
        peer_word = "peer" if peers == 1 else "peers"
        self.title = f"felundchat  {label} | #{self._current_channel} | {peers} {peer_word}"

    # ── Message log ──────────────────────────────────────────────────────────

    def _visible_msgs(self) -> List[ChatMessage]:
        return sorted(
            (
                m for m in self.state.messages.values()
                if m.circle_id == self._current_circle_id
                and m.channel_id == self._current_channel
            ),
            key=lambda m: (m.created_ts, m.msg_id),
        )

    def _load_history(self) -> None:
        if not self._current_circle_id:
            return
        log = self.query_one("#message-log", RichLog)
        for m in self._visible_msgs()[-50:]:
            self._seen.add(m.msg_id)
            log.write(self._fmt(m))

    def _poll_new_messages(self) -> None:
        if not self._current_circle_id:
            return
        self._process_control_events()
        log = self.query_one("#message-log", RichLog)
        new_msgs = [m for m in self._visible_msgs() if m.msg_id not in self._seen]
        for m in new_msgs:
            self._seen.add(m.msg_id)
            log.write(self._fmt(m))
        if new_msgs:
            self._update_title()

    def _process_control_events(self) -> None:
        if not self._current_circle_id:
            return
        for m in list(self.state.messages.values()):
            if m.circle_id != self._current_circle_id:
                continue
            if m.channel_id != CONTROL_CHANNEL_ID:
                continue
            if m.msg_id in self._seen:
                continue
            self._seen.add(m.msg_id)
            event = parse_channel_event(m.text)
            if event:
                apply_channel_event(self.state, self._current_circle_id, event)
                save_state(self.state)
                continue
            name_event = parse_circle_name_event(m.text)
            if name_event:
                changed = apply_circle_name_event(self.state, self._current_circle_id, name_event)
                if changed:
                    save_state(self.state)
                    self._refresh_sidebar()

    def _fmt(self, m: ChatMessage) -> str:
        ts = time.strftime("%H:%M", time.localtime(m.created_ts))
        live_name = self.state.node_display_names.get(m.author_node_id, "")
        author = markup_escape(live_name or m.display_name or m.author_node_id[:8])
        body = _render_text(m.text)
        is_me = m.author_node_id == self.state.node.node_id
        if is_me:
            return f"[dim]{ts}[/dim] [bold green]{author}[/bold green]: {body}"
        return f"[dim]{ts}[/dim] [bold]{author}[/bold]: {body}"

    def _log_system(self, msg: str) -> None:
        self.query_one("#message-log", RichLog).write(f"[dim italic]  {msg}[/dim italic]")

    def _log_raw(self, msg: str) -> None:
        """Write a line to the message log with full Rich markup, no auto-dimming."""
        self.query_one("#message-log", RichLog).write(msg)

    # ── Rendezvous ───────────────────────────────────────────────────────────

    async def _rendezvous_loop(self, api_base: str) -> None:
        while not self.node._stop_event.is_set():
            async with self.node._lock:
                cids = list(self.state.circles.keys())
            for cid in cids:
                try:
                    await asyncio.to_thread(register_presence, api_base, self.state, cid)
                    discovered = await asyncio.to_thread(lookup_peer_addrs, api_base, self.state, cid)
                    async with self.node._lock:
                        changed = merge_discovered_peers(self.state, cid, discovered)
                        if changed:
                            save_state(self.state)
                    for _, addr in discovered[:5]:
                        await self.node.connect_and_sync(addr, cid)
                except Exception as e:
                    if self.node.debug_sync and not is_network_error(e):
                        self._log_system(f"[api] {cid[:8]}: {type(e).__name__}: {e}")
            try:
                await asyncio.wait_for(self.node._stop_event.wait(), timeout=8)
            except asyncio.TimeoutError:
                pass

    # ── Input handling ───────────────────────────────────────────────────────

    async def on_input_submitted(self, event: Input.Submitted) -> None:
        text = event.value.strip()
        self.query_one("#message-input", Input).value = ""
        if not text:
            return
        if text.startswith("/"):
            await self._handle_command(text)
        else:
            await self._send_message(text)

    async def _send_message(self, text: str) -> None:
        if not self._current_circle_id:
            self._log_system("No active circle. Type /help to get started.")
            return
        circle = self.state.circles.get(self._current_circle_id)
        if not circle:
            return
        created = now_ts()
        msg_id = sha256_hex(
            f"{self.state.node.node_id}|{created}|{secrets.token_hex(8)}".encode()
        )[:32]
        msg = ChatMessage(
            msg_id=msg_id,
            circle_id=self._current_circle_id,
            channel_id=self._current_channel,
            author_node_id=self.state.node.node_id,
            display_name=self.state.node.display_name,
            created_ts=created,
            text=text,
        )
        msg.mac = make_message_mac(circle.secret_hex, msg)
        async with self.node._lock:
            self.state.messages[msg_id] = msg
            save_state(self.state)
        self._seen.add(msg_id)
        self.query_one("#message-log", RichLog).write(self._fmt(msg))
        asyncio.create_task(self._sync_once())

    async def _sync_once(self) -> None:
        if not self._current_circle_id or not self.node:
            return
        async with self.node._lock:
            peers = [p.addr for p in self.node.known_peers_for_circle(self._current_circle_id)]
        for addr in peers[:5]:
            await self.node.connect_and_sync(addr, self._current_circle_id)

    # ── Slash commands ───────────────────────────────────────────────────────

    async def _handle_command(self, text: str) -> None:
        parts = text.split()
        cmd = parts[0].lower()

        if cmd == "/help":
            C = "bold cyan"
            A = "dim"
            topic = parts[1].lstrip("/").lower() if len(parts) > 1 else ""
            HELP = {
                "invite":   f"  [{C}]/invite[/{C}]  —  Show invite code for the active circle (also copied to clipboard)",
                "join":     f"  [{C}]/join[/{C}] [{A}]<code>[/{A}]  —  Join a circle using a felund invite code",
                "circles":  f"  [{C}]/circles[/{C}]  —  List all circles you belong to",
                "circle":   "\n".join([
                    f"  [{C}]/circle create[/{C}] [{A}][name][/{A}]  —  Create a new circle and show its invite code",
                    f"  [{C}]/circle name[/{C}] [{A}]<label>[/{A}]    —  Set a friendly name for the active circle (gossiped)",
                    f"  [{C}]/circle leave[/{C}]              —  Leave and locally delete the active circle",
                ]),
                "channels": f"  [{C}]/channels[/{C}]  —  List channels in the active circle",
                "channel":  "\n".join([
                    f"  [{C}]/channel create[/{C}] [{A}]<name> [public|key|invite][/{A}]  —  Create a channel",
                    f"  [{C}]/channel join[/{C}] [{A}]<name>[/{A}]      —  Join an existing channel",
                    f"  [{C}]/channel switch[/{C}] [{A}]<name>[/{A}]    —  Switch to a channel",
                    f"  [{C}]/channel leave[/{C}] [{A}]<name>[/{A}]     —  Leave a channel",
                    f"  [{C}]/channel requests[/{C}] [{A}]<name>[/{A}]  —  Show pending access requests (owner only)",
                    f"  [{C}]/channel approve[/{C}] [{A}]<name> <node_id>[/{A}]  —  Approve a request (owner only)",
                ]),
                "who":      f"  [{C}]/who[/{C}] [{A}][channel][/{A}]  —  Show members of the active (or named) channel",
                "name":     f"  [{C}]/name[/{C}]           —  Show your current display name\n  [{C}]/name[/{C}] [{A}]<new_name>[/{A}]  —  Update display name and sync to peers",
                "debug":    f"  [{C}]/debug[/{C}]  —  Toggle gossip debug log",
                "quit":     f"  [{C}]/quit[/{C}]   —  Exit felundchat",
            }
            if topic and topic in HELP:
                self._log_raw(HELP[topic])
            else:
                self._log_raw("[bold]─── Commands ───────────────────────────────────────[/bold]")
                self._log_raw(f"  [{C}]/invite[/{C}]                              Show invite code for active circle")
                self._log_raw(f"  [{C}]/join[/{C}] [{A}]<code>[/{A}]                         Join a circle from an invite code")
                self._log_raw(f"  [{C}]/circles[/{C}]                             List all circles")
                self._log_raw(f"  [{C}]/circle create[/{C}] [{A}][name][/{A}]              Create a new circle")
                self._log_raw(f"  [{C}]/circle name[/{C}] [{A}]<label>[/{A}]               Rename active circle (gossiped)")
                self._log_raw(f"  [{C}]/circle leave[/{C}]                        Leave active circle")
                self._log_raw(f"  [{C}]/channels[/{C}]                            List channels in active circle")
                self._log_raw(f"  [{C}]/channel create[/{C}] [{A}]<name> [mode][/{A}]       Create a channel (public/key/invite)")
                self._log_raw(f"  [{C}]/channel join|switch|leave[/{C}] [{A}]<name>[/{A}]   Manage channel membership")
                self._log_raw(f"  [{C}]/channel requests[/{C}] [{A}]<name>[/{A}]            Pending requests (owner only)")
                self._log_raw(f"  [{C}]/channel approve[/{C}] [{A}]<name> <node_id>[/{A}]   Approve a request (owner only)")
                self._log_raw(f"  [{C}]/who[/{C}] [{A}][channel][/{A}]                      Show channel members")
                self._log_raw(f"  [{C}]/name[/{C}] [{A}][new_name][/{A}]                    Show or update your display name")
                self._log_raw(f"  [{C}]/debug[/{C}]                               Toggle gossip debug log")
                self._log_raw(f"  [{C}]/quit[/{C}]                                Exit")
                self._log_raw("[dim]Tip: /help <command> for details  e.g. /help channel[/dim]")
                self._log_raw("[bold]─── Formatting ─────────────────────────────────────[/bold]")
                self._log_raw("  [bold]**bold**[/bold]   [italic]*italic*[/italic]   [bold bright_black on grey23] `code` [/bold bright_black on grey23]   [strike]~~strike~~[/strike]")

        elif cmd == "/quit":
            await self.app.action_quit()

        elif cmd == "/circles":
            for cid in sorted(self.state.circles.keys()):
                count = len(self.state.circle_members.get(cid, set()))
                active = " <" if cid == self._current_circle_id else ""
                label = self._circle_label(cid)
                self._log_system(f"  {label} ({count} members){active}")

        elif cmd == "/channels":
            if not self._current_circle_id:
                self._log_system("No active circle.")
                return
            ensure_default_channel(self.state, self._current_circle_id)
            for ch in sorted(self.state.channels.get(self._current_circle_id, {}).keys()):
                active = " <" if ch == self._current_channel else ""
                self._log_system(f"  #{ch}{active}")

        elif cmd == "/invite":
            if not self._current_circle_id:
                self._log_system("No active circle.")
                return
            circle = self.state.circles.get(self._current_circle_id)
            if circle:
                addr = public_addr_hint(self.state.node.bind, self.state.node.port)
                code = make_felund_code(circle.secret_hex, addr)
                await self.app.push_screen(InviteModal(code))

        elif cmd == "/join":
            if len(parts) < 2:
                self._log_system("Usage: /join <invite_code>")
                return
            try:
                secret_hex, peer_addr = parse_felund_code(parts[1])
            except Exception as e:
                self._log_system(f"Invalid code: {e}")
                return
            secret = bytes.fromhex(secret_hex)
            circle_id = sha256_hex(secret)[:24]
            self.state.circles[circle_id] = Circle(circle_id=circle_id, secret_hex=secret_hex)
            self.state.circle_members.setdefault(circle_id, set()).add(self.state.node.node_id)
            ensure_default_channel(self.state, circle_id)
            async with self.node._lock:
                save_state(self.state)
            self._current_circle_id = circle_id
            self._current_channel = "general"
            self._seen = set()
            self.query_one("#message-log", RichLog).clear()
            self._refresh_sidebar()
            asyncio.create_task(self.node.connect_and_sync(peer_addr, circle_id))
            self._log_system(f"Joined circle {circle_id[:8]}. Syncing...")
            self._load_history()

        elif cmd == "/who":
            target = parts[1].lstrip("#") if len(parts) > 1 else self._current_channel
            if not self._current_circle_id:
                return
            members = sorted(
                self.state.channel_members.get(self._current_circle_id, {}).get(target, set())
            )
            self._log_system(f"#{target} — {len(members)} member(s):")
            for nid in members:
                p = self.state.peers.get(nid)
                display = self.state.node_display_names.get(nid, nid[:8])
                if nid == self.state.node.node_id:
                    tag = "(you)"
                elif p:
                    tag = f"@ {p.addr}"
                else:
                    tag = ""
                self._log_system(f"  {display} [{nid[:8]}] {tag}")

        elif cmd == "/name":
            if len(parts) == 1:
                self._log_system(f"Your display name: {self.state.node.display_name}")
                return
            new_name = " ".join(parts[1:]).strip()[:40]
            if not new_name:
                self._log_system("Name cannot be empty.")
                return
            self.state.node.display_name = new_name
            self.state.node_display_names[self.state.node.node_id] = new_name
            async with self.node._lock:
                save_state(self.state)
            for cid in list(self.state.circles.keys()):
                event = {
                    "t": "CHANNEL_EVT", "op": "rename",
                    "node_id": self.state.node.node_id,
                    "display_name": new_name,
                }
                msg = make_channel_event_message(self.state, cid, event)
                if msg:
                    self.state.messages[msg.msg_id] = msg
                    self._seen.add(msg.msg_id)
            self._log_system(f"Display name updated to '{new_name}'. Syncing to peers...")
            asyncio.create_task(self._sync_once())

        elif cmd == "/debug":
            if self.node:
                self.node.debug_sync = not self.node.debug_sync
                self._log_system(f"Sync debug: {'on' if self.node.debug_sync else 'off'}")

        elif cmd == "/circle":
            await self._circle_mgmt_cmd(parts[1:])

        elif cmd == "/channel":
            await self._channel_cmd(parts[1:])

        else:
            self._log_system(f"Unknown command '{cmd}'. Type /help.")

    def _gossip_circle_name(self, circle_id: str, name: str) -> None:
        """Queue a CIRCLE_NAME_EVT message so peers receive the friendly name."""
        msg = make_circle_name_message(self.state, circle_id, name)
        if msg:
            self.state.messages[msg.msg_id] = msg
            self._seen.add(msg.msg_id)

    async def _circle_mgmt_cmd(self, args: list) -> None:
        if not args:
            self._log_system("Usage: /circle create [name]  |  /circle name <label>  |  /circle leave")
            return
        sub = args[0].lower()

        if sub == "create":
            name = " ".join(args[1:]).strip() if len(args) > 1 else ""
            circle = create_circle(self.state)
            if name:
                circle.name = name
            async with self.node._lock:
                save_state(self.state)
            if name:
                self._gossip_circle_name(circle.circle_id, name)
            # Switch to the new circle
            self._current_circle_id = circle.circle_id
            self._current_channel = "general"
            self._seen = set()
            self.query_one("#message-log", RichLog).clear()
            self._refresh_sidebar()
            # Show invite modal immediately
            addr = public_addr_hint(self.state.node.bind, self.state.node.port)
            code = make_felund_code(circle.secret_hex, addr)
            label = f'"{name}"' if name else circle.circle_id[:8]
            self._log_system(f"Circle {label} created.")
            await self.app.push_screen(InviteModal(code))

        elif sub == "name":
            if len(args) < 2:
                self._log_system("Usage: /circle name <friendly label>")
                return
            if not self._current_circle_id:
                self._log_system("No active circle.")
                return
            new_name = " ".join(args[1:]).strip()
            circle = self.state.circles.get(self._current_circle_id)
            if circle:
                circle.name = new_name
                save_state(self.state)
                self._gossip_circle_name(self._current_circle_id, new_name)
                self._refresh_sidebar()
                self._log_system(f"Circle renamed to '{new_name}'. Name will gossip to peers.")

        elif sub == "leave":
            cid = self._current_circle_id
            if not cid:
                self._log_system("No active circle.")
                return
            label = self._circle_label(cid)
            # Remove all state for this circle
            self.state.circles.pop(cid, None)
            self.state.circle_members.pop(cid, None)
            self.state.channels.pop(cid, None)
            self.state.channel_members.pop(cid, None)
            self.state.channel_requests.pop(cid, None)
            to_drop = [mid for mid, m in self.state.messages.items() if m.circle_id == cid]
            for mid in to_drop:
                del self.state.messages[mid]
            save_state(self.state)
            self._log_system(f"Left circle '{label}'.")
            remaining = sorted(self.state.circles.keys())
            if remaining:
                self._current_circle_id = remaining[0]
                self._current_channel = "general"
                self._seen = set()
                self.query_one("#message-log", RichLog).clear()
                self._refresh_sidebar()
                self._load_history()
            else:
                self._current_circle_id = None
                self.query_one("#message-log", RichLog).clear()
                self._refresh_sidebar()
                await self.app.push_screen(SetupScreen())

        else:
            self._log_system("Usage: /circle create [name]  |  /circle name <label>  |  /circle leave")

    async def _channel_cmd(self, args: list) -> None:
        if not args or not self._current_circle_id:
            self._log_system("Usage: /channel create|join|switch|leave <name>")
            return

        ensure_default_channel(self.state, self._current_circle_id)
        channels = self.state.channels[self._current_circle_id]
        members_map = self.state.channel_members[self._current_circle_id]
        requests_map = self.state.channel_requests.setdefault(self._current_circle_id, {})
        sub = args[0].lower()

        if sub == "create":
            if len(args) < 2:
                self._log_system("Usage: /channel create <name> [public|key|invite]")
                return
            ch_id = args[1].lower()
            access_mode = args[2].lower() if len(args) > 2 else "public"
            if access_mode not in {"public", "key", "invite"}:
                self._log_system("Access mode must be: public, key, or invite")
                return
            if ch_id in channels:
                self._log_system(f"#{ch_id} already exists.")
                return
            channels[ch_id] = Channel(
                channel_id=ch_id,
                circle_id=self._current_circle_id,
                created_by=self.state.node.node_id,
                created_ts=now_ts(),
                access_mode=access_mode,
            )
            members_map.setdefault(ch_id, set()).add(self.state.node.node_id)
            requests_map.setdefault(ch_id, set())
            event = {
                "t": "CHANNEL_EVT", "op": "create",
                "circle_id": self._current_circle_id, "channel_id": ch_id,
                "access_mode": access_mode, "key_hash": "",
                "actor_node_id": self.state.node.node_id,
                "created_by": self.state.node.node_id, "created_ts": now_ts(),
            }
            msg = make_channel_event_message(self.state, self._current_circle_id, event)
            if msg:
                self.state.messages[msg.msg_id] = msg
                self._seen.add(msg.msg_id)
            async with self.node._lock:
                save_state(self.state)
            self._refresh_sidebar()
            self._log_system(f"Created #{ch_id} [{access_mode}].")

        elif sub == "switch":
            if len(args) < 2:
                self._log_system("Usage: /channel switch <name>")
                return
            ch_id = args[1].lower()
            if ch_id not in channels:
                self._log_system(f"Unknown channel #{ch_id}.")
                return
            self._current_channel = ch_id
            self._seen = set()
            self.query_one("#message-log", RichLog).clear()
            self._load_history()
            self._refresh_sidebar()

        elif sub == "join":
            if len(args) < 2:
                self._log_system("Usage: /channel join <name>")
                return
            ch_id = args[1].lower()
            if ch_id not in channels:
                self._log_system(f"Unknown channel #{ch_id}.")
                return
            members_map.setdefault(ch_id, set()).add(self.state.node.node_id)
            async with self.node._lock:
                save_state(self.state)
            self._log_system(f"Joined #{ch_id}.")

        elif sub == "leave":
            if len(args) < 2:
                self._log_system("Usage: /channel leave <name>")
                return
            ch_id = args[1].lower()
            if ch_id == "general":
                self._log_system("Cannot leave #general.")
                return
            members_map.get(ch_id, set()).discard(self.state.node.node_id)
            async with self.node._lock:
                save_state(self.state)
            if self._current_channel == ch_id:
                self._current_channel = "general"
                self._seen = set()
                self.query_one("#message-log", RichLog).clear()
                self._load_history()
            self._refresh_sidebar()
            self._log_system(f"Left #{ch_id}.")

        elif sub == "requests":
            if len(args) < 2:
                self._log_system("Usage: /channel requests <name>")
                return
            ch_id = args[1].lower()
            if ch_id not in channels:
                self._log_system(f"Unknown channel #{ch_id}.")
                return
            channel = channels[ch_id]
            if channel.created_by != self.state.node.node_id:
                self._log_system("Only the channel owner can view pending requests.")
                return
            reqs = sorted(requests_map.get(ch_id, set()))
            if not reqs:
                self._log_system(f"#{ch_id} — no pending requests.")
            else:
                self._log_system(f"#{ch_id} — {len(reqs)} pending request(s):")
                for nid in reqs:
                    display = self.state.node_display_names.get(nid, nid[:8])
                    self._log_system(f"  {display} [{nid[:8]}]")

        elif sub == "approve":
            if len(args) < 3:
                self._log_system("Usage: /channel approve <name> <node_id>")
                return
            ch_id = args[1].lower()
            prefix = args[2]
            if ch_id not in channels:
                self._log_system(f"Unknown channel #{ch_id}.")
                return
            channel = channels[ch_id]
            if channel.created_by != self.state.node.node_id:
                self._log_system("Only the channel owner can approve requests.")
                return
            full_id = next(
                (nid for nid in requests_map.get(ch_id, set()) if nid.startswith(prefix)),
                None,
            )
            if not full_id:
                self._log_system(f"No pending request matching '{prefix}' in #{ch_id}.")
                return
            event = {
                "t": "CHANNEL_EVT", "op": "approve",
                "circle_id": self._current_circle_id, "channel_id": ch_id,
                "actor_node_id": self.state.node.node_id,
                "target_node_id": full_id,
            }
            apply_channel_event(self.state, self._current_circle_id, event)
            msg = make_channel_event_message(self.state, self._current_circle_id, event)
            if msg:
                self.state.messages[msg.msg_id] = msg
                self._seen.add(msg.msg_id)
            async with self.node._lock:
                save_state(self.state)
            display = self.state.node_display_names.get(full_id, full_id[:8])
            self._log_system(f"Approved {display} [{full_id[:8]}] to join #{ch_id}.")

        else:
            self._log_system("Usage: /channel create|join|switch|leave|requests|approve <name>")

    # ── Sidebar interaction ───────────────────────────────────────────────────

    def on_tree_node_selected(self, event: Tree.NodeSelected) -> None:
        data = event.node.data
        if not data or data.get("type") != "channel":
            return
        cid = data["cid"]
        ch_id = data["channel"]
        if cid == self._current_circle_id and ch_id == self._current_channel:
            self.query_one("#message-input", Input).focus()
            return
        self._current_circle_id = cid
        self._current_channel = ch_id
        self._seen = set()
        self.query_one("#message-log", RichLog).clear()
        self._load_history()
        self._refresh_sidebar()
        self.query_one("#message-input", Input).focus()

    # ── Actions ───────────────────────────────────────────────────────────────

    async def action_show_invite(self) -> None:
        await self._handle_command("/invite")

    def action_focus_input(self) -> None:
        self.query_one("#message-input", Input).focus()


# ---------------------------------------------------------------------------
# App
# ---------------------------------------------------------------------------

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
