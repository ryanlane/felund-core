from __future__ import annotations

import asyncio
import base64
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


async def write_enc_frame(
    writer: asyncio.StreamWriter, session_key: bytes, obj: Dict[str, Any]
) -> None:
    """Encrypt *obj* with AES-256-GCM and send as a base64-encoded line.

    Wire format: base64(12-byte-nonce || ciphertext+tag) + "\\n"
    """
    from felundchat.crypto import encrypt_frame_bytes

    plaintext = json.dumps(obj, separators=(",", ":")).encode("utf-8")
    encrypted = encrypt_frame_bytes(session_key, plaintext)
    line = base64.b64encode(encrypted) + b"\n"
    # Encrypted frames are ~4/3 the size of the plaintext; allow headroom.
    if len(line) > MSG_MAX * 2:
        raise ValueError("Frame too large")
    writer.write(line)
    await writer.drain()


async def read_enc_frame(
    reader: asyncio.StreamReader, session_key: bytes
) -> Dict[str, Any]:
    """Read a base64-encoded encrypted frame and return the decrypted object.

    Raises ``cryptography.exceptions.InvalidTag`` if the GCM auth tag fails.
    """
    from felundchat.crypto import decrypt_frame_bytes

    line = await asyncio.wait_for(reader.readline(), timeout=READ_TIMEOUT_S)
    if not line:
        raise EOFError
    data = base64.b64decode(line.strip())
    plaintext = decrypt_frame_bytes(session_key, data)
    return json.loads(plaintext.decode("utf-8"))
