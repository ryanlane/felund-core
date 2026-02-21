from __future__ import annotations

import json
import urllib.error
import urllib.parse
import urllib.request
from typing import List, Optional, Tuple

from .crypto import sha256_hex
from .models import State
from .transport import parse_hostport, public_addr_hint


def circle_hint(circle_id: str) -> str:
    return sha256_hex(circle_id.encode("utf-8"))[:16]


def _api_request(
    method: str,
    url: str,
    body: Optional[dict] = None,
    headers: Optional[dict] = None,
    timeout: int = 8,
) -> dict:
    raw = None
    request_headers = {"content-type": "application/json"}
    if headers:
        request_headers.update(headers)
    if body is not None:
        raw = json.dumps(body, separators=(",", ":")).encode("utf-8")
    req = urllib.request.Request(url=url, data=raw, headers=request_headers, method=method)
    with urllib.request.urlopen(req, timeout=timeout) as resp:
        payload = resp.read().decode("utf-8")
        if not payload:
            return {}
        return json.loads(payload)


def register_presence(api_base: str, state: State, circle_id: str, ttl_s: int = 120) -> None:
    listen_addr = public_addr_hint(state.node.bind, state.node.port)
    host, port = parse_hostport(listen_addr)

    payload = {
        "node_id": state.node.node_id,
        "circle_hint": circle_hint(circle_id),
        "endpoints": [
            {
                "transport": "tcp",
                "host": host,
                "port": port,
                "family": "ipv6" if ":" in host else "ipv4",
                "nat": "unknown",
            }
        ],
        "capabilities": {"relay": False, "transport": ["tcp"]},
        "ttl_s": ttl_s,
    }
    headers = {"X-Felund-Node": state.node.node_id}
    _api_request("POST", f"{api_base}/v1/register", payload, headers=headers)


def unregister_presence(api_base: str, state: State, circle_id: str) -> None:
    payload = {
        "node_id": state.node.node_id,
        "circle_hint": circle_hint(circle_id),
    }
    _api_request("DELETE", f"{api_base}/v1/register", payload)


def lookup_peer_addrs(
    api_base: str,
    state: State,
    circle_id: str,
    limit: int = 50,
) -> List[Tuple[str, str]]:
    query = urllib.parse.urlencode({"circle_hint": circle_hint(circle_id), "limit": limit})
    url = f"{api_base}/v1/peers?{query}"
    headers = {"X-Felund-Node": state.node.node_id}
    data = _api_request("GET", url, headers=headers)

    out: List[Tuple[str, str]] = []
    for peer in data.get("peers", []):
        node_id = str(peer.get("node_id", ""))
        if not node_id or node_id == state.node.node_id:
            continue
        endpoints = peer.get("endpoints", [])
        addr = ""
        for endpoint in endpoints:
            if str(endpoint.get("transport", "")) != "tcp":
                continue
            host = str(endpoint.get("host", "")).strip()
            port = int(endpoint.get("port", 0) or 0)
            if host and port > 0:
                addr = f"{host}:{port}"
                break
        if addr:
            out.append((node_id, addr))
    return out


def merge_discovered_peers(state: State, circle_id: str, peer_addrs: List[Tuple[str, str]]) -> bool:
    from .models import Peer, now_ts  # avoid circular import at module load

    changed = False
    members = state.circle_members.setdefault(circle_id, set())
    now = now_ts()

    for node_id, addr in peer_addrs:
        if not node_id or not addr or node_id == state.node.node_id:
            continue
        if node_id not in members:
            members.add(node_id)
            changed = True

        existing = state.peers.get(node_id)
        if (not existing) or (existing.addr != addr) or (now > existing.last_seen):
            state.peers[node_id] = Peer(node_id=node_id, addr=addr, last_seen=now)
            changed = True

    return changed


def push_messages_to_relay(api_base: str, state: State, circle_id: str) -> int:
    """Push local messages for *circle_id* to the relay server.

    Only the 100 most-recent non-control messages are sent per call.
    Returns the number of newly stored messages reported by the server.
    """
    msgs = sorted(
        (
            m for m in state.messages.values()
            if m.circle_id == circle_id and m.channel_id != "__control"
        ),
        key=lambda m: m.created_ts,
    )[-100:]
    if not msgs:
        return 0
    hint = circle_hint(circle_id)
    stored_total = 0
    for i in range(0, len(msgs), 50):  # server limit: 50 per batch
        batch = msgs[i : i + 50]
        payload = {
            "circle_hint": hint,
            "messages": [
                {
                    "msg_id": m.msg_id,
                    "circle_id": m.circle_id,
                    "channel_id": m.channel_id,
                    "author_node_id": m.author_node_id,
                    "display_name": m.display_name,
                    "created_ts": m.created_ts,
                    "text": m.text,
                    "mac": m.mac,
                }
                for m in batch
            ],
        }
        data = _api_request("POST", f"{api_base}/v1/messages", payload)
        stored_total += data.get("stored", 0)
    return stored_total


def pull_messages_from_relay(
    api_base: str,
    state: State,
    circle_id: str,
    since: int = 0,
) -> Tuple[List[dict], int]:
    """Fetch messages from the relay for *circle_id* newer than *since*.

    Returns ``(raw_message_dicts, server_time)`` — pass *server_time* as
    *since* on the next call to avoid re-fetching already-seen messages.
    """
    hint = circle_hint(circle_id)
    query = urllib.parse.urlencode({"circle_hint": hint, "since": since, "limit": 200})
    data = _api_request("GET", f"{api_base}/v1/messages?{query}")
    return data.get("messages", []), int(data.get("server_time", 0))


def merge_relay_messages(state: State, circle_id: str, raw_msgs: List[dict]) -> bool:
    """Merge relay messages into *state* after verifying each MAC.

    Skips messages already present.  Returns True if anything was added.
    """
    from .crypto import verify_message_mac
    from .models import ChatMessage

    circle = state.circles.get(circle_id)
    if not circle:
        return False
    changed = False
    for raw in raw_msgs:
        msg_id = str(raw.get("msg_id", ""))
        if not msg_id or msg_id in state.messages:
            continue
        try:
            msg = ChatMessage(
                msg_id=msg_id,
                circle_id=str(raw.get("circle_id", "")),
                channel_id=str(raw.get("channel_id", "general")),
                author_node_id=str(raw.get("author_node_id", "")),
                display_name=str(raw.get("display_name", "")),
                created_ts=int(raw.get("created_ts", 0)),
                text=str(raw.get("text", "")),
                mac=str(raw.get("mac", "")),
            )
        except (TypeError, ValueError):
            continue
        if msg.circle_id != circle_id:
            continue
        if not verify_message_mac(circle.secret_hex, msg):
            continue
        state.messages[msg_id] = msg
        changed = True
    return changed


def safe_api_base_from_env() -> str:
    import os

    value = os.getenv("FELUND_API_BASE", "").strip()
    return value.rstrip("/")


def is_network_error(exc: Exception) -> bool:
    return isinstance(exc, (urllib.error.URLError, TimeoutError, OSError, ValueError, json.JSONDecodeError))
