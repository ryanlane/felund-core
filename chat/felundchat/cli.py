from __future__ import annotations

import argparse
import asyncio
import secrets

from felundchat.chat import (
    create_circle,
    ensure_default_channel,
    render_message,
    run_interactive_flow,
)
from felundchat.crypto import make_message_mac, sha256_hex
from felundchat.gossip import GossipNode
from felundchat.invite import make_felund_code, parse_felund_code
from felundchat.models import ChatMessage, Circle, now_ts
from felundchat.persistence import load_state, save_state
from felundchat.transport import detect_local_ip, parse_hostport, public_addr_hint


def cmd_init(args: argparse.Namespace) -> None:
    state = load_state()
    state.node.bind = args.bind
    state.node.port = args.port
    if args.name:
        state.node.display_name = args.name.strip()
    if not state.node.node_id:
        state.node.node_id = sha256_hex(secrets.token_bytes(32))[:24]
    save_state(state)
    print("Initialized.")
    print(f" node_id: {state.node.node_id}")
    print(f" listen : {public_addr_hint(state.node.bind, state.node.port)}")
    from felundchat.config import STATE_FILE
    print(f" state  : {STATE_FILE}")


def cmd_invite(args: argparse.Namespace) -> None:
    state = load_state()
    circle = create_circle(state)
    save_state(state)

    bootstrap = public_addr_hint(state.node.bind, state.node.port)
    invite_code = make_felund_code(circle.secret_hex, bootstrap)
    print("Circle created.")
    print(f" circle_id   : {circle.circle_id}")
    print(f" circle_secret (hex): {circle.secret_hex}")
    print(f" felund_code : {invite_code}")
    print()
    print("Share this join command with a friend:")
    print(f"  python felundchat.py join --code {invite_code}")
    print("  (or legacy: --secret ... --peer ...)")
    print()
    print("Then run your node:")
    print("  python felundchat.py run")


def cmd_join(args: argparse.Namespace) -> None:
    state = load_state()
    try:
        if args.code:
            secret_hex, peer_addr = parse_felund_code(args.code)
        else:
            if not args.secret or not args.peer:
                print("Provide --code, or both --secret and --peer.")
                return
            secret_hex = args.secret.strip().lower()
            peer_addr = args.peer.strip()
            bytes.fromhex(secret_hex)
            parse_hostport(peer_addr)
    except Exception as e:
        print(f"Invalid join input: {e}")
        return

    secret = bytes.fromhex(secret_hex)
    circle_id = sha256_hex(secret)[:24]
    state.circles[circle_id] = Circle(circle_id=circle_id, secret_hex=secret_hex)
    state.circle_members.setdefault(circle_id, set()).add(state.node.node_id)
    ensure_default_channel(state, circle_id)
    save_state(state)

    print(f"Joined circle {circle_id}. Bootstrapping via {peer_addr} ...")
    node = GossipNode(state)

    async def _bootstrap() -> None:
        await node.connect_and_sync(peer_addr, circle_id)

    asyncio.run(_bootstrap())
    print("Bootstrap attempted. Now run:")
    print("  python felundchat.py run")


def cmd_peers(args: argparse.Namespace) -> None:
    state = load_state()
    if args.circle_id:
        cid = args.circle_id
        if cid not in state.circles:
            print("Unknown circle_id")
            return
        members = state.circle_members.get(cid, set())
        print(f"Peers for circle {cid}:")
        for pid in sorted(members):
            p = state.peers.get(pid)
            if p:
                print(f" - {pid} @ {p.addr} (last_seen={p.last_seen})")
            else:
                print(f" - {pid} (no addr yet)")
    else:
        print("Circles:")
        for cid in sorted(state.circles.keys()):
            print(f" - {cid} (members={len(state.circle_members.get(cid, set()))})")


def cmd_send(args: argparse.Namespace) -> None:
    state = load_state()
    cid = args.circle_id
    if cid not in state.circles:
        print("Unknown circle_id. Use `invite` or `join` first.")
        return

    text = args.text
    channel_id = args.channel
    ensure_default_channel(state, cid)
    if channel_id not in state.channels.get(cid, {}):
        print(f"Unknown channel #{channel_id}.")
        return

    created = now_ts()
    msg_id = sha256_hex(
        f"{state.node.node_id}|{created}|{secrets.token_hex(8)}".encode("utf-8")
    )[:32]
    msg = ChatMessage(
        msg_id=msg_id,
        circle_id=cid,
        channel_id=channel_id,
        author_node_id=state.node.node_id,
        display_name=state.node.display_name,
        created_ts=created,
        text=text,
    )
    msg.mac = make_message_mac(state.circles[cid].secret_hex, msg)
    state.messages[msg_id] = msg
    save_state(state)
    print(f"Queued message {msg_id}. It will gossip out while `run` is active.")


def cmd_run(args: argparse.Namespace) -> None:
    state = load_state()
    node = GossipNode(state)

    async def main() -> None:
        await node.start_server()
        try:
            if args.peer and args.circle_id:
                await node.connect_and_sync(args.peer, args.circle_id)
            await node.gossip_loop(interval_s=args.interval)
        finally:
            node.stop()
            await node.stop_server()

    try:
        asyncio.run(main())
    except KeyboardInterrupt:
        pass


def cmd_inbox(args: argparse.Namespace) -> None:
    state = load_state()
    cid = args.circle_id
    if cid not in state.circles:
        print("Unknown circle_id")
        return
    channel_id = args.channel
    msgs = [m for m in state.messages.values() if m.circle_id == cid and m.channel_id == channel_id]
    msgs.sort(key=lambda m: (m.created_ts, m.msg_id))
    for m in msgs[-args.limit:]:
        print(render_message(m))


def build_parser() -> argparse.ArgumentParser:
    p = argparse.ArgumentParser(prog="felundchat", description="Simple gossip + direct connect chat")
    sub = p.add_subparsers(dest="cmd", required=False)

    sp = sub.add_parser("init", help="Initialize local node")
    sp.add_argument("--bind", default=detect_local_ip(), help="Bind address (default: detected local IP)")
    sp.add_argument("--port", type=int, default=9999, help="Listen port")
    sp.add_argument("--name", help="Display name")
    sp.set_defaults(func=cmd_init)

    sp = sub.add_parser("interactive", help="Run guided host/client setup + chat")
    sp.set_defaults(func=None)

    sp = sub.add_parser("invite", help="Create a new circle and print an invite")
    sp.set_defaults(func=cmd_invite)

    sp = sub.add_parser("join", help="Join a circle using a felund code or secret + bootstrap peer")
    sp.add_argument("--code", help="Single felund invite code")
    sp.add_argument("--secret", help="Circle secret hex (shared)")
    sp.add_argument("--peer", help="Bootstrap peer host:port")
    sp.set_defaults(func=cmd_join)

    sp = sub.add_parser("run", help="Run server + gossip loop")
    sp.add_argument("--interval", type=int, default=5, help="Gossip interval seconds")
    sp.add_argument("--circle-id", help="Optional: circle_id to immediately sync")
    sp.add_argument("--peer", help="Optional: peer host:port to immediately sync")
    sp.set_defaults(func=cmd_run)

    sp = sub.add_parser("send", help="Broadcast a text message to a circle")
    sp.add_argument("--circle-id", required=True, help="Circle id")
    sp.add_argument("--channel", default="general", help="Channel id (default: general)")
    sp.add_argument("text", help="Message text")
    sp.set_defaults(func=cmd_send)

    sp = sub.add_parser("inbox", help="Show recent messages for a circle")
    sp.add_argument("--circle-id", required=True)
    sp.add_argument("--channel", default="general")
    sp.add_argument("--limit", type=int, default=50)
    sp.set_defaults(func=cmd_inbox)

    sp = sub.add_parser("peers", help="List circles or peers for a circle")
    sp.add_argument("--circle-id", help="Circle id")
    sp.set_defaults(func=cmd_peers)

    sp = sub.add_parser("tui", help="Launch the Textual panel-based TUI (default)")
    sp.set_defaults(func=cmd_tui)

    return p


def cmd_tui(args: argparse.Namespace) -> None:
    from felundchat.tui import FelundApp
    FelundApp().run()


def main() -> None:
    parser = build_parser()
    args = parser.parse_args()
    if not getattr(args, "cmd", None) or args.cmd in {"interactive", "tui"}:
        from felundchat.tui import FelundApp
        FelundApp().run()
        return
    args.func(args)
