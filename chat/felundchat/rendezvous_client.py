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


def safe_api_base_from_env() -> str:
    import os

    value = os.getenv("FELUND_API_BASE", "").strip()
    return value.rstrip("/")


def is_network_error(exc: Exception) -> bool:
    return isinstance(exc, (urllib.error.URLError, TimeoutError, OSError, ValueError, json.JSONDecodeError))
