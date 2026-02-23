from __future__ import annotations

import dataclasses
import secrets
import time
from typing import Any, Dict, Set


def now_ts() -> int:
    return int(time.time())


@dataclasses.dataclass
class NodeConfig:
    node_id: str
    bind: str
    port: int
    display_name: str = "anon"
    rendezvous_base: str = ""  # relay URL; set via FELUND_API_BASE env or F3 settings; empty = disabled
    can_anchor: bool = False       # True: this node is willing to store-and-forward for the circle
    is_mobile: bool = False        # True: this node is on a mobile/intermittent connection
    public_reachable: bool = False  # True: this node is publicly reachable (updated from observed connects)


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
class AnchorRecord:
    """Tracks a peer that has announced anchor capability for a circle."""
    node_id: str
    capabilities: Dict[str, Any]  # can_anchor, public_reachable, is_mobile, bandwidth_hint
    announced_at: int              # unix timestamp of the latest ANCHOR_ANNOUNCE event
    last_seen_ts: int              # unix timestamp when we last processed an announce from this node


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
    schema_version: int = 1  # 1 = legacy plaintext, 2 = v2 encrypted envelope


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
    anchor_records: Dict[str, Dict[str, AnchorRecord]] = dataclasses.field(
        default_factory=dict
    )  # circle_id -> node_id -> AnchorRecord (persisted so nodes remember known anchors)

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
            anchor_records={},
        )
