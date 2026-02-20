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
    if op not in {"create", "join", "request", "approve", "leave"}:
        return None
    return data


# ---------------------------------------------------------------------------
# Circle name events
# ---------------------------------------------------------------------------

def make_circle_name_message(state: State, circle_id: str, name: str) -> Optional[ChatMessage]:
    """Create a control-channel message that announces a circle's friendly name."""
    event = {
        "t": "CIRCLE_NAME_EVT",
        "circle_id": circle_id,
        "name": name.strip(),
        "actor_node_id": state.node.node_id,
    }
    return make_channel_event_message(state, circle_id, event)


def parse_circle_name_event(text: str) -> Optional[Dict[str, Any]]:
    try:
        data = json.loads(text)
    except Exception:
        return None
    if not isinstance(data, dict):
        return None
    if data.get("t") != "CIRCLE_NAME_EVT":
        return None
    if not data.get("name"):
        return None
    return data


def apply_circle_name_event(state: State, circle_id: str, event: Dict[str, Any]) -> bool:
    """Apply a CIRCLE_NAME_EVT to local state.

    Accepts the gossiped name only when the local circle has no name yet,
    so each peer's deliberate local rename is never overwritten by gossip.
    Returns True when the name was changed.
    """
    name = str(event.get("name", "")).strip()
    if not name:
        return False
    circle = state.circles.get(circle_id)
    if circle and not circle.name:
        circle.name = name
        return True
    return False


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
