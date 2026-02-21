"""Main panel-based chat screen."""
from __future__ import annotations

import asyncio
import contextlib
import secrets
import time
from typing import List, Optional, Set

from textual.app import ComposeResult
from textual.binding import Binding
from textual.containers import Horizontal
from textual.screen import Screen
from textual.widgets import Footer, Header, Input, RichLog, Tree

from felundchat.channel_sync import (
    CONTROL_CHANNEL_ID,
    apply_channel_event,
    apply_circle_name_event,
    parse_channel_event,
    parse_circle_name_event,
)
from felundchat.chat import ensure_default_channel
from felundchat.crypto import make_message_mac, sha256_hex
from felundchat.gossip import GossipNode
from felundchat.models import ChatMessage, State, now_ts
from felundchat.persistence import save_state
from felundchat.rendezvous_client import (
    is_network_error,
    lookup_peer_addrs,
    merge_discovered_peers,
    register_presence,
    safe_api_base_from_env,
    unregister_presence,
)
from felundchat.transport import detect_local_ip

from ._utils import _peer_color, _render_text_with_mentions
from .commands import CommandsMixin
from .modals import InviteModal

from rich.markup import escape as markup_escape


class ChatScreen(CommandsMixin, Screen):
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

    # ── Sidebar ───────────────────────────────────────────────────────────────

    def _circle_label(self, cid: str) -> str:
        circle = self.state.circles.get(cid)
        return circle.name if circle and circle.name else cid[:8]

    def _refresh_sidebar(self) -> None:
        tree = self.query_one("#circle-tree", Tree)
        tree.clear()
        for cid in sorted(self.state.circles.keys()):
            circle_node = tree.root.add(
                f"* {self._circle_label(cid)}",
                data={"type": "circle", "cid": cid},
            )
            ensure_default_channel(self.state, cid)
            for ch_id in sorted(self.state.channels.get(cid, {}).keys()):
                active = cid == self._current_circle_id and ch_id == self._current_channel
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

    # ── Message log ───────────────────────────────────────────────────────────

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

    def _my_names(self) -> set:
        """Names/prefixes that count as 'me' for @mention matching."""
        names = {self.state.node.display_name.lower()}
        names.add(self.state.node.node_id[:8].lower())
        return names

    def _fmt(self, m: ChatMessage) -> str:
        ts = time.strftime("%H:%M", time.localtime(m.created_ts))
        live_name = self.state.node_display_names.get(m.author_node_id, "")
        author = markup_escape(live_name or m.display_name or m.author_node_id[:8])
        body, mentioned = _render_text_with_mentions(m.text, self._my_names())
        is_me = m.author_node_id == self.state.node.node_id
        if is_me:
            return f"[dim]{ts}[/dim] [bold green]{author}[/bold green]: {body}"
        color = _peer_color(m.author_node_id)
        if mentioned:
            # Entire row gets a subtle highlight so the mention is hard to miss.
            return (
                f"[on navy_blue][dim]{ts}[/dim] [bold {color}]{author}[/bold {color}]: {body}[/on navy_blue]"
            )
        return f"[dim]{ts}[/dim] [bold {color}]{author}[/bold {color}]: {body}"

    def _log_system(self, msg: str) -> None:
        self.query_one("#message-log", RichLog).write(f"[dim italic]  {msg}[/dim italic]")

    def _log_raw(self, msg: str) -> None:
        """Write a line to the message log with full Rich markup."""
        self.query_one("#message-log", RichLog).write(msg)

    # ── Rendezvous ────────────────────────────────────────────────────────────

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

    # ── Input handling ────────────────────────────────────────────────────────

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
