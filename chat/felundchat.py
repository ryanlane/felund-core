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
import contextlib
import dataclasses
import hashlib
import hmac
import json
import base64
import secrets
import socket
import time
from pathlib import Path
from typing import Any, Dict, List, Optional, Set, Tuple


APP_DIR = Path.home() / ".felundchat"
STATE_FILE = APP_DIR / "state.json"
MSG_MAX = 16_384  # bytes per frame, keep it small
READ_TIMEOUT_S = 30
MESSAGE_MAX_AGE_S = 30 * 24 * 60 * 60
MAX_MESSAGES_PER_CIRCLE = 1_000


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

def make_felund_code(secret_hex: str, peer_addr: str) -> str:
    payload = {"v": 1, "secret": secret_hex, "peer": peer_addr}
    raw = json.dumps(payload, separators=(",", ":")).encode("utf-8")
    return "felund1." + base64.urlsafe_b64encode(raw).decode("ascii").rstrip("=")

def parse_felund_code(code: str) -> Tuple[str, str]:
    code = code.strip()
    if not code.startswith("felund1."):
        raise ValueError("Invalid code prefix")
    token = code.split(".", 1)[1]
    padding = "=" * (-len(token) % 4)
    raw = base64.urlsafe_b64decode((token + padding).encode("ascii"))
    payload = json.loads(raw.decode("utf-8"))
    if payload.get("v") != 1:
        raise ValueError("Unsupported code version")
    secret_hex = str(payload.get("secret", "")).strip().lower()
    peer_addr = str(payload.get("peer", "")).strip()
    if not secret_hex or not peer_addr:
        raise ValueError("Code missing fields")
    bytes.fromhex(secret_hex)
    parse_hostport(peer_addr)
    return secret_hex, peer_addr


@dataclasses.dataclass
class NodeConfig:
    node_id: str
    bind: str
    port: int
    display_name: str = "anon"


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
    display_name: str = ""
    mac: str = ""


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
            node=NodeConfig(node_id=node_id, bind=bind, port=port, display_name="anon"),
            circles={},
            peers={},
            circle_members={},
            messages={},
        )


def detect_local_ip() -> str:
    try:
        s = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
        try:
            s.connect(("8.8.8.8", 80))
            return str(s.getsockname()[0])
        finally:
            s.close()
    except Exception:
        return "127.0.0.1"


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

    state = State(node=node, circles=circles, peers=peers, circle_members=circle_members, messages=messages)
    if not state.node.bind or state.node.bind == "0.0.0.0":
        state.node.bind = detect_local_ip()
    if not state.node.display_name:
        state.node.display_name = "anon"
    prune_messages(state)
    return state


def save_state(state: State) -> None:
    ensure_app_dir()
    prune_messages(state)
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
#   "listen_addr": "host:port"  (optional hint)
# }
#
# Server -> Client: CHALLENGE / ERROR
# {
#   "t": "CHALLENGE",
#   "nonce": "..."
# }
#
# Client -> Server: HELLO_AUTH
# {
#   "t": "HELLO_AUTH",
#   "token": "..."  (HMAC(secret, node_id||circle_id||nonce))
# }
#
# Server -> Client: WELCOME / ERROR
#
# Then:
# Client <-> Server exchange:
#  - PEERS: list of peers known for this circle
#  - MSGS_HAVE: list of message ids for this circle
#  - MSGS_SEND: full message objects for requested ids


def make_token(secret_hex: str, node_id: str, circle_id: str, nonce: str) -> str:
    secret = bytes.fromhex(secret_hex)
    payload = f"{node_id}|{circle_id}|{nonce}".encode("utf-8")
    return hmac_hex(secret, payload)


def verify_token(secret_hex: str, node_id: str, circle_id: str, nonce: str, token: str) -> bool:
    return hmac.compare_digest(make_token(secret_hex, node_id, circle_id, nonce), token)


def make_message_mac(secret_hex: str, msg: ChatMessage) -> str:
    secret = bytes.fromhex(secret_hex)
    payload = (
        f"{msg.msg_id}|{msg.circle_id}|{msg.author_node_id}|"
        f"{msg.display_name}|{msg.created_ts}|{msg.text}"
    ).encode("utf-8")
    return hmac_hex(secret, payload)


def verify_message_mac(secret_hex: str, msg: ChatMessage) -> bool:
    if not msg.mac:
        return False
    return hmac.compare_digest(make_message_mac(secret_hex, msg), msg.mac)


def prune_messages(state: State) -> None:
    now = now_ts()
    expired = [
        mid for mid, message in state.messages.items()
        if now - message.created_ts > MESSAGE_MAX_AGE_S
    ]
    for mid in expired:
        state.messages.pop(mid, None)

    for circle_id in state.circles.keys():
        circle_msgs = [m for m in state.messages.values() if m.circle_id == circle_id]
        if len(circle_msgs) <= MAX_MESSAGES_PER_CIRCLE:
            continue
        circle_msgs.sort(key=lambda message: (message.created_ts, message.msg_id))
        keep_ids = {message.msg_id for message in circle_msgs[-MAX_MESSAGES_PER_CIRCLE:]}
        drop_ids = [message.msg_id for message in circle_msgs if message.msg_id not in keep_ids]
        for mid in drop_ids:
            state.messages.pop(mid, None)


async def write_frame(writer: asyncio.StreamWriter, obj: Dict[str, Any]) -> None:
    raw = (json.dumps(obj, separators=(",", ":")) + "\n").encode("utf-8")
    if len(raw) > MSG_MAX:
        raise ValueError("Frame too large")
    writer.write(raw)
    await writer.drain()


async def read_frame(reader: asyncio.StreamReader) -> Dict[str, Any]:
    line = await asyncio.wait_for(reader.readline(), timeout=READ_TIMEOUT_S)
    if not line:
        raise EOFError
    if len(line) > MSG_MAX:
        raise ValueError("Frame too large")
    return json.loads(line.decode("utf-8"))


def public_addr_hint(bind: str, port: int) -> str:
    # Best-effort: try to guess a LAN IP for convenience in local testing.
    # Not reliable in multi-NIC setups.
    host = bind if bind and bind != "0.0.0.0" else detect_local_ip()
    return canonical_peer_addr(host, port)


# ---------------------------
# Gossip Engine
# ---------------------------

class GossipNode:
    def __init__(self, state: State):
        self.state = state
        self._lock = asyncio.Lock()
        self._server: Optional[asyncio.AbstractServer] = None
        self._stop_event = asyncio.Event()

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

    def stop(self) -> None:
        self._stop_event.set()

    async def _handle_conn(self, reader: asyncio.StreamReader, writer: asyncio.StreamWriter) -> None:
        peername = writer.get_extra_info("peername")
        try:
            hello = await read_frame(reader)
            if hello.get("t") != "HELLO":
                await write_frame(writer, {"t": "ERROR", "err": "Expected HELLO"})
                return

            peer_node_id = str(hello.get("node_id", ""))
            circle_id = str(hello.get("circle_id", ""))
            listen_addr = str(hello.get("listen_addr", "")) if hello.get("listen_addr") else ""
            nonce = secrets.token_hex(16)

            async with self._lock:
                circle = self.state.circles.get(circle_id)
                if not circle:
                    await write_frame(writer, {"t": "ERROR", "err": "Unknown circle_id"})
                    return

            await write_frame(writer, {"t": "CHALLENGE", "nonce": nonce})
            hello_auth = await read_frame(reader)
            if hello_auth.get("t") != "HELLO_AUTH":
                await write_frame(writer, {"t": "ERROR", "err": "Expected HELLO_AUTH"})
                return

            token = str(hello_auth.get("token", ""))

            async with self._lock:
                circle = self.state.circles.get(circle_id)
                if not circle:
                    await write_frame(writer, {"t": "ERROR", "err": "Unknown circle_id"})
                    return
                if not verify_token(circle.secret_hex, peer_node_id, circle_id, nonce, token):
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
            print(f"[server] error handling {peername}: {type(e).__name__}: {e}")
            try:
                await write_frame(writer, {"t": "ERROR", "err": "Internal error"})
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
            self._merge_messages(circle_id, messages)
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
            if (not existing) or (last_seen > existing.last_seen):
                self.state.peers[node_id] = Peer(node_id=node_id, addr=addr, last_seen=last_seen)

    def _merge_messages(self, circle_id: str, msg_dicts: List[Dict[str, Any]]) -> None:
        circle = self.state.circles.get(circle_id)
        if not circle:
            return
        for md in msg_dicts:
            try:
                m = ChatMessage(**md)
            except TypeError:
                continue
            if m.circle_id != circle_id:
                continue
            if not verify_message_mac(circle.secret_hex, m):
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
                "listen_addr": public_addr_hint(self.state.node.bind, self.state.node.port),
            }
            await write_frame(writer, hello)

            challenge = await read_frame(reader)
            if challenge.get("t") != "CHALLENGE":
                print(f"[sync] {peer_addr} {circle_id}: expected CHALLENGE")
                return

            nonce = str(challenge.get("nonce", ""))
            hello = {
                "t": "HELLO_AUTH",
                "token": make_token(circle.secret_hex, self.state.node.node_id, circle_id, nonce),
            }
            await write_frame(writer, hello)

            resp = await read_frame(reader)
            if resp.get("t") != "WELCOME":
                print(f"[sync] {peer_addr} {circle_id}: rejected ({resp.get('err', 'unknown')})")
                return

            # Server side will now send PEERS + MSGS_HAVE; we will respond accordingly.
            await self._sync_with_connected_peer(reader, writer, circle_id)

        except Exception as e:
            print(f"[sync] {peer_addr} {circle_id}: {type(e).__name__}: {e}")
        finally:
            try:
                writer.close()
                await writer.wait_closed()
            except Exception:
                pass

    async def gossip_loop(self, interval_s: int = 5) -> None:
        while not self._stop_event.is_set():
            try:
                await asyncio.wait_for(self._stop_event.wait(), timeout=interval_s)
                break
            except asyncio.TimeoutError:
                pass
            async with self._lock:
                circles = list(self.state.circles.keys())
            for cid in circles:
                async with self._lock:
                    peers = [p.addr for p in self.known_peers_for_circle(cid)]
                # Try a few peers each interval
                for addr in peers[:5]:
                    await self.connect_and_sync(addr, cid)


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

    async def watch_incoming() -> None:
        while not node._stop_event.is_set():
            async with node._lock:
                msgs = [m for m in state.messages.values() if m.circle_id == circle_id]
                msgs.sort(key=lambda m: (m.created_ts, m.msg_id))
            for m in msgs:
                if m.msg_id in seen:
                    continue
                seen.add(m.msg_id)
                print(render_message(m))
            await asyncio.sleep(1)

    watcher = asyncio.create_task(watch_incoming())
    print("Chat ready. Type messages and press Enter.")
    print("Commands: /switch, /circles, /inbox, /quit")

    try:
        while True:
            raw = await asyncio.to_thread(input, f"[{circle_id[:8]}] > ")
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

            created = now_ts()
            msg_id = sha256_hex(f"{state.node.node_id}|{created}|{secrets.token_hex(8)}".encode("utf-8"))[:32]
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
            await sync_circle_once(node, state, circle_id, extra_peers=[bootstrap_peer] if bootstrap_peer else None)
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

    bootstrap_task: Optional[asyncio.Task[Any]] = None
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


# ---------------------------
# CLI Commands
# ---------------------------

def cmd_init(args: argparse.Namespace) -> None:
    state = load_state()
    state.node.bind = args.bind
    state.node.port = args.port
    if args.name:
        state.node.display_name = args.name.strip()
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
    save_state(state)

    # Save bootstrap as a "peer" placeholder (node_id unknown until first sync)
    print(f"Joined circle {circle_id}. Bootstrapping via {peer_addr} ...")
    node = GossipNode(state)

    async def _bootstrap():
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
    created = now_ts()
    msg_id = sha256_hex(f"{state.node.node_id}|{created}|{secrets.token_hex(8)}".encode("utf-8"))[:32]
    msg = ChatMessage(
        msg_id=msg_id,
        circle_id=cid,
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

    async def main():
        await node.start_server()
        try:
            # optional: immediate bootstrap attempt to a peer if provided
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
    msgs = [m for m in state.messages.values() if m.circle_id == cid]
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
    if not getattr(args, "cmd", None) or args.cmd == "interactive":
        asyncio.run(run_interactive_flow())
        return
    args.func(args)


if __name__ == "__main__":
    main()