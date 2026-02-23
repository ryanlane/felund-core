from __future__ import annotations

import json
import secrets
from typing import Any, Dict, Optional

from .crypto import make_message_mac, sha256_hex
from .models import Channel, ChatMessage, State, now_ts


CONTROL_CHANNEL_ID = "__control"


def _valid_channel_id(channel_id: str) -> bool:
    if not channel_id or len(channel_id) > 32:
        return False
    if channel_id.startswith("__"):
        return False
    return all(c.isalnum() or c in {"-", "_"} for c in channel_id)


def make_channel_event_message(state: State, circle_id: str, event: Dict[str, Any]) -> Optional[ChatMessage]:
    circle = state.circles.get(circle_id)
    if not circle:
        return None

    created = now_ts()
    msg_id = sha256_hex(
        f"{state.node.node_id}|{created}|{secrets.token_hex(8)}".encode("utf-8")
    )[:32]
    text = json.dumps(event, separators=(",", ":"), sort_keys=True)

    msg = ChatMessage(
        msg_id=msg_id,
        circle_id=circle_id,
        channel_id=CONTROL_CHANNEL_ID,
        author_node_id=state.node.node_id,
        display_name=state.node.display_name,
        created_ts=created,
        text=text,
    )
    msg.mac = make_message_mac(circle.secret_hex, msg)
    return msg


def parse_channel_event(text: str) -> Optional[Dict[str, Any]]:
    try:
        data = json.loads(text)
    except Exception:
        return None

    if not isinstance(data, dict):
        return None
    if data.get("t") != "CHANNEL_EVT":
        return None
    op = str(data.get("op", ""))
    if op not in {"create", "join", "request", "approve", "leave", "rename"}:
        return None
    return data


def _ensure_channel_maps(state: State, circle_id: str) -> None:
    state.channels.setdefault(circle_id, {})
    state.channel_members.setdefault(circle_id, {})
    state.channel_requests.setdefault(circle_id, {})


def _ensure_general(state: State, circle_id: str) -> None:
    _ensure_channel_maps(state, circle_id)
    channels = state.channels[circle_id]
    if "general" not in channels:
        channels["general"] = Channel(
            channel_id="general",
            circle_id=circle_id,
            created_by=state.node.node_id,
            created_ts=now_ts(),
            access_mode="public",
        )
    state.channel_members[circle_id].setdefault("general", set())
    state.channel_requests[circle_id].setdefault("general", set())


def apply_channel_event(state: State, circle_id: str, event: Dict[str, Any]) -> None:
    _ensure_general(state, circle_id)

    op = str(event.get("op", ""))

    if op == "rename":
        node_id = str(event.get("node_id", "")).strip()
        display_name = str(event.get("display_name", "")).strip()
        if node_id and display_name:
            state.node_display_names[node_id] = display_name[:40]
        return

    channel_id = str(event.get("channel_id", "")).strip().lower()

    if op == "create":
        if not _valid_channel_id(channel_id):
            return
        access_mode = str(event.get("access_mode", "public"))
        if access_mode not in {"public", "key", "invite"}:
            access_mode = "public"
        created_by = str(event.get("actor_node_id", "")) or str(event.get("created_by", ""))
        created_ts = int(event.get("created_ts", now_ts()) or now_ts())
        key_hash = str(event.get("key_hash", "")) if access_mode == "key" else ""

        channels = state.channels[circle_id]
        members_map = state.channel_members[circle_id]
        requests_map = state.channel_requests[circle_id]

        if channel_id not in channels:
            channels[channel_id] = Channel(
                channel_id=channel_id,
                circle_id=circle_id,
                created_by=created_by,
                created_ts=created_ts,
                access_mode=access_mode,
                key_hash=key_hash,
            )
        members_map.setdefault(channel_id, set()).add(created_by)
        requests_map.setdefault(channel_id, set())
        return

    if not _valid_channel_id(channel_id):
        return

    channels = state.channels[circle_id]
    members_map = state.channel_members[circle_id]
    requests_map = state.channel_requests[circle_id]

    if channel_id not in channels:
        channels[channel_id] = Channel(
            channel_id=channel_id,
            circle_id=circle_id,
            created_by=str(event.get("actor_node_id", "")),
            created_ts=int(event.get("created_ts", now_ts()) or now_ts()),
            access_mode="public",
        )
    members_map.setdefault(channel_id, set())
    requests_map.setdefault(channel_id, set())

    if op == "join":
        node_id = str(event.get("node_id", ""))
        if node_id:
            members_map[channel_id].add(node_id)
            requests_map[channel_id].discard(node_id)
        return

    if op == "leave":
        node_id = str(event.get("node_id", ""))
        if node_id and channel_id != "general":
            members_map[channel_id].discard(node_id)
            requests_map[channel_id].discard(node_id)
        return

    if op == "request":
        node_id = str(event.get("node_id", ""))
        if node_id:
            requests_map[channel_id].add(node_id)
        return

    if op == "approve":
        target_node_id = str(event.get("target_node_id", ""))
        if target_node_id:
            requests_map[channel_id].discard(target_node_id)
            members_map[channel_id].add(target_node_id)


# ---------------------------------------------------------------------------
# Circle name events (CIRCLE_NAME_EVT) — gossip a human-friendly circle alias
# ---------------------------------------------------------------------------

def make_circle_name_message(state: State, circle_id: str, name: str) -> Optional[ChatMessage]:
    """Build a CIRCLE_NAME_EVT control message to gossip a friendly circle name."""
    circle = state.circles.get(circle_id)
    if not circle:
        return None
    created = now_ts()
    msg_id = sha256_hex(
        f"{state.node.node_id}|{created}|{secrets.token_hex(8)}".encode("utf-8")
    )[:32]
    event = {"t": "CIRCLE_NAME_EVT", "circle_id": circle_id, "name": name}
    text = json.dumps(event, separators=(",", ":"), sort_keys=True)
    msg = ChatMessage(
        msg_id=msg_id,
        circle_id=circle_id,
        channel_id=CONTROL_CHANNEL_ID,
        author_node_id=state.node.node_id,
        display_name=state.node.display_name,
        created_ts=created,
        text=text,
    )
    msg.mac = make_message_mac(circle.secret_hex, msg)
    return msg


def parse_circle_name_event(text: str) -> Optional[Dict[str, Any]]:
    try:
        data = json.loads(text)
    except Exception:
        return None
    if not isinstance(data, dict):
        return None
    if data.get("t") != "CIRCLE_NAME_EVT":
        return None
    if not data.get("name") or not data.get("circle_id"):
        return None
    return data


def apply_circle_name_event(state: State, circle_id: str, event: Dict[str, Any]) -> bool:
    """Apply a CIRCLE_NAME_EVT. Returns True if the circle name was updated.

    First-to-name-wins: the local name takes precedence over any gossiped one.
    """
    circle = state.circles.get(circle_id)
    if not circle:
        return False
    name = str(event.get("name", "")).strip()[:40]
    if not name:
        return False
    if circle.name == name:  # unchanged
        return False
    circle.name = name
    return True


# ---------------------------------------------------------------------------
# Anchor announce events (ANCHOR_ANNOUNCE) — gossip anchor capability
# ---------------------------------------------------------------------------

def make_anchor_announce_message(state: State, circle_id: str) -> Optional[ChatMessage]:
    """Build an ANCHOR_ANNOUNCE control message to gossip this node's anchor capability."""
    circle = state.circles.get(circle_id)
    if not circle:
        return None
    created = now_ts()
    msg_id = sha256_hex(
        f"{state.node.node_id}|{created}|{secrets.token_hex(8)}".encode("utf-8")
    )[:32]
    event: Dict[str, Any] = {
        "t": "ANCHOR_ANNOUNCE",
        "node_id": state.node.node_id,
        "capabilities": {
            "can_anchor": state.node.can_anchor,
            "public_reachable": state.node.public_reachable,
            "is_mobile": state.node.is_mobile,
        },
        "announced_at": created,
    }
    text = json.dumps(event, separators=(",", ":"), sort_keys=True)
    msg = ChatMessage(
        msg_id=msg_id,
        circle_id=circle_id,
        channel_id=CONTROL_CHANNEL_ID,
        author_node_id=state.node.node_id,
        display_name=state.node.display_name,
        created_ts=created,
        text=text,
    )
    msg.mac = make_message_mac(circle.secret_hex, msg)
    return msg


def parse_anchor_announce_event(text: str) -> Optional[Dict[str, Any]]:
    try:
        data = json.loads(text)
    except Exception:
        return None
    if not isinstance(data, dict):
        return None
    if data.get("t") != "ANCHOR_ANNOUNCE":
        return None
    if not data.get("node_id"):
        return None
    return data


def apply_anchor_announce_event(state: State, circle_id: str, event: Dict[str, Any]) -> bool:
    """Apply an ANCHOR_ANNOUNCE event to state.anchor_records.

    Returns True if anchor_records was updated.
    """
    from .models import AnchorRecord

    node_id = str(event.get("node_id", "")).strip()
    if not node_id:
        return False

    caps = event.get("capabilities", {})
    if not isinstance(caps, dict):
        caps = {}
    announced_at = int(event.get("announced_at", 0) or 0)
    now = now_ts()

    circle_anchors = state.anchor_records.setdefault(circle_id, {})
    existing = circle_anchors.get(node_id)

    # Always refresh last_seen; only replace the record if this announcement is newer.
    if existing:
        existing.last_seen_ts = now
        if existing.announced_at >= announced_at:
            return False

    circle_anchors[node_id] = AnchorRecord(
        node_id=node_id,
        capabilities={
            "can_anchor": bool(caps.get("can_anchor", False)),
            "public_reachable": bool(caps.get("public_reachable", False)),
            "is_mobile": bool(caps.get("is_mobile", False)),
        },
        announced_at=announced_at,
        last_seen_ts=now,
    )
    return True
