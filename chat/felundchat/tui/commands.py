"""Slash-command mixin for ChatScreen.

All public methods are injected into ChatScreen via multiple inheritance.
They rely on attributes and helper methods defined in ChatScreen; the mixin
itself is intentionally free of __init__ and screen-lifecycle code.
"""
from __future__ import annotations

import asyncio

from textual.widgets import RichLog

from felundchat.channel_sync import (
    apply_channel_event,
    make_channel_event_message,
    make_circle_name_message,
)
from felundchat.chat import create_circle, ensure_default_channel
from felundchat.crypto import sha256_hex
from felundchat.invite import make_felund_code, parse_felund_code
from felundchat.models import Channel, Circle, now_ts
from felundchat.persistence import save_state
from felundchat.transport import public_addr_hint

from ._utils import mentions_me, _render_text_with_mentions
from .modals import HelpModal, InviteModal, SettingsModal


# ---------------------------------------------------------------------------
# Help content
# ---------------------------------------------------------------------------

def _help_lines(topic: str = "") -> list:
    """Return Rich-markup lines for the help modal.

    Pass an empty string (or no argument) for the full command reference.
    Pass a command name (e.g. ``"channel"``) for focused help on that group.
    """
    FULL = [
        "[bold]felundchat — slash commands[/bold]",
        "",
        "[bold cyan]General[/bold cyan]",
        "  [bold]/help[/bold] [dim][topic][/dim]           This screen; /help channel for channel docs",
        "  [bold]/settings[/bold]               Open settings modal (display name, relay URL)",
        "  [bold]/quit[/bold]                   Exit felundchat",
        "  [bold]/debug[/bold]                  Toggle gossip-sync debug log",
        "",
        "[bold cyan]Identity[/bold cyan]",
        "  [bold]/name[/bold]                   Show your current display name",
        "  [bold]/name[/bold] [dim]<new name>[/dim]        Change your display name (gossiped to peers)",
        "",
        "[bold cyan]Circles[/bold cyan]",
        "  [bold]/circles[/bold]                List all circles you are in",
        "  [bold]/circle create[/bold] [dim][name][/dim]   Create a new circle (shows invite code)",
        "  [bold]/circle name[/bold] [dim]<label>[/dim]    Rename the active circle",
        "  [bold]/circle leave[/bold]            Leave the active circle",
        "  [bold]/invite[/bold]                  Show/copy invite code for the active circle",
        "  [bold]/join[/bold] [dim]<code>[/dim]            Join a circle using an invite code",
        "",
        "[bold cyan]Channels[/bold cyan]",
        "  [bold]/channels[/bold]               List channels in the active circle",
        "  [bold]/channel create[/bold] [dim]<name>[/dim] [dim][public|key|invite][/dim]",
        "                          Create a channel (default access: public)",
        "  [bold]/channel switch[/bold] [dim]<name>[/dim]  Switch to another channel",
        "  [bold]/channel join[/bold] [dim]<name>[/dim]    Join a channel",
        "  [bold]/channel leave[/bold] [dim]<name>[/dim]   Leave a channel",
        "  [bold]/channel requests[/bold] [dim]<name>[/dim]",
        "                          View pending join requests (owner only)",
        "  [bold]/channel approve[/bold] [dim]<name> <node_id>[/dim]",
        "                          Approve a join request (owner only)",
        "",
        "[bold cyan]People & Messages[/bold cyan]",
        "  [bold]/who[/bold] [dim][channel][/dim]           Show members of a channel",
        "  [bold]/inbox[/bold] [dim][--mentions|-m] [N][/dim]",
        "                          Recent messages (across all circles/channels).",
        "                          --mentions / -m  →  only show @mentions of you.",
        "                          N                →  how many to show (default 20).",
        "",
        "[bold cyan]Keyboard shortcuts[/bold cyan]",
        "  [bold]F1[/bold]         Open this help screen",
        "  [bold]F2[/bold]         Show invite code modal",
        "  [bold]F3[/bold]         Open settings (display name, relay URL)",
        "  [bold]Ctrl+Q[/bold]     Quit",
        "  [bold]Escape[/bold]     Focus the message input",
        "  [bold]Tab[/bold]        Accept @mention autocomplete suggestion",
        "",
        "[dim]Tip: @mention a peer by typing @ and at least 2 characters — Tab to complete.[/dim]",
    ]

    CHANNEL_HELP = [
        "[bold]Channel commands[/bold]",
        "",
        "  [bold]/channel create[/bold] [dim]<name> [public|key|invite][/dim]",
        "      Create a channel.  Access modes:",
        "        [bold]public[/bold]  — anyone in the circle can join automatically",
        "        [bold]key[/bold]     — requires a shared passphrase (not yet enforced)",
        "        [bold]invite[/bold]  — owner must approve each join request",
        "",
        "  [bold]/channel switch[/bold] [dim]<name>[/dim]",
        "      Switch the active channel (also clickable in the sidebar).",
        "",
        "  [bold]/channel join[/bold] [dim]<name>[/dim]",
        "      Join a channel and start receiving its messages.",
        "",
        "  [bold]/channel leave[/bold] [dim]<name>[/dim]",
        "      Leave a channel (you cannot leave #general).",
        "",
        "  [bold]/channel requests[/bold] [dim]<name>[/dim]",
        "      List users waiting to join an invite-only channel.",
        "      Only available to the channel owner.",
        "",
        "  [bold]/channel approve[/bold] [dim]<name> <node_id_prefix>[/dim]",
        "      Approve a pending join request.  You only need the first few",
        "      characters of the node ID shown by /channel requests.",
    ]

    topic = (topic or "").strip().lstrip("/").lower()

    if topic in ("channel", "channels"):
        return CHANNEL_HELP
    return FULL


class CommandsMixin:
    """Slash-command handlers mixed into ChatScreen.

    ``self`` is always a fully-initialised ``ChatScreen`` at runtime.
    Helper methods called here (_log_system, _refresh_sidebar, etc.) are
    defined in ChatScreen; Python's MRO resolves them transparently.
    """

    # ── Circle-name gossip ────────────────────────────────────────────────────

    def _gossip_circle_name(self, circle_id: str, name: str) -> None:
        """Queue a CIRCLE_NAME_EVT so peers learn the circle's friendly name."""
        msg = make_circle_name_message(self.state, circle_id, name)
        if msg:
            self.state.messages[msg.msg_id] = msg
            self._seen.add(msg.msg_id)

    # ── Top-level command dispatcher ──────────────────────────────────────────

    async def _handle_command(self, text: str) -> None:
        parts = text.split()
        cmd = parts[0].lower()

        if cmd == "/help":
            await self._cmd_help(parts)

        elif cmd == "/quit":
            await self.app.action_quit()

        elif cmd == "/circles":
            for cid in sorted(self.state.circles.keys()):
                count = len(self.state.circle_members.get(cid, set()))
                active = " <" if cid == self._current_circle_id else ""
                self._log_system(f"  {self._circle_label(cid)} ({count} members){active}")

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
            await self._cmd_join(parts)

        elif cmd == "/who":
            self._cmd_who(parts)

        elif cmd == "/inbox":
            self._cmd_inbox(parts)

        elif cmd == "/name":
            await self._cmd_name(parts)

        elif cmd == "/settings":
            await self.action_show_settings()

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

    # ── Individual command implementations ────────────────────────────────────

    async def _cmd_help(self, parts: list) -> None:
        topic = parts[1].lstrip("/").lower() if len(parts) > 1 else ""
        lines = _help_lines(topic)
        title = f"felundchat — /help {topic}" if topic else "felundchat — commands"
        await self.app.push_screen(HelpModal(lines, title=title))

    async def _cmd_join(self, parts: list) -> None:
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

    def _cmd_who(self, parts: list) -> None:
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

    def _cmd_inbox(self, parts: list) -> None:
        """Show recent messages across all circles, optionally filtered to @mentions."""
        import time as _time

        mentions_only = "--mentions" in parts or "-m" in parts
        limit = 20
        for p in parts[1:]:
            if p.isdigit():
                limit = int(p)

        my_names = self._my_names()

        all_msgs = sorted(
            (
                m for m in self.state.messages.values()
                if m.channel_id != "__control"
                and (not mentions_only or mentions_me(m.text, my_names))
            ),
            key=lambda m: (m.created_ts, m.msg_id),
        )[-limit:]

        if not all_msgs:
            label = "@mentions" if mentions_only else "messages"
            self._log_system(f"No {label} found.")
            return

        label = f"Last {len(all_msgs)} @mentions" if mentions_only else f"Last {len(all_msgs)} messages"
        self._log_raw(f"[bold]─── {label} ─────────────────────────────────[/bold]")
        for m in all_msgs:
            ts = _time.strftime("%m/%d %H:%M", _time.localtime(m.created_ts))
            live_name = self.state.node_display_names.get(m.author_node_id, "")
            author = live_name or m.display_name or m.author_node_id[:8]
            circle = self.state.circles.get(m.circle_id)
            circle_label = (circle.name if circle and circle.name else m.circle_id[:8])
            body, _ = _render_text_with_mentions(m.text, my_names)
            self._log_raw(
                f"[dim]{ts}[/dim] [dim cyan]{circle_label}[/dim cyan]"
                f"[dim]/#[/dim][dim cyan]{m.channel_id}[/dim cyan]"
                f"  [bold]{author}[/bold]: {body}"
            )

    async def _cmd_name(self, parts: list) -> None:
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

    # ── /circle sub-commands ──────────────────────────────────────────────────

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
            self._current_circle_id = circle.circle_id
            self._current_channel = "general"
            self._seen = set()
            self.query_one("#message-log", RichLog).clear()
            self._refresh_sidebar()
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
                # Local import breaks the circular dep with setup_screen.
                from .setup_screen import SetupScreen
                await self.app.push_screen(SetupScreen())

        else:
            self._log_system("Usage: /circle create [name]  |  /circle name <label>  |  /circle leave")

    # ── /channel sub-commands ─────────────────────────────────────────────────

    async def _channel_cmd(self, args: list) -> None:
        if not args or not self._current_circle_id:
            self._log_system("Usage: /channel create|join|switch|leave|requests|approve <name>")
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
            if channels[ch_id].created_by != self.state.node.node_id:
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
            ch_id, prefix = args[1].lower(), args[2]
            if ch_id not in channels:
                self._log_system(f"Unknown channel #{ch_id}.")
                return
            if channels[ch_id].created_by != self.state.node.node_id:
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
