from __future__ import annotations

import asyncio
import contextlib
import secrets
import sys
import time
from typing import List, Optional, Set

from felundchat.crypto import make_message_mac, sha256_hex
from felundchat.gossip import GossipNode
from felundchat.invite import make_felund_code, parse_felund_code
from felundchat.models import ChatMessage, Circle, State, now_ts
from felundchat.persistence import load_state, save_state
from felundchat.transport import detect_local_ip, parse_hostport, public_addr_hint


def create_circle(state: State) -> Circle:
    secret = secrets.token_bytes(32)
    secret_hex = secret.hex()
    circle_id = sha256_hex(secret)[:24]
    circle = Circle(circle_id=circle_id, secret_hex=secret_hex)
    state.circles[circle_id] = circle
    state.circle_members.setdefault(circle_id, set()).add(state.node.node_id)
    return circle


def render_message(m: ChatMessage) -> str:
    ts = time.strftime("%Y-%m-%d %H:%M:%S", time.localtime(m.created_ts))
    author = m.display_name or m.author_node_id[:8]
    return f"[{ts}] {author}: {m.text}"


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
    circle_id = choose_circle_id(state, selected_circle_id)
    if not circle_id:
        print("No circles available.")
        return

    seen: Set[str] = set()

    def prompt_text() -> str:
        return f"[{circle_id[:8]}] > "

    def redraw_prompt() -> None:
        print(prompt_text(), end="", flush=True)

    async def watch_incoming() -> None:
        while not node._stop_event.is_set():
            async with node._lock:
                msgs = [m for m in state.messages.values() if m.circle_id == circle_id]
                msgs.sort(key=lambda m: (m.created_ts, m.msg_id))
            for m in msgs:
                if m.msg_id in seen:
                    continue
                seen.add(m.msg_id)
                sys.stdout.write("\r")
                print(render_message(m))
                redraw_prompt()
            await asyncio.sleep(1)

    watcher = asyncio.create_task(watch_incoming())
    print("Chat ready. Type messages and press Enter.")
    print("Commands: /switch, /circles, /inbox, /debug, /quit")

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
            if text == "/switch":
                next_cid = choose_circle_id(state)
                if next_cid:
                    circle_id = next_cid
                    seen = set()
                continue
            if text == "/inbox":
                async with node._lock:
                    msgs = [m for m in state.messages.values() if m.circle_id == circle_id]
                    msgs.sort(key=lambda m: (m.created_ts, m.msg_id))
                for m in msgs[-20:]:
                    print(render_message(m))
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
                author_node_id=state.node.node_id,
                display_name=state.node.display_name,
                created_ts=created,
                text=text,
            )
            circle = state.circles.get(circle_id)
            if not circle:
                print("Selected circle no longer exists.")
                continue
            msg.mac = make_message_mac(circle.secret_hex, msg)
            async with node._lock:
                state.messages[msg_id] = msg
                save_state(state)
            seen.add(msg_id)
            print(render_message(msg))
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
        save_state(state)

    node = GossipNode(state)
    await node.start_server()
    gossip_task = asyncio.create_task(node.gossip_loop(interval_s=5))

    bootstrap_task: Optional[asyncio.Task] = None
    if mode == "client" and peer:
        async def periodic_bootstrap() -> None:
            while not node._stop_event.is_set():
                await node.connect_and_sync(peer, selected_circle_id or "")
                try:
                    await asyncio.wait_for(node._stop_event.wait(), timeout=5)
                except asyncio.TimeoutError:
                    pass

        bootstrap_task = asyncio.create_task(periodic_bootstrap())

    try:
        if mode == "client" and peer:
            await node.connect_and_sync(peer, selected_circle_id or "")
        await interactive_chat(node, state, selected_circle_id, bootstrap_peer=peer)
    finally:
        node.stop()
        gossip_task.cancel()
        with contextlib.suppress(asyncio.CancelledError):
            await gossip_task
        if bootstrap_task:
            bootstrap_task.cancel()
            with contextlib.suppress(asyncio.CancelledError):
                await bootstrap_task
        await node.stop_server()
