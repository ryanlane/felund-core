from __future__ import annotations

import dataclasses
import secrets
import time
from typing import Dict, Optional, Set


def now_ts() -> int:
    return int(time.time())


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
    circles: Dict[str, Circle]              # circle_id -> Circle
    peers: Dict[str, Peer]                  # peer_node_id -> Peer
    circle_members: Dict[str, Set[str]]     # circle_id -> set(peer_node_id)
    messages: Dict[str, ChatMessage]        # msg_id -> ChatMessage

    @staticmethod
    def default(bind: str, port: int) -> State:
        from felundchat.crypto import sha256_hex  # local import avoids circular dep
        node_id = sha256_hex(secrets.token_bytes(32))[:24]
        return State(
            node=NodeConfig(node_id=node_id, bind=bind, port=port, display_name="anon"),
            circles={},
            peers={},
            circle_members={},
            messages={},
        )
