from __future__ import annotations

import base64
import hashlib
import hmac
import json
import os
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
        f"{msg.msg_id}|{msg.circle_id}|{msg.channel_id}|{msg.author_node_id}|"
        f"{msg.display_name}|{msg.created_ts}|{msg.text}"
    ).encode("utf-8")
    return hmac_hex(secret, payload)


def verify_message_mac(secret_hex: str, msg: ChatMessage) -> bool:
    if not msg.mac:
        return False
    return hmac.compare_digest(make_message_mac(secret_hex, msg), msg.mac)


# ── AES-256-GCM message encryption ────────────────────────────────────────────

def derive_message_key(secret_hex: str) -> bytes:
    """Derive a 256-bit AES key from the circle secret using HKDF-SHA256."""
    from cryptography.hazmat.primitives import hashes
    from cryptography.hazmat.primitives.kdf.hkdf import HKDF

    hkdf = HKDF(
        algorithm=hashes.SHA256(),
        length=32,
        salt=None,
        info=b"felund-msg-v1",
    )
    return hkdf.derive(bytes.fromhex(secret_hex))


def _aad(msg_id: str, circle_id: str, channel_id: str, author_node_id: str, created_ts: int) -> bytes:
    return f"{msg_id}|{circle_id}|{channel_id}|{author_node_id}|{created_ts}".encode("utf-8")


def encrypt_message_fields(secret_hex: str, msg: ChatMessage) -> dict:
    """Encrypt display_name and text with AES-256-GCM.

    Returns an ``enc`` dict with ``alg``, ``key_id``, ``nonce`` (base64),
    and ``ciphertext`` (base64, includes the 16-byte GCM authentication tag).
    """
    from cryptography.hazmat.primitives.ciphers.aead import AESGCM

    key = derive_message_key(secret_hex)
    nonce = os.urandom(12)
    aad = _aad(msg.msg_id, msg.circle_id, msg.channel_id, msg.author_node_id, msg.created_ts)
    plaintext = json.dumps({"display_name": msg.display_name, "text": msg.text},
                           separators=(",", ":")).encode("utf-8")
    ciphertext = AESGCM(key).encrypt(nonce, plaintext, aad)
    return {
        "alg": "AES-256-GCM",
        "key_id": "epoch-0",
        "nonce": base64.b64encode(nonce).decode(),
        "ciphertext": base64.b64encode(ciphertext).decode(),
    }


def decrypt_message_fields(
    secret_hex: str,
    enc: dict,
    msg_id: str,
    circle_id: str,
    channel_id: str,
    author_node_id: str,
    created_ts: int,
) -> dict:
    """Decrypt an ``enc`` dict produced by :func:`encrypt_message_fields`.

    Returns ``{"display_name": str, "text": str}``.
    Raises ``cryptography.exceptions.InvalidTag`` on auth failure,
    or ``ValueError`` / ``KeyError`` on malformed input.
    """
    from cryptography.hazmat.primitives.ciphers.aead import AESGCM

    key = derive_message_key(secret_hex)
    nonce = base64.b64decode(enc["nonce"])
    ciphertext = base64.b64decode(enc["ciphertext"])
    aad = _aad(msg_id, circle_id, channel_id, author_node_id, created_ts)
    plaintext = AESGCM(key).decrypt(nonce, ciphertext, aad)
    data = json.loads(plaintext)
    return {
        "display_name": str(data.get("display_name", "")),
        "text": str(data.get("text", "")),
    }
