from __future__ import annotations

import base64
import json
from typing import Tuple

from felundchat.transport import parse_hostport


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
