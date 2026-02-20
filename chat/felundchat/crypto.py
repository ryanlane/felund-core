from __future__ import annotations

import hashlib
import hmac
from typing import TYPE_CHECKING

if TYPE_CHECKING:
    from felundchat.models import ChatMessage


def sha256_hex(b: bytes) -> str:
    return hashlib.sha256(b).hexdigest()


def hmac_hex(key: bytes, msg: bytes) -> str:
    return hmac.new(key, msg, hashlib.sha256).hexdigest()


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
