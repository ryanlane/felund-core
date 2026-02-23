"""Peer anchor system — ranking, selection, and store helpers.

An anchor node is a publicly-reachable peer that is willing to store
encrypted message envelopes on behalf of the circle.  The anchor cannot
decrypt the envelopes (it doesn't have the circle secret), so it acts as a
blind store-and-forward relay — similar to the hosted relay but peer-operated.

Selection policy (Policy 1):
  score = public_reachable*8 + can_anchor*4 + (not is_mobile)*2 + node_id_tiebreak
  Hysteresis: keep the current anchor for ANCHOR_COOLDOWN_S before switching,
  unless it goes stale (no announcement for ANCHOR_STALENESS_S).

Store policy (Policy 3):
  - Drop envelopes older than ANCHOR_MAX_AGE_S (24 h)
  - Cap at ANCHOR_MAX_MSGS per circle (drop oldest first)
  - Cap at ANCHOR_MAX_BYTES total per circle (drop oldest first)
"""

from __future__ import annotations

import json
import time
from typing import Dict, List, Optional, Tuple

from .models import AnchorRecord, State

# ── Hysteresis constants ──────────────────────────────────────────────────────

ANCHOR_COOLDOWN_S = 60   # minimum seconds before switching to a different anchor
ANCHOR_STALENESS_S = 20  # seconds without announcement before anchor is considered stale

# ── Store retention constants ─────────────────────────────────────────────────

ANCHOR_MAX_AGE_S = 24 * 3600      # 24 hours
ANCHOR_MAX_MSGS = 500              # per circle
ANCHOR_MAX_BYTES = 50 * 1024 * 1024  # 50 MB per circle


# ── Scoring ───────────────────────────────────────────────────────────────────

def _score_anchor(record: AnchorRecord, now: int) -> float:
    """Compute a numeric score for a candidate anchor record.

    Returns -1.0 if the record is stale (should be excluded).
    Higher score → better anchor.
    """
    if now - record.last_seen_ts > ANCHOR_STALENESS_S:
        return -1.0  # stale; exclude

    caps = record.capabilities
    score = 0.0
    if caps.get("public_reachable"):
        score += 8.0
    if caps.get("can_anchor"):
        score += 4.0
    if not caps.get("is_mobile"):
        score += 2.0
    # Deterministic tiebreak using node_id hash (normalised to [0, 1))
    score += (hash(record.node_id) % (1 << 32)) / (1 << 32)
    return score


# ── Ranking ───────────────────────────────────────────────────────────────────

def rank_anchor_candidates(state: State, circle_id: str) -> List[str]:
    """Return node_ids of anchor candidates for *circle_id*, best first.

    Only includes nodes that have ``can_anchor=True`` and are not stale.
    """
    circle_anchors = state.anchor_records.get(circle_id, {})
    now = int(time.time())
    scored: List[Tuple[float, str]] = []
    for node_id, record in circle_anchors.items():
        if not record.capabilities.get("can_anchor"):
            continue
        s = _score_anchor(record, now)
        if s >= 0.0:
            scored.append((s, node_id))
    scored.sort(key=lambda x: x[0], reverse=True)
    return [nid for _, nid in scored]


# ── Selection with hysteresis ─────────────────────────────────────────────────

def get_current_anchor(
    state: State,
    circle_id: str,
    current_anchor: Optional[str],
    current_anchor_ts: int,
) -> Optional[str]:
    """Return the best anchor for *circle_id* applying cooldown hysteresis.

    Args:
        state: application state (reads anchor_records)
        circle_id: target circle
        current_anchor: previously selected anchor node_id, or None
        current_anchor_ts: unix timestamp when the current anchor was selected

    Returns:
        The node_id of the selected anchor, or None if no valid candidate.
    """
    candidates = rank_anchor_candidates(state, circle_id)
    if not candidates:
        return None

    best = candidates[0]
    now = int(time.time())

    # Stick with the current anchor if it's still a valid candidate and
    # the cooldown window hasn't expired yet.
    if (
        current_anchor
        and current_anchor in candidates
        and (now - current_anchor_ts) < ANCHOR_COOLDOWN_S
    ):
        return current_anchor

    return best


# ── In-memory anchor store ────────────────────────────────────────────────────
#
# anchor_store: Dict[circle_id, Dict[msg_id, envelope_dict]]
#
# Each envelope_dict has the same shape as the relay message format:
#   {"msg_id": str, "circle_id": str, "channel_id": str,
#    "author_node_id": str, "created_ts": int, "enc": {...}}
#
# The anchor node stores ciphertext-only: it never has the circle secret,
# so it cannot decrypt the "enc" payload.


def store_anchor_envelope(
    anchor_store: Dict[str, Dict[str, dict]],
    circle_id: str,
    msg_id: str,
    envelope: dict,
) -> None:
    """Store an encrypted envelope in the anchor's in-memory store.

    No-op if *msg_id* is already present (idempotent push).
    """
    circle_store = anchor_store.setdefault(circle_id, {})
    if msg_id not in circle_store:
        circle_store[msg_id] = envelope


def prune_anchor_store(
    anchor_store: Dict[str, Dict[str, dict]],
    circle_id: str,
) -> None:
    """Prune the anchor store for *circle_id* per Policy 3.

    Drops envelopes older than ANCHOR_MAX_AGE_S, then caps by count, then
    caps by total serialised byte size (oldest-first in both cases).
    """
    circle_store = anchor_store.get(circle_id)
    if not circle_store:
        return

    now = int(time.time())

    # 1. Drop age-expired envelopes.
    stale = [
        mid for mid, env in circle_store.items()
        if now - int(env.get("created_ts", 0)) > ANCHOR_MAX_AGE_S
    ]
    for mid in stale:
        del circle_store[mid]

    # 2. Cap by message count.
    if len(circle_store) > ANCHOR_MAX_MSGS:
        oldest_first = sorted(
            circle_store, key=lambda mid: circle_store[mid].get("created_ts", 0)
        )
        for mid in oldest_first[:len(circle_store) - ANCHOR_MAX_MSGS]:
            del circle_store[mid]

    # 3. Cap by total serialised size.
    def _env_bytes(env: dict) -> int:
        return len(json.dumps(env, separators=(",", ":")).encode())

    total = sum(_env_bytes(env) for env in circle_store.values())
    if total > ANCHOR_MAX_BYTES:
        oldest_first = sorted(
            circle_store, key=lambda mid: circle_store[mid].get("created_ts", 0)
        )
        for mid in oldest_first:
            if total <= ANCHOR_MAX_BYTES:
                break
            env = circle_store.pop(mid)
            total -= _env_bytes(env)
