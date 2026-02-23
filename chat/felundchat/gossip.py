from __future__ import annotations

import asyncio
import dataclasses
import secrets
from typing import Any, Dict, List, Optional

from felundchat.crypto import derive_session_key, make_token, verify_message_mac, verify_token
from felundchat.channel_sync import (
    CONTROL_CHANNEL_ID,
    apply_channel_event,
    apply_circle_name_event,
    parse_channel_event,
    parse_circle_name_event,
)
from felundchat.models import ChatMessage, Peer, State, now_ts
from felundchat.persistence import save_state
from felundchat.transport import (
    canonical_peer_addr,
    parse_hostport,
    public_addr_hint,
    read_enc_frame,
    read_frame,
    write_enc_frame,
    write_frame,
)


class GossipNode:
    def __init__(self, state: State):
        self.state = state
        self._lock = asyncio.Lock()
        self._server: Optional[asyncio.AbstractServer] = None
        self._stop_event = asyncio.Event()
        self.debug_sync = False

    def _sync_log(self, message: str) -> None:
        if self.debug_sync:
            print(message)

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
        self._server = await asyncio.start_server(
            self._handle_conn, self.state.node.bind, self.state.node.port
        )
        addrs = ", ".join(str(sock.getsockname()) for sock in (self._server.sockets or []))
        print(f"[server] listening on {addrs}")

    async def stop_server(self) -> None:
        if self._server:
            self._server.close()
            await self._server.wait_closed()
            self._server = None

    def stop(self) -> None:
        self._stop_event.set()

    async def _handle_conn(
        self, reader: asyncio.StreamReader, writer: asyncio.StreamWriter
    ) -> None:
        peername = writer.get_extra_info("peername")
        try:
            hello = await read_frame(reader)
            if hello.get("t") != "HELLO":
                await write_frame(writer, {"t": "ERROR", "err": "Expected HELLO"})
                return

            peer_node_id = str(hello.get("node_id", ""))
            circle_id = str(hello.get("circle_id", ""))
            listen_addr = str(hello.get("listen_addr", "")) if hello.get("listen_addr") else ""
            # Client-supplied nonce for session key derivation (optional; absent = legacy client)
            client_nonce = str(hello.get("nonce", ""))
            server_nonce = secrets.token_hex(16)

            async with self._lock:
                circle = self.state.circles.get(circle_id)
                if not circle:
                    await write_frame(writer, {"t": "ERROR", "err": "Unknown circle_id"})
                    return

            await write_frame(writer, {"t": "CHALLENGE", "nonce": server_nonce})
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
                if not verify_token(circle.secret_hex, peer_node_id, circle_id, server_nonce, token):
                    await write_frame(writer, {"t": "ERROR", "err": "Auth failed"})
                    return

                resolved_addr = self._resolve_peer_addr(peername, listen_addr)
                if listen_addr:
                    self.state.peers[peer_node_id] = Peer(
                        node_id=peer_node_id, addr=resolved_addr, last_seen=now_ts()
                    )
                else:
                    if peer_node_id in self.state.peers:
                        self.state.peers[peer_node_id].last_seen = now_ts()

                self.state.circle_members.setdefault(circle_id, set()).add(peer_node_id)
                save_state(self.state)
                secret_hex = circle.secret_hex

            # Negotiate session encryption: signal readiness if client sent a nonce.
            enc_ready = bool(client_nonce)
            await write_frame(writer, {
                "t": "WELCOME",
                "node_id": self.state.node.node_id,
                "enc_ready": enc_ready,
            })

            session_key: Optional[bytes] = None
            if enc_ready:
                session_key = derive_session_key(secret_hex, client_nonce, server_nonce)
                self._sync_log(f"[server] session encryption enabled for {peer_node_id[:8]}")

            await self._sync_with_connected_peer(reader, writer, circle_id, session_key)

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

    async def _sync_with_connected_peer(
        self,
        reader: asyncio.StreamReader,
        writer: asyncio.StreamWriter,
        circle_id: str,
        session_key: Optional[bytes] = None,
    ) -> None:
        # Transparent read/write helpers — use session encryption when available.
        async def _read() -> Dict[str, Any]:
            if session_key:
                return await read_enc_frame(reader, session_key)
            return await read_frame(reader)

        async def _write(obj: Dict[str, Any]) -> None:
            if session_key:
                await write_enc_frame(writer, session_key, obj)
            else:
                await write_frame(writer, obj)

        # 1) Send our peer list + message ids
        async with self._lock:
            peers = [
                {"node_id": p.node_id, "addr": p.addr, "last_seen": p.last_seen}
                for p in self.known_peers_for_circle(circle_id)
            ]
            mids = self.message_ids_for_circle(circle_id)

        await _write({"t": "PEERS", "circle_id": circle_id, "peers": peers})
        await _write({"t": "MSGS_HAVE", "circle_id": circle_id, "msg_ids": mids})

        # 2) Read their PEERS + MSGS_HAVE
        their_peers = await _read()
        their_have = await _read()

        if their_peers.get("t") != "PEERS" or their_have.get("t") != "MSGS_HAVE":
            await _write({"t": "ERROR", "err": "Bad sync frames"})
            return

        incoming_peers = their_peers.get("peers", [])
        incoming_msg_ids = set(their_have.get("msg_ids", []))

        # 3) Merge peers
        async with self._lock:
            self._merge_peers(circle_id, incoming_peers)
            my_msg_ids = set(self.message_ids_for_circle(circle_id))

        # 4) Request missing messages
        missing = sorted(list(incoming_msg_ids - my_msg_ids))
        await _write({"t": "MSGS_REQ", "circle_id": circle_id, "msg_ids": missing})

        req = await _read()
        if req.get("t") != "MSGS_REQ":
            await _write({"t": "ERROR", "err": "Expected MSGS_REQ"})
            return
        they_missing = req.get("msg_ids", [])

        # 5) Send messages they're missing
        async with self._lock:
            send_msgs = []
            for mid in they_missing:
                m = self.state.messages.get(mid)
                if m and m.circle_id == circle_id:
                    send_msgs.append(dataclasses.asdict(m))
        await _write({"t": "MSGS_SEND", "circle_id": circle_id, "messages": send_msgs})

        # 6) Receive messages we requested
        their_send = await _read()
        if their_send.get("t") != "MSGS_SEND":
            await _write({"t": "ERROR", "err": "Expected MSGS_SEND"})
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
                if m.display_name:
                    self.state.node_display_names[m.author_node_id] = m.display_name[:40]
                if m.channel_id == CONTROL_CHANNEL_ID:
                    event = parse_channel_event(m.text)
                    if event:
                        apply_channel_event(self.state, circle_id, event)
                    else:
                        name_event = parse_circle_name_event(m.text)
                        if name_event:
                            apply_circle_name_event(self.state, circle_id, name_event)

    def _resolve_peer_addr(self, peername: Any, listen_addr: str) -> str:
        if not listen_addr:
            return ""

        try:
            adv_host, adv_port = parse_hostport(listen_addr)
        except Exception:
            return listen_addr

        observed_host = ""
        if isinstance(peername, tuple) and len(peername) >= 1:
            observed_host = str(peername[0])

        if observed_host:
            return canonical_peer_addr(observed_host, adv_port)

        return canonical_peer_addr(adv_host, adv_port)

    async def connect_and_sync(self, peer_addr: str, circle_id: str) -> None:
        circle = self.state.circles.get(circle_id)
        if not circle:
            return

        host, port = parse_hostport(peer_addr)
        try:
            reader, writer = await asyncio.open_connection(host, port)
        except Exception as e:
            print(f"[sync] {peer_addr}: connect failed ({type(e).__name__}: {e})")
            return

        try:
            client_nonce = secrets.token_hex(16)
            hello = {
                "t": "HELLO",
                "node_id": self.state.node.node_id,
                "circle_id": circle_id,
                "listen_addr": public_addr_hint(self.state.node.bind, self.state.node.port),
                "nonce": client_nonce,
            }
            await write_frame(writer, hello)

            challenge = await read_frame(reader)
            if challenge.get("t") != "CHALLENGE":
                self._sync_log(f"[sync] {peer_addr} {circle_id}: expected CHALLENGE")
                return

            server_nonce = str(challenge.get("nonce", ""))
            auth = {
                "t": "HELLO_AUTH",
                "token": make_token(circle.secret_hex, self.state.node.node_id, circle_id, server_nonce),
            }
            await write_frame(writer, auth)

            resp = await read_frame(reader)
            if resp.get("t") != "WELCOME":
                self._sync_log(
                    f"[sync] {peer_addr} {circle_id}: rejected ({resp.get('err', 'unknown')})"
                )
                return

            # Record the server peer so our gossip loop can reach them in future rounds.
            server_node_id = str(resp.get("node_id", ""))
            if server_node_id and server_node_id != self.state.node.node_id:
                async with self._lock:
                    self.state.circle_members.setdefault(circle_id, set()).add(server_node_id)
                    ts = now_ts()
                    existing = self.state.peers.get(server_node_id)
                    if not existing or ts >= existing.last_seen:
                        self.state.peers[server_node_id] = Peer(
                            node_id=server_node_id, addr=peer_addr, last_seen=ts,
                        )

            # Enable session encryption when the server confirms it's ready.
            session_key: Optional[bytes] = None
            if resp.get("enc_ready"):
                session_key = derive_session_key(circle.secret_hex, client_nonce, server_nonce)
                self._sync_log(f"[sync] {peer_addr} {circle_id}: session encryption enabled")

            await self._sync_with_connected_peer(reader, writer, circle_id, session_key)

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
                for addr in peers[:5]:
                    await self.connect_and_sync(addr, cid)
