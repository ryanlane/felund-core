from __future__ import annotations

import asyncio
import contextlib
import secrets
import sys
import time
from typing import List, Optional, Set

from felundchat.channel_sync import (
    CONTROL_CHANNEL_ID,
    apply_channel_event,
    apply_circle_name_event,
    make_channel_event_message,
    parse_channel_event,
    parse_circle_name_event,
)
from felundchat.crypto import make_message_mac, sha256_hex
from felundchat.gossip import GossipNode
from felundchat.invite import is_relay_url, make_felund_code, parse_felund_code
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
from felundchat.transport import detect_local_ip, parse_hostport, public_addr_hint


HELP_SUMMARY = {
    "help": "Show all commands or details for one command",
    "circles": "List circles",
    "switch": "Switch active circle",
    "channels": "List channels in active circle",
    "channel": "Manage channels (create/join/leave/switch/requests/approve)",
    "who": "Show members in a channel",
    "inbox": "Show recent messages in active channel",
    "name": "Show or change your display name",
    "debug": "Toggle local sync debug logs",
    "quit": "Exit chat",
}


HELP_DETAILS = {
    "help": [
        "/help",
        "/help <command>",
    ],
    "circles": [
        "/circles",
    ],
    "switch": [
        "/switch",
    ],
    "channels": [
        "/channels",
    ],
    "channel": [
        "/channel create <name> [public|key|invite] [key]",
        "/channel join <name> [key]",
        "/channel leave <name>",
        "/channel switch <name>",
        "/channel requests <name>",
        "/channel approve <name> <node_id>",
    ],
    "who": [
        "/who",
        "/who <channel>",
    ],
    "name": [
        "/name",
        "/name <new_display_name>",
    ],
    "inbox": [
        "/inbox",
    ],
    "debug": [
        "/debug",
    ],
    "quit": [
        "/quit",
    ],
}


def create_circle(state: State) -> Circle:
    secret = secrets.token_bytes(32)
    secret_hex = secret.hex()
    circle_id = sha256_hex(secret)[:24]
    circle = Circle(circle_id=circle_id, secret_hex=secret_hex)
    state.circles[circle_id] = circle
    state.circle_members.setdefault(circle_id, set()).add(state.node.node_id)
    ensure_default_channel(state, circle_id)
    return circle


def normalize_channel_id(raw: str) -> Optional[str]:
    channel = raw.strip().lower()
    if channel.startswith("#"):
        channel = channel[1:]
    if not channel or len(channel) > 32:
        return None
    if channel.startswith("__"):
        return None
    if not all(c.isalnum() or c in {"-", "_"} for c in channel):
        return None
    return channel


def ensure_default_channel(state: State, circle_id: str) -> None:
    state.channels.setdefault(circle_id, {})
    state.channel_members.setdefault(circle_id, {})
    state.channel_requests.setdefault(circle_id, {})

    channels = state.channels[circle_id]
    if "general" not in channels:
        channels["general"] = Channel(
            channel_id="general",
            circle_id=circle_id,
            created_by=state.node.node_id,
            created_ts=now_ts(),
            access_mode="public",
        )

    members = state.channel_members[circle_id].setdefault("general", set())
    members.add(state.node.node_id)
    state.channel_requests[circle_id].setdefault("general", set())


def get_channel_ids(state: State, circle_id: str) -> List[str]:
    channels = set(state.channels.get(circle_id, {}).keys())
    for message in state.messages.values():
        if message.circle_id == circle_id and message.channel_id and not message.channel_id.startswith("__"):
            channels.add(message.channel_id)
    if not channels:
        channels.add("general")
    return sorted(channels)


def can_send_in_channel(state: State, circle_id: str, channel_id: str) -> bool:
    members = state.channel_members.get(circle_id, {}).get(channel_id)
    if not members:
        return True
    return state.node.node_id in members


def print_help(topic: Optional[str] = None) -> None:
    if not topic:
        print("Available commands:")
        for name in sorted(HELP_SUMMARY.keys()):
            print(f" /{name:<8} {HELP_SUMMARY[name]}")
        print("Use /help <command> for details.")
        return

    key = topic.strip().lower().lstrip("/")
    if key not in HELP_DETAILS:
        print(f"Unknown command '{topic}'.")
        return

    print(f"/{key} - {HELP_SUMMARY[key]}")
    for line in HELP_DETAILS[key]:
        print(f"  {line}")


def append_channel_event(state: State, circle_id: str, event: dict) -> None:
    msg = make_channel_event_message(state, circle_id, event)
    if msg:
        state.messages[msg.msg_id] = msg


def render_message(m: ChatMessage, state: Optional[State] = None) -> str:
    ts = time.strftime("%Y-%m-%d %H:%M:%S", time.localtime(m.created_ts))
    author = m.display_name or m.author_node_id[:8]
    if state is not None:
        author = state.node_display_names.get(m.author_node_id, author)
    return f"[{ts}] #{m.channel_id} {author}: {m.text}"


def choose_circle_id(state: State, preferred: Optional[str] = None) -> Optional[str]:
    circles = sorted(state.circles.keys())
    if not circles:
        return None
    if preferred and preferred in state.circles:
        return preferred
    if len(circles) == 1:
        return circles[0]

    print("Available circles:")
    for i, cid in enumerate(circles, start=1):
        print(f" {i}) {cid}")

    while True:
        raw = input("Select a circle number: ").strip()
        try:
            idx = int(raw)
            if 1 <= idx <= len(circles):
                return circles[idx - 1]
        except ValueError:
            pass
        print("Invalid selection.")


async def sync_circle_once(
    node: GossipNode,
    state: State,
    circle_id: str,
    extra_peers: Optional[List[str]] = None,
    limit: int = 5,
) -> None:
    async with node._lock:
        peer_addrs = [p.addr for p in node.known_peers_for_circle(circle_id)]
    if extra_peers:
        peer_addrs.extend(extra_peers)

    seen_addrs: Set[str] = set()
    unique_addrs: List[str] = []
    for addr in peer_addrs:
        if not addr or addr in seen_addrs:
            continue
        seen_addrs.add(addr)
        unique_addrs.append(addr)

    for addr in unique_addrs[:limit]:
        await node.connect_and_sync(addr, circle_id)


async def interactive_chat(
    node: GossipNode,
    state: State,
    selected_circle_id: Optional[str],
    bootstrap_peer: Optional[str] = None,
) -> None:
    chosen_circle_id = choose_circle_id(state, selected_circle_id)
    if not chosen_circle_id:
        print("No circles available.")
        return
    circle_id: str = chosen_circle_id
    ensure_default_channel(state, circle_id)
    current_channel = "general"

    seen: Set[str] = set()

    def prompt_text() -> str:
        assert circle_id is not None
        return f"[{circle_id[:8]} #{current_channel}] > "

    def redraw_prompt() -> None:
        print(prompt_text(), end="", flush=True)

    async def watch_incoming() -> None:
        while not node._stop_event.is_set():
            async with node._lock:
                msgs = [
                    m for m in state.messages.values()
                    if m.circle_id == circle_id and m.channel_id == current_channel
                ]
                msgs.sort(key=lambda m: (m.created_ts, m.msg_id))
            for m in msgs:
                if m.msg_id in seen:
                    continue
                seen.add(m.msg_id)
                if m.channel_id == CONTROL_CHANNEL_ID:
                    event = parse_channel_event(m.text)
                    if event:
                        assert circle_id is not None
                        async with node._lock:
                            apply_channel_event(state, circle_id, event)
                            save_state(state)
                    else:
                        name_event = parse_circle_name_event(m.text)
                        if name_event:
                            assert circle_id is not None
                            async with node._lock:
                                apply_circle_name_event(state, circle_id, name_event)
                                save_state(state)
                    continue
                sys.stdout.write("\r")
                print(render_message(m, state))
                redraw_prompt()
            await asyncio.sleep(1)

    watcher = asyncio.create_task(watch_incoming())
    print("Chat ready. Type messages and press Enter.")
    print("Commands: /help, /switch, /circles, /channels, /channel ..., /who, /name, /inbox, /debug, /quit")

    try:
        while True:
            redraw_prompt()
            raw = await asyncio.to_thread(input)
            text = raw.strip()
            if not text:
                continue
            if text == "/quit":
                return
            if text == "/circles":
                for cid in sorted(state.circles.keys()):
                    print(f" - {cid}")
                continue
            if text == "/channels":
                ensure_default_channel(state, circle_id)
                channel_ids = get_channel_ids(state, circle_id)
                members_map = state.channel_members.get(circle_id, {})
                channel_map = state.channels.get(circle_id, {})
                for channel_id in channel_ids:
                    meta = channel_map.get(channel_id)
                    access_mode = meta.access_mode if meta else "public"
                    is_member = state.node.node_id in members_map.get(channel_id, set()) if channel_id in members_map else True
                    marker = "*" if channel_id == current_channel else " "
                    member_mark = "member" if is_member else "no-access"
                    print(f"{marker} #{channel_id} [{access_mode}] ({member_mark})")
                continue
            if text.startswith("/help"):
                parts = text.split(maxsplit=1)
                print_help(parts[1] if len(parts) > 1 else None)
                continue
            if text.startswith("/name"):
                parts = text.split(maxsplit=1)
                if len(parts) == 1:
                    print(f"Current display name: {state.node.display_name}")
                    continue
                new_name = parts[1].strip()
                if not new_name:
                    print("Display name cannot be empty.")
                    continue
                if len(new_name) > 40:
                    print("Display name must be 40 characters or fewer.")
                    continue

                state.node.display_name = new_name
                state.node_display_names[state.node.node_id] = new_name

                for cid in list(state.circles.keys()):
                    append_channel_event(
                        state,
                        cid,
                        {
                            "t": "CHANNEL_EVT",
                            "op": "rename",
                            "circle_id": cid,
                            "node_id": state.node.node_id,
                            "display_name": new_name,
                            "actor_node_id": state.node.node_id,
                            "created_ts": now_ts(),
                        },
                    )

                async with node._lock:
                    save_state(state)

                await sync_circle_once(
                    node,
                    state,
                    circle_id,
                    extra_peers=[bootstrap_peer] if bootstrap_peer else None,
                )
                print(f"Display name updated to: {new_name}")
                continue
            if text.startswith("/who"):
                parts = text.split(maxsplit=1)
                target_channel = current_channel
                if len(parts) > 1:
                    parsed = normalize_channel_id(parts[1])
                    if not parsed:
                        print("Invalid channel name.")
                        continue
                    target_channel = parsed

                ensure_default_channel(state, circle_id)
                channel_map = state.channels.get(circle_id, {})
                members_map = state.channel_members.get(circle_id, {})
                requests_map = state.channel_requests.get(circle_id, {})

                channel_meta = channel_map.get(target_channel)
                if not channel_meta and target_channel not in get_channel_ids(state, circle_id):
                    print(f"Unknown channel #{target_channel}.")
                    continue

                members = sorted(members_map.get(target_channel, set()))
                if not members:
                    print(f"#{target_channel} members: none known")
                else:
                    print(f"#{target_channel} members ({len(members)}):")
                    for node_id in members:
                        peer = state.peers.get(node_id)
                        display_name = state.node_display_names.get(node_id, "")
                        name_part = f" ({display_name})" if display_name else ""
                        if node_id == state.node.node_id:
                            print(f" - {node_id}{name_part} (you)")
                        elif peer:
                            print(f" - {node_id}{name_part} @ {peer.addr}")
                        else:
                            print(f" - {node_id}{name_part}")

                if channel_meta and channel_meta.created_by == state.node.node_id:
                    pending = sorted(requests_map.get(target_channel, set()))
                    if pending:
                        print(f"#{target_channel} pending requests ({len(pending)}):")
                        for node_id in pending:
                            print(f" - {node_id}")
                continue
            if text == "/switch":
                next_cid = choose_circle_id(state)
                if next_cid:
                    circle_id = next_cid
                    ensure_default_channel(state, circle_id)
                    current_channel = "general"
                    seen = set()
                continue
            if text.startswith("/channel"):
                parts = text.split()
                if len(parts) < 2:
                    print_help("channel")
                    continue
                sub = parts[1].lower()
                ensure_default_channel(state, circle_id)
                channels = state.channels.setdefault(circle_id, {})
                members_map = state.channel_members.setdefault(circle_id, {})
                requests_map = state.channel_requests.setdefault(circle_id, {})

                if sub == "create":
                    if len(parts) < 3:
                        print("Usage: /channel create <name> [public|key|invite] [key]")
                        continue
                    channel_id = normalize_channel_id(parts[2])
                    if not channel_id:
                        print("Invalid channel name. Use letters, numbers, -, _. Max 32 chars.")
                        continue
                    if channel_id in channels:
                        print(f"Channel #{channel_id} already exists.")
                        continue
                    access_mode = "public"
                    key_hash = ""
                    if len(parts) >= 4:
                        access_mode = parts[3].lower()
                    if access_mode not in {"public", "key", "invite"}:
                        print("Access mode must be one of: public, key, invite")
                        continue
                    if access_mode == "key":
                        if len(parts) < 5:
                            print("Usage: /channel create <name> key <channel_key>")
                            continue
                        key_material = f"{circle_id}|{channel_id}|{parts[4]}".encode("utf-8")
                        key_hash = sha256_hex(key_material)

                    channels[channel_id] = Channel(
                        channel_id=channel_id,
                        circle_id=circle_id,
                        created_by=state.node.node_id,
                        created_ts=now_ts(),
                        access_mode=access_mode,
                        key_hash=key_hash,
                    )
                    members_map.setdefault(channel_id, set()).add(state.node.node_id)
                    requests_map.setdefault(channel_id, set())
                    event = {
                        "t": "CHANNEL_EVT",
                        "op": "create",
                        "circle_id": circle_id,
                        "channel_id": channel_id,
                        "access_mode": access_mode,
                        "key_hash": key_hash,
                        "actor_node_id": state.node.node_id,
                        "created_by": state.node.node_id,
                        "created_ts": now_ts(),
                    }
                    append_channel_event(state, circle_id, event)
                    async with node._lock:
                        save_state(state)
                    print(f"Created #{channel_id} [{access_mode}].")
                    continue

                if sub in {"join", "switch", "leave", "requests", "approve"} and len(parts) < 3:
                    print("Usage: /channel <join|switch|leave|requests> <name>")
                    continue

                channel_id = normalize_channel_id(parts[2]) if len(parts) >= 3 else None
                if not channel_id:
                    print("Invalid channel name.")
                    continue

                if sub == "join":
                    channel = channels.get(channel_id)
                    if not channel:
                        print(f"Unknown channel #{channel_id}.")
                        continue
                    members = members_map.setdefault(channel_id, set())
                    requests = requests_map.setdefault(channel_id, set())
                    if state.node.node_id in members:
                        print(f"Already in #{channel_id}.")
                        continue
                    if channel.access_mode == "public":
                        members.add(state.node.node_id)
                        append_channel_event(
                            state,
                            circle_id,
                            {
                                "t": "CHANNEL_EVT",
                                "op": "join",
                                "circle_id": circle_id,
                                "channel_id": channel_id,
                                "node_id": state.node.node_id,
                                "actor_node_id": state.node.node_id,
                                "created_ts": now_ts(),
                            },
                        )
                        async with node._lock:
                            save_state(state)
                        print(f"Joined #{channel_id}.")
                        continue
                    if channel.access_mode == "key":
                        if len(parts) < 4:
                            print("Usage: /channel join <name> <channel_key>")
                            continue
                        key_material = f"{circle_id}|{channel_id}|{parts[3]}".encode("utf-8")
                        if sha256_hex(key_material) != channel.key_hash:
                            print("Invalid channel key.")
                            continue
                        members.add(state.node.node_id)
                        append_channel_event(
                            state,
                            circle_id,
                            {
                                "t": "CHANNEL_EVT",
                                "op": "join",
                                "circle_id": circle_id,
                                "channel_id": channel_id,
                                "node_id": state.node.node_id,
                                "actor_node_id": state.node.node_id,
                                "created_ts": now_ts(),
                            },
                        )
                        async with node._lock:
                            save_state(state)
                        print(f"Joined #{channel_id}.")
                        continue

                    # invite mode
                    requests.add(state.node.node_id)
                    append_channel_event(
                        state,
                        circle_id,
                        {
                            "t": "CHANNEL_EVT",
                            "op": "request",
                            "circle_id": circle_id,
                            "channel_id": channel_id,
                            "node_id": state.node.node_id,
                            "actor_node_id": state.node.node_id,
                            "created_ts": now_ts(),
                        },
                    )
                    async with node._lock:
                        save_state(state)
                    print(f"Access requested for #{channel_id}. Owner must approve.")
                    continue

                if sub == "switch":
                    members = members_map.get(channel_id, set())
                    if members and state.node.node_id not in members:
                        print(f"You are not a member of #{channel_id}.")
                        continue
                    if channel_id not in channels and channel_id not in get_channel_ids(state, circle_id):
                        print(f"Unknown channel #{channel_id}.")
                        continue
                    current_channel = channel_id
                    seen = set()
                    continue

                if sub == "leave":
                    if channel_id == "general":
                        print("Cannot leave #general.")
                        continue
                    members = members_map.get(channel_id, set())
                    if state.node.node_id in members:
                        members.remove(state.node.node_id)
                        append_channel_event(
                            state,
                            circle_id,
                            {
                                "t": "CHANNEL_EVT",
                                "op": "leave",
                                "circle_id": circle_id,
                                "channel_id": channel_id,
                                "node_id": state.node.node_id,
                                "actor_node_id": state.node.node_id,
                                "created_ts": now_ts(),
                            },
                        )
                        async with node._lock:
                            save_state(state)
                    if current_channel == channel_id:
                        current_channel = "general"
                        seen = set()
                    print(f"Left #{channel_id}.")
                    continue

                if sub == "requests":
                    channel = channels.get(channel_id)
                    if not channel:
                        print(f"Unknown channel #{channel_id}.")
                        continue
                    if channel.created_by != state.node.node_id:
                        print("Only channel owner can view requests.")
                        continue
                    requests = sorted(requests_map.get(channel_id, set()))
                    if not requests:
                        print("No pending requests.")
                        continue
                    print(f"Pending requests for #{channel_id}:")
                    for node_id in requests:
                        print(f" - {node_id}")
                    continue

                if sub == "approve":
                    if len(parts) < 4:
                        print("Usage: /channel approve <name> <node_id>")
                        continue
                    target_node_id = parts[3]
                    channel = channels.get(channel_id)
                    if not channel:
                        print(f"Unknown channel #{channel_id}.")
                        continue
                    if channel.created_by != state.node.node_id:
                        print("Only channel owner can approve.")
                        continue
                    requests = requests_map.setdefault(channel_id, set())
                    members = members_map.setdefault(channel_id, set())
                    if target_node_id not in requests:
                        print("No such pending request.")
                        continue
                    requests.remove(target_node_id)
                    members.add(target_node_id)
                    append_channel_event(
                        state,
                        circle_id,
                        {
                            "t": "CHANNEL_EVT",
                            "op": "approve",
                            "circle_id": circle_id,
                            "channel_id": channel_id,
                            "target_node_id": target_node_id,
                            "actor_node_id": state.node.node_id,
                            "created_ts": now_ts(),
                        },
                    )
                    async with node._lock:
                        save_state(state)
                    print(f"Approved {target_node_id} for #{channel_id}.")
                    continue

                print_help("channel")
                continue
            if text == "/inbox":
                async with node._lock:
                    msgs = [
                        m for m in state.messages.values()
                        if m.circle_id == circle_id and m.channel_id == current_channel
                    ]
                    msgs.sort(key=lambda m: (m.created_ts, m.msg_id))
                for m in msgs[-20:]:
                    print(render_message(m, state))
                continue
            if text == "/debug":
                node.debug_sync = not node.debug_sync
                status = "on" if node.debug_sync else "off"
                print(f"Sync debug is now {status}.")
                continue

            created = now_ts()
            msg_id = sha256_hex(
                f"{state.node.node_id}|{created}|{secrets.token_hex(8)}".encode("utf-8")
            )[:32]
            msg = ChatMessage(
                msg_id=msg_id,
                circle_id=circle_id,
                channel_id=current_channel,
                author_node_id=state.node.node_id,
                display_name=state.node.display_name,
                created_ts=created,
                text=text,
            )
            if not can_send_in_channel(state, circle_id, current_channel):
                print(f"You do not have access to #{current_channel}.")
                continue
            circle = state.circles.get(circle_id)
            if not circle:
                print("Selected circle no longer exists.")
                continue
            msg.mac = make_message_mac(circle.secret_hex, msg)
            async with node._lock:
                state.messages[msg_id] = msg
                state.node_display_names[state.node.node_id] = state.node.display_name
                save_state(state)
            seen.add(msg_id)
            print(render_message(msg, state))
            await sync_circle_once(
                node, state, circle_id,
                extra_peers=[bootstrap_peer] if bootstrap_peer else None,
            )
    finally:
        node.stop()
        watcher.cancel()
        with contextlib.suppress(asyncio.CancelledError):
            await watcher


async def run_interactive_flow() -> None:
    state = load_state()

    print("Select mode:")
    print(" 1) host")
    print(" 2) client")
    mode_raw = input("Mode [1]: ").strip().lower()
    mode = "client" if mode_raw in {"2", "client", "c"} else "host"

    current_name = state.node.display_name or "anon"
    entered_name = input(f"Display name [{current_name}]: ").strip()
    state.node.display_name = entered_name or current_name
    state.node_display_names[state.node.node_id] = state.node.display_name

    local_ip = detect_local_ip()
    state.node.bind = local_ip

    default_port = state.node.port or 9999
    while True:
        port_raw = input(f"Listen port [{default_port}]: ").strip()
        if not port_raw:
            state.node.port = default_port
            break
        try:
            state.node.port = int(port_raw)
            if state.node.port <= 0 or state.node.port > 65535:
                raise ValueError
            break
        except ValueError:
            print("Enter a valid port (1-65535).")

    selected_circle_id: Optional[str] = None
    peer: Optional[str] = None

    if mode == "host":
        circle = create_circle(state)
        selected_circle_id = circle.circle_id
        save_state(state)

        bootstrap = public_addr_hint(state.node.bind, state.node.port)
        invite_code = make_felund_code(circle.secret_hex, bootstrap)
        print()
        print("Invite generated:")
        print(f" felund code: {invite_code}")
        print(" (contains both secret + peer)")
    else:
        code_or_secret = input("Paste felund code (or legacy secret): ").strip()
        try:
            if code_or_secret.startswith("felund1."):
                secret_hex, peer = parse_felund_code(code_or_secret)
            else:
                secret_hex = code_or_secret.lower()
                bytes.fromhex(secret_hex)
                peer = input("Paste bootstrap peer host:port: ").strip()
                parse_hostport(peer)
        except Exception as e:
            print(f"Invalid invite data: {e}")
            return

        secret = bytes.fromhex(secret_hex)
        circle_id = sha256_hex(secret)[:24]
        state.circles[circle_id] = Circle(circle_id=circle_id, secret_hex=secret_hex)
        state.circle_members.setdefault(circle_id, set()).add(state.node.node_id)
        selected_circle_id = circle_id
        ensure_default_channel(state, circle_id)
        save_state(state)

    for cid in list(state.circles.keys()):
        ensure_default_channel(state, cid)
    save_state(state)

    node = GossipNode(state)
    await node.start_server()
    gossip_task = asyncio.create_task(node.gossip_loop(interval_s=5))

    api_base = safe_api_base_from_env()
    rendezvous_task: Optional[asyncio.Task] = None
    if api_base:
        async def rendezvous_loop() -> None:
            while not node._stop_event.is_set():
                async with node._lock:
                    circle_ids = list(state.circles.keys())

                for circle_id in circle_ids:
                    try:
                        await asyncio.to_thread(register_presence, api_base, state, circle_id)
                        discovered = await asyncio.to_thread(lookup_peer_addrs, api_base, state, circle_id)
                        async with node._lock:
                            changed = merge_discovered_peers(state, circle_id, discovered)
                            if changed:
                                save_state(state)
                        for _, addr in discovered[:5]:
                            await node.connect_and_sync(addr, circle_id)
                    except Exception as e:
                        if node.debug_sync and not is_network_error(e):
                            print(f"[api] {circle_id}: {type(e).__name__}: {e}")

                try:
                    await asyncio.wait_for(node._stop_event.wait(), timeout=8)
                except asyncio.TimeoutError:
                    pass

        rendezvous_task = asyncio.create_task(rendezvous_loop())
        print(f"[api] rendezvous enabled: {api_base}")

    # Web-client codes carry a relay URL instead of a TCP host:port.
    # In that case skip the direct TCP bootstrap; the relay loop handles sync.
    tcp_peer = peer if peer and not is_relay_url(peer) else None

    bootstrap_task: Optional[asyncio.Task] = None
    if mode == "client" and tcp_peer:
        async def periodic_bootstrap() -> None:
            while not node._stop_event.is_set():
                await node.connect_and_sync(tcp_peer, selected_circle_id or "")
                try:
                    await asyncio.wait_for(node._stop_event.wait(), timeout=5)
                except asyncio.TimeoutError:
                    pass

        bootstrap_task = asyncio.create_task(periodic_bootstrap())

    try:
        if mode == "client" and tcp_peer:
            await node.connect_and_sync(tcp_peer, selected_circle_id or "")
        await interactive_chat(node, state, selected_circle_id, bootstrap_peer=tcp_peer)
    finally:
        node.stop()
        gossip_task.cancel()
        with contextlib.suppress(asyncio.CancelledError):
            await gossip_task
        if rendezvous_task:
            rendezvous_task.cancel()
            with contextlib.suppress(asyncio.CancelledError):
                await rendezvous_task
            for circle_id in list(state.circles.keys()):
                with contextlib.suppress(Exception):
                    await asyncio.to_thread(unregister_presence, api_base, state, circle_id)
        if bootstrap_task:
            bootstrap_task.cancel()
            with contextlib.suppress(asyncio.CancelledError):
                await bootstrap_task
        await node.stop_server()
