#!/usr/bin/env python3
"""
felundchat.py — simple gossip + direct-connect group chat (CLI)

Design goals:
- No central server
- Small private circle via shared secret (invite)
- Gossip peer list + message store-and-forward
- Works on LAN/VPN or with reachable ports

NOT included (yet):
- NAT traversal (STUN/TURN/ICE)
- True end-to-end encryption & group key rotation
- Anti-spam / Sybil resistance / anonymity
"""

from __future__ import annotations

import argparse
import asyncio
import dataclasses
import hashlib
import hmac
import json
import os
import secrets
import socket
import time
from pathlib import Path
from typing import Any, Dict, List, Optional, Set, Tuple


APP_DIR = Path.home() / ".felundchat"
STATE_FILE = APP_DIR / "state.json"
MSG_MAX = 16_384  # bytes per frame, keep it small


def now_ts() -> int:
    return int(time.time())


def sha256_hex(b: bytes) -> str:
    return hashlib.sha256(b).hexdigest()


def hmac_hex(key: bytes, msg: bytes) -> str:
    return hmac.new(key, msg, hashlib.sha256).hexdigest()


def parse_hostport(s: str) -> Tuple[str, int]:
    if ":" not in s:
        raise ValueError("Expected host:port")
    host, port_s = s.rsplit(":", 1)
    return host, int(port_s)


def canonical_peer_addr(host: str, port: int) -> str:
    return f"{host}:{port}"


@dataclasses.dataclass
class NodeConfig:
    node_id: str
    bind: str
    port: int


@dataclasses.dataclass
class Circle:
    circle_id: str
    secret_hex: str  # shared secret in hex


@dataclasses.dataclass
class Peer:
    node_id: str
    addr: str  # host:port
    last_seen: int


@dataclasses.dataclass
class ChatMessage:
    msg_id: str
    circle_id: str
    author_node_id: str
    created_ts: int
    text: str


@dataclasses.dataclass
class State:
    node: NodeConfig
    circles: Dict[str, Circle]                # circle_id -> Circle
    peers: Dict[str, Peer]                    # peer_node_id -> Peer
    circle_members: Dict[str, Set[str]]       # circle_id -> set(peer_node_id)
    messages: Dict[str, ChatMessage]          # msg_id -> ChatMessage

    @staticmethod
    def default(bind: str, port: int) -> "State":
        node_id = sha256_hex(secrets.token_bytes(32))[:24]
        return State(
            node=NodeConfig(node_id=node_id, bind=bind, port=port),
            circles={},
            peers={},
            circle_members={},
            messages={},
        )


def ensure_app_dir() -> None:
    APP_DIR.mkdir(parents=True, exist_ok=True)


def load_state() -> State:
    ensure_app_dir()
    if not STATE_FILE.exists():
        # Default bind/port are placeholders until init
        return State.default(bind="0.0.0.0", port=9999)

    data = json.loads(STATE_FILE.read_text(encoding="utf-8"))
    node = NodeConfig(**data["node"])

    circles = {cid: Circle(**c) for cid, c in data.get("circles", {}).items()}
    peers = {pid: Peer(**p) for pid, p in data.get("peers", {}).items()}
    circle_members = {cid: set(v) for cid, v in data.get("circle_members", {}).items()}
    messages = {}
    for mid, m in data.get("messages", {}).items():
        messages[mid] = ChatMessage(**m)

    return State(node=node, circles=circles, peers=peers, circle_members=circle_members, messages=messages)


def save_state(state: State) -> None:
    ensure_app_dir()
    data = {
        "node": dataclasses.asdict(state.node),
        "circles": {cid: dataclasses.asdict(c) for cid, c in state.circles.items()},
        "peers": {pid: dataclasses.asdict(p) for pid, p in state.peers.items()},
        "circle_members": {cid: sorted(list(v)) for cid, v in state.circle_members.items()},
        "messages": {mid: dataclasses.asdict(m) for mid, m in state.messages.items()},
    }
    tmp = STATE_FILE.with_suffix(".tmp")
    tmp.write_text(json.dumps(data, indent=2, sort_keys=True), encoding="utf-8")
    tmp.replace(STATE_FILE)


# ---------------------------
# Protocol (JSON lines)
# ---------------------------

# Client -> Server: HELLO
# {
#   "t": "HELLO",
#   "node_id": "...",
#   "circle_id": "...",
#   "token": "..."  (HMAC(secret, node_id||circle_id))
#   "listen_addr": "host:port"  (optional hint)
# }
#
# Server -> Client: WELCOME / ERROR
#
# Then:
# Client <-> Server exchange:
#  - PEERS: list of peers known for this circle
#  - MSGS_HAVE: list of message ids for this circle
#  - MSGS_SEND: full message objects for requested ids


def make_token(secret_hex: str, node_id: str, circle_id: str) -> str:
    secret = bytes.fromhex(secret_hex)
    payload = f"{node_id}|{circle_id}".encode("utf-8")
    return hmac_hex(secret, payload)


def verify_token(secret_hex: str, node_id: str, circle_id: str, token: str) -> bool:
    return hmac.compare_digest(make_token(secret_hex, node_id, circle_id), token)


async def write_frame(writer: asyncio.StreamWriter, obj: Dict[str, Any]) -> None:
    raw = (json.dumps(obj, separators=(",", ":")) + "\n").encode("utf-8")
    if len(raw) > MSG_MAX:
        raise ValueError("Frame too large")
    writer.write(raw)
    await writer.drain()


async def read_frame(reader: asyncio.StreamReader) -> Dict[str, Any]:
    line = await reader.readline()
    if not line:
        raise EOFError
    if len(line) > MSG_MAX:
        raise ValueError("Frame too large")
    return json.loads(line.decode("utf-8"))


def public_addr_hint(bind: str, port: int) -> str:
    # Best-effort: try to guess a LAN IP for convenience in local testing.
    # Not reliable in multi-NIC setups.
    host = bind
    if bind == "0.0.0.0":
        try:
            s = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
            s.connect(("8.8.8.8", 80))
            host = s.getsockname()[0]
            s.close()
        except Exception:
            host = "127.0.0.1"
    return canonical_peer_addr(host, port)


# ---------------------------
# Gossip Engine
# ---------------------------

class GossipNode:
    def __init__(self, state: State):
        self.state = state
        self._lock = asyncio.Lock()
        self._server: Optional[asyncio.AbstractServer] = None

    def circles_list(self) -> List[str]:
        return sorted(self.state.circles.keys())

    def known_peers_for_circle(self, circle_id: str) -> List[Peer]:
        member_ids = self.state.circle_members.get(circle_id, set())
        peers = [self.state.peers[pid] for pid in member_ids if pid in self.state.peers]
        return sorted(peers, key=lambda p: p.last_seen, reverse=True)

    def message_ids_for_circle(self, circle_id: str) -> List[str]:
        return sorted([mid for mid, m in self.state.messages.items() if m.circle_id == circle_id])

    def messages_for_circle(self, circle_id: str) -> List[ChatMessage]:
        msgs = [m for m in self.state.messages.values() if m.circle_id == circle_id]
        return sorted(msgs, key=lambda m: (m.created_ts, m.msg_id))

    async def start_server(self) -> None:
        self._server = await asyncio.start_server(self._handle_conn, self.state.node.bind, self.state.node.port)
        addrs = ", ".join(str(sock.getsockname()) for sock in (self._server.sockets or []))
        print(f"[server] listening on {addrs}")

    async def stop_server(self) -> None:
        if self._server:
            self._server.close()
            await self._server.wait_closed()
            self._server = None

    async def _handle_conn(self, reader: asyncio.StreamReader, writer: asyncio.StreamWriter) -> None:
        peername = writer.get_extra_info("peername")
        try:
            hello = await read_frame(reader)
            if hello.get("t") != "HELLO":
                await write_frame(writer, {"t": "ERROR", "err": "Expected HELLO"})
                return

            peer_node_id = str(hello.get("node_id", ""))
            circle_id = str(hello.get("circle_id", ""))
            token = str(hello.get("token", ""))
            listen_addr = str(hello.get("listen_addr", "")) if hello.get("listen_addr") else ""

            async with self._lock:
                circle = self.state.circles.get(circle_id)
                if not circle:
                    await write_frame(writer, {"t": "ERROR", "err": "Unknown circle_id"})
                    return
                if not verify_token(circle.secret_hex, peer_node_id, circle_id, token):
                    await write_frame(writer, {"t": "ERROR", "err": "Auth failed"})
                    return

                # Accept/update peer info
                if listen_addr:
                    self.state.peers[peer_node_id] = Peer(node_id=peer_node_id, addr=listen_addr, last_seen=now_ts())
                else:
                    # fallback to remote IP: ephemeral port is useless, but keep last_seen
                    if peer_node_id in self.state.peers:
                        self.state.peers[peer_node_id].last_seen = now_ts()

                self.state.circle_members.setdefault(circle_id, set()).add(peer_node_id)
                save_state(self.state)

            await write_frame(writer, {"t": "WELCOME", "node_id": self.state.node.node_id})

            # Sync dance: exchange peers + messages inventory
            await self._sync_with_connected_peer(reader, writer, circle_id)

        except EOFError:
            return
        except Exception as e:
            try:
                await write_frame(writer, {"t": "ERROR", "err": f"{type(e).__name__}: {e}"})
            except Exception:
                pass
        finally:
            try:
                writer.close()
                await writer.wait_closed()
            except Exception:
                pass

    async def _sync_with_connected_peer(self, reader: asyncio.StreamReader, writer: asyncio.StreamWriter, circle_id: str) -> None:
        # 1) Send our peer list + message ids
        async with self._lock:
            peers = [{"node_id": p.node_id, "addr": p.addr, "last_seen": p.last_seen}
                     for p in self.known_peers_for_circle(circle_id)]
            mids = self.message_ids_for_circle(circle_id)

        await write_frame(writer, {"t": "PEERS", "circle_id": circle_id, "peers": peers})
        await write_frame(writer, {"t": "MSGS_HAVE", "circle_id": circle_id, "msg_ids": mids})

        # 2) Read their PEERS + MSGS_HAVE
        their_peers = await read_frame(reader)
        their_have = await read_frame(reader)

        if their_peers.get("t") != "PEERS" or their_have.get("t") != "MSGS_HAVE":
            await write_frame(writer, {"t": "ERROR", "err": "Bad sync frames"})
            return

        incoming_peers = their_peers.get("peers", [])
        incoming_msg_ids = set(their_have.get("msg_ids", []))

        # 3) Merge peers
        async with self._lock:
            self._merge_peers(circle_id, incoming_peers)
            my_msg_ids = set(self.message_ids_for_circle(circle_id))

        # 4) Request missing messages
        missing = sorted(list(incoming_msg_ids - my_msg_ids))
        await write_frame(writer, {"t": "MSGS_REQ", "circle_id": circle_id, "msg_ids": missing})

        req = await read_frame(reader)
        if req.get("t") != "MSGS_REQ":
            await write_frame(writer, {"t": "ERROR", "err": "Expected MSGS_REQ"})
            return
        they_missing = req.get("msg_ids", [])

        # 5) Send messages they’re missing
        async with self._lock:
            send_msgs = []
            for mid in they_missing:
                m = self.state.messages.get(mid)
                if m and m.circle_id == circle_id:
                    send_msgs.append(dataclasses.asdict(m))
        await write_frame(writer, {"t": "MSGS_SEND", "circle_id": circle_id, "messages": send_msgs})

        # 6) Receive messages we requested
        their_send = await read_frame(reader)
        if their_send.get("t") != "MSGS_SEND":
            await write_frame(writer, {"t": "ERROR", "err": "Expected MSGS_SEND"})
            return
        messages = their_send.get("messages", [])
        async with self._lock:
            self._merge_messages(messages)
            save_state(self.state)

    def _merge_peers(self, circle_id: str, peer_dicts: List[Dict[str, Any]]) -> None:
        members = self.state.circle_members.setdefault(circle_id, set())
        for pd in peer_dicts:
            node_id = str(pd.get("node_id", ""))
            addr = str(pd.get("addr", ""))
            last_seen = int(pd.get("last_seen", 0) or 0)
            if not node_id or not addr:
                continue
            members.add(node_id)
            existing = self.state.peers.get(node_id)
            if (not existing) or (last_seen > existing.last_seen) or (existing.addr != addr):
                self.state.peers[node_id] = Peer(node_id=node_id, addr=addr, last_seen=last_seen)

    def _merge_messages(self, msg_dicts: List[Dict[str, Any]]) -> None:
        for md in msg_dicts:
            try:
                m = ChatMessage(**md)
            except TypeError:
                continue
            if m.msg_id not in self.state.messages:
                self.state.messages[m.msg_id] = m

    async def connect_and_sync(self, peer_addr: str, circle_id: str) -> None:
        circle = self.state.circles.get(circle_id)
        if not circle:
            return

        host, port = parse_hostport(peer_addr)
        try:
            reader, writer = await asyncio.open_connection(host, port)
        except Exception:
            return

        try:
            hello = {
                "t": "HELLO",
                "node_id": self.state.node.node_id,
                "circle_id": circle_id,
                "token": make_token(circle.secret_hex, self.state.node.node_id, circle_id),
                "listen_addr": public_addr_hint(self.state.node.bind, self.state.node.port),
            }
            await write_frame(writer, hello)

            resp = await read_frame(reader)
            if resp.get("t") != "WELCOME":
                return

            # Server side will now send PEERS + MSGS_HAVE; we will respond accordingly.
            await self._sync_with_connected_peer(reader, writer, circle_id)

        except Exception:
            pass
        finally:
            try:
                writer.close()
                await writer.wait_closed()
            except Exception:
                pass

    async def gossip_loop(self, interval_s: int = 5) -> None:
        while True:
            await asyncio.sleep(interval_s)
            async with self._lock:
                circles = list(self.state.circles.keys())
            for cid in circles:
                async with self._lock:
                    peers = [p.addr for p in self.known_peers_for_circle(cid)]
                # Try a few peers each interval
                for addr in peers[:5]:
                    await self.connect_and_sync(addr, cid)


# ---------------------------
# CLI Commands
# ---------------------------

def cmd_init(args: argparse.Namespace) -> None:
    state = load_state()
    state.node.bind = args.bind
    state.node.port = args.port
    # Preserve existing node_id if present
    if not state.node.node_id:
        state.node.node_id = sha256_hex(secrets.token_bytes(32))[:24]
    save_state(state)
    print("Initialized.")
    print(f" node_id: {state.node.node_id}")
    print(f" listen : {public_addr_hint(state.node.bind, state.node.port)}")
    print(f" state  : {STATE_FILE}")


def cmd_invite(args: argparse.Namespace) -> None:
    state = load_state()
    secret = secrets.token_bytes(32)
    secret_hex = secret.hex()
    circle_id = sha256_hex(secret)[:24]
    circle = Circle(circle_id=circle_id, secret_hex=secret_hex)
    state.circles[circle_id] = circle
    state.circle_members.setdefault(circle_id, set()).add(state.node.node_id)
    save_state(state)

    bootstrap = public_addr_hint(state.node.bind, state.node.port)
    print("Circle created.")
    print(f" circle_id   : {circle_id}")
    print(f" circle_secret (hex): {secret_hex}")
    print()
    print("Share this join command with a friend:")
    print(f"  python felundchat.py join --secret {secret_hex} --peer {bootstrap}")
    print()
    print("Then run your node:")
    print("  python felundchat.py run")


def cmd_join(args: argparse.Namespace) -> None:
    state = load_state()
    secret_hex = args.secret.strip().lower()
    secret = bytes.fromhex(secret_hex)
    circle_id = sha256_hex(secret)[:24]
    state.circles[circle_id] = Circle(circle_id=circle_id, secret_hex=secret_hex)
    state.circle_members.setdefault(circle_id, set()).add(state.node.node_id)
    save_state(state)

    # Save bootstrap as a "peer" placeholder (node_id unknown until first sync)
    print(f"Joined circle {circle_id}. Bootstrapping via {args.peer} ...")
    node = GossipNode(state)

    async def _bootstrap():
        await node.connect_and_sync(args.peer, circle_id)

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
    created = now_ts()
    msg_id = sha256_hex(f"{state.node.node_id}|{created}|{secrets.token_hex(8)}".encode("utf-8"))[:32]
    msg = ChatMessage(
        msg_id=msg_id,
        circle_id=cid,
        author_node_id=state.node.node_id,
        created_ts=created,
        text=text,
    )
    state.messages[msg_id] = msg
    save_state(state)
    print(f"Queued message {msg_id}. It will gossip out while `run` is active.")


def cmd_run(args: argparse.Namespace) -> None:
    state = load_state()
    node = GossipNode(state)

    async def main():
        await node.start_server()
        # optional: immediate bootstrap attempt to a peer if provided
        if args.peer and args.circle_id:
            await node.connect_and_sync(args.peer, args.circle_id)
        await node.gossip_loop(interval_s=args.interval)

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
    msgs = [m for m in state.messages.values() if m.circle_id == cid]
    msgs.sort(key=lambda m: (m.created_ts, m.msg_id))
    for m in msgs[-args.limit:]:
        ts = time.strftime("%Y-%m-%d %H:%M:%S", time.localtime(m.created_ts))
        author = m.author_node_id[:8]
        print(f"[{ts}] {author}: {m.text}")


def build_parser() -> argparse.ArgumentParser:
    p = argparse.ArgumentParser(prog="felundchat", description="Simple gossip + direct connect chat")
    sub = p.add_subparsers(dest="cmd", required=True)

    sp = sub.add_parser("init", help="Initialize local node")
    sp.add_argument("--bind", default="0.0.0.0", help="Bind address (default 0.0.0.0)")
    sp.add_argument("--port", type=int, default=9999, help="Listen port")
    sp.set_defaults(func=cmd_init)

    sp = sub.add_parser("invite", help="Create a new circle and print an invite")
    sp.set_defaults(func=cmd_invite)

    sp = sub.add_parser("join", help="Join a circle using secret + a bootstrap peer")
    sp.add_argument("--secret", required=True, help="Circle secret hex (shared)")
    sp.add_argument("--peer", required=True, help="Bootstrap peer host:port")
    sp.set_defaults(func=cmd_join)

    sp = sub.add_parser("run", help="Run server + gossip loop")
    sp.add_argument("--interval", type=int, default=5, help="Gossip interval seconds")
    sp.add_argument("--circle-id", help="Optional: circle_id to immediately sync")
    sp.add_argument("--peer", help="Optional: peer host:port to immediately sync")
    sp.set_defaults(func=cmd_run)

    sp = sub.add_parser("send", help="Broadcast a text message to a circle")
    sp.add_argument("--circle-id", required=True, help="Circle id")
    sp.add_argument("text", help="Message text")
    sp.set_defaults(func=cmd_send)

    sp = sub.add_parser("inbox", help="Show recent messages for a circle")
    sp.add_argument("--circle-id", required=True)
    sp.add_argument("--limit", type=int, default=50)
    sp.set_defaults(func=cmd_inbox)

    sp = sub.add_parser("peers", help="List circles or peers for a circle")
    sp.add_argument("--circle-id", help="Circle id")
    sp.set_defaults(func=cmd_peers)

    return p


def main() -> None:
    parser = build_parser()
    args = parser.parse_args()
    args.func(args)


if __name__ == "__main__":
    main()