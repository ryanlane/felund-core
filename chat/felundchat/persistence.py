from __future__ import annotations

import dataclasses
import json

import felundchat.config as _cfg
from felundchat.config import MESSAGE_MAX_AGE_S, MAX_MESSAGES_PER_CIRCLE
from felundchat.models import (
    ChatMessage,
    Channel,
    Circle,
    NodeConfig,
    Peer,
    State,
    now_ts,
)
from felundchat.transport import detect_local_ip


def _load_dataclass_strict(cls, payload: dict, label: str):
    try:
        return cls(**payload)
    except TypeError as e:
        raise ValueError(
            f"State schema mismatch in {label}: {e}. "
            f"Delete or reset {_cfg.STATE_FILE} to start fresh with the current schema."
        ) from e


def ensure_app_dir() -> None:
    _cfg.APP_DIR.mkdir(parents=True, exist_ok=True)


def prune_messages(state: State) -> None:
    now = now_ts()
    expired = [
        mid for mid, message in state.messages.items()
        if now - message.created_ts > MESSAGE_MAX_AGE_S
    ]
    for mid in expired:
        state.messages.pop(mid, None)

    for circle_id in state.circles.keys():
        circle_msgs = [m for m in state.messages.values() if m.circle_id == circle_id]
        if len(circle_msgs) <= MAX_MESSAGES_PER_CIRCLE:
            continue
        circle_msgs.sort(key=lambda message: (message.created_ts, message.msg_id))
        keep_ids = {message.msg_id for message in circle_msgs[-MAX_MESSAGES_PER_CIRCLE:]}
        drop_ids = [message.msg_id for message in circle_msgs if message.msg_id not in keep_ids]
        for mid in drop_ids:
            state.messages.pop(mid, None)


def load_state() -> State:
    ensure_app_dir()
    if not _cfg.STATE_FILE.exists():
        return State.default(bind="0.0.0.0", port=9999)

    data = json.loads(_cfg.STATE_FILE.read_text(encoding="utf-8"))
    node = _load_dataclass_strict(NodeConfig, data["node"], "node")

    circles = {
        cid: _load_dataclass_strict(Circle, c, f"circles[{cid}]")
        for cid, c in data.get("circles", {}).items()
    }
    peers = {
        pid: _load_dataclass_strict(Peer, p, f"peers[{pid}]")
        for pid, p in data.get("peers", {}).items()
    }
    circle_members = {cid: set(v) for cid, v in data.get("circle_members", {}).items()}
    channels = {
        cid: {
            channel_id: _load_dataclass_strict(Channel, channel_data, f"channels[{cid}][{channel_id}]")
            for channel_id, channel_data in circle_channels.items()
        }
        for cid, circle_channels in data.get("channels", {}).items()
    }
    channel_members = {
        cid: {channel_id: set(members) for channel_id, members in circle_map.items()}
        for cid, circle_map in data.get("channel_members", {}).items()
    }
    channel_requests = {
        cid: {channel_id: set(requests) for channel_id, requests in circle_map.items()}
        for cid, circle_map in data.get("channel_requests", {}).items()
    }
    node_display_names = {str(node_id): str(name) for node_id, name in data.get("node_display_names", {}).items()}
    messages = {}
    for mid, m in data.get("messages", {}).items():
        if "channel_id" not in m:
            m["channel_id"] = "general"
        messages[mid] = _load_dataclass_strict(ChatMessage, m, f"messages[{mid}]")

    state = State(
        node=node,
        circles=circles,
        peers=peers,
        circle_members=circle_members,
        messages=messages,
        channels=channels,
        channel_members=channel_members,
        channel_requests=channel_requests,
        node_display_names=node_display_names,
    )
    if not state.node.bind or state.node.bind == "0.0.0.0":
        state.node.bind = detect_local_ip()
    if not state.node.display_name:
        state.node.display_name = "anon"
    state.node_display_names[state.node.node_id] = state.node.display_name
    prune_messages(state)
    return state


def save_state(state: State) -> None:
    ensure_app_dir()
    prune_messages(state)
    data = {
        "node": dataclasses.asdict(state.node),
        "circles": {cid: dataclasses.asdict(c) for cid, c in state.circles.items()},
        "peers": {pid: dataclasses.asdict(p) for pid, p in state.peers.items()},
        "circle_members": {cid: sorted(list(v)) for cid, v in state.circle_members.items()},
        "messages": {mid: dataclasses.asdict(m) for mid, m in state.messages.items()},
        "channels": {
            cid: {channel_id: dataclasses.asdict(channel) for channel_id, channel in circle_channels.items()}
            for cid, circle_channels in state.channels.items()
        },
        "channel_members": {
            cid: {channel_id: sorted(list(members)) for channel_id, members in circle_map.items()}
            for cid, circle_map in state.channel_members.items()
        },
        "channel_requests": {
            cid: {channel_id: sorted(list(requests)) for channel_id, requests in circle_map.items()}
            for cid, circle_map in state.channel_requests.items()
        },
        "node_display_names": dict(state.node_display_names),
    }
    tmp = _cfg.STATE_FILE.with_suffix(".tmp")
    tmp.write_text(json.dumps(data, indent=2, sort_keys=True), encoding="utf-8")
    tmp.replace(_cfg.STATE_FILE)
