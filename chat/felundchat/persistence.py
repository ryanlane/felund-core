from __future__ import annotations

import dataclasses
import json

from felundchat.config import (
    APP_DIR,
    MESSAGE_MAX_AGE_S,
    MAX_MESSAGES_PER_CIRCLE,
    STATE_FILE,
)
from felundchat.models import (
    ChatMessage,
    Circle,
    NodeConfig,
    Peer,
    State,
    now_ts,
)
from felundchat.transport import detect_local_ip


def ensure_app_dir() -> None:
    APP_DIR.mkdir(parents=True, exist_ok=True)


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
    if not STATE_FILE.exists():
        return State.default(bind="0.0.0.0", port=9999)

    data = json.loads(STATE_FILE.read_text(encoding="utf-8"))
    node = NodeConfig(**data["node"])

    circles = {cid: Circle(**c) for cid, c in data.get("circles", {}).items()}
    peers = {pid: Peer(**p) for pid, p in data.get("peers", {}).items()}
    circle_members = {cid: set(v) for cid, v in data.get("circle_members", {}).items()}
    messages = {}
    for mid, m in data.get("messages", {}).items():
        messages[mid] = ChatMessage(**m)

    state = State(
        node=node,
        circles=circles,
        peers=peers,
        circle_members=circle_members,
        messages=messages,
    )
    if not state.node.bind or state.node.bind == "0.0.0.0":
        state.node.bind = detect_local_ip()
    if not state.node.display_name:
        state.node.display_name = "anon"
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
    }
    tmp = STATE_FILE.with_suffix(".tmp")
    tmp.write_text(json.dumps(data, indent=2, sort_keys=True), encoding="utf-8")
    tmp.replace(STATE_FILE)
