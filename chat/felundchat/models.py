from __future__ import annotations

import dataclasses
import secrets
import time
from typing import Dict, Set


def now_ts() -> int:
    return int(time.time())


@dataclasses.dataclass
class NodeConfig:
    node_id: str
    bind: str
    port: int
    display_name: str = "anon"
    rendezvous_base: str = ""  # relay URL; set via FELUND_API_BASE env or F3 settings; empty = disabled


@dataclasses.dataclass
class Circle:
    circle_id: str
    secret_hex: str  # shared secret in hex
    name: str = ""   # optional friendly label


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
    channel_id: str = "general"
    display_name: str = ""
    mac: str = ""


@dataclasses.dataclass
class Channel:
    channel_id: str
    circle_id: str
    created_by: str
    created_ts: int
    access_mode: str = "public"  # public | key | invite
    key_hash: str = ""


@dataclasses.dataclass
class State:
    node: NodeConfig
    circles: Dict[str, Circle]              # circle_id -> Circle
    peers: Dict[str, Peer]                  # peer_node_id -> Peer
    circle_members: Dict[str, Set[str]]     # circle_id -> set(peer_node_id)
    messages: Dict[str, ChatMessage]        # msg_id -> ChatMessage
    channels: Dict[str, Dict[str, Channel]]  # circle_id -> channel_id -> Channel
    channel_members: Dict[str, Dict[str, Set[str]]]  # circle_id -> channel_id -> member node_ids
    channel_requests: Dict[str, Dict[str, Set[str]]]  # circle_id -> channel_id -> pending node_ids
    node_display_names: Dict[str, str]  # node_id -> latest display name

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
            channels={},
            channel_members={},
            channel_requests={},
            node_display_names={node_id: "anon"},
        )
