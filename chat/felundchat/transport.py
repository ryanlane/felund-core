from __future__ import annotations

import asyncio
import json
import socket
from typing import Any, Dict, Tuple

from felundchat.config import MSG_MAX, READ_TIMEOUT_S


def parse_hostport(s: str) -> Tuple[str, int]:
    if ":" not in s:
        raise ValueError("Expected host:port")
    host, port_s = s.rsplit(":", 1)
    return host, int(port_s)


def canonical_peer_addr(host: str, port: int) -> str:
    return f"{host}:{port}"


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


def public_addr_hint(bind: str, port: int) -> str:
    host = bind if bind and bind != "0.0.0.0" else detect_local_ip()
    return canonical_peer_addr(host, port)


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
