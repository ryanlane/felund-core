"""Felund Relay WS Service — drop-in replacement for api/php/rendezvous.php
that adds WebSocket push notification for real-time message delivery.

All HTTP endpoints are API-compatible with the PHP server, so any client can
point its rendezvous_base at this service without code changes.  The WS
endpoint is the new addition.

Usage:
    pip install -r relay_requirements.txt
    python relay_ws.py --host 0.0.0.0 --port 8765 --db data/felund.sqlite

WebSocket protocol (server → client):
    {"t": "MESSAGES", "messages": [...], "server_time": <unix-ts>}
    {"t": "PING"}

WebSocket protocol (client → server):
    {"t": "PONG"}
"""

from __future__ import annotations

import argparse
import asyncio
import json
import os
import time
from collections import defaultdict
from pathlib import Path
from typing import Dict, Set

import aiohttp
import aiosqlite
from aiohttp import web

VERSION = "0.4.0"
MAX_ENDPOINTS = 16
MAX_LIMIT = 200
PING_INTERVAL_S = 15
WS_BUFFER_S = 120  # seconds of messages to send to new WS subscribers on connect

# Signal TTLs / limits
SIGNAL_TTL_CANDIDATE_S = 60
SIGNAL_TTL_OFFER_ANSWER_S = 120
SIGNAL_RATE_LIMIT = int(os.environ.get("FELUND_SIGNAL_RATE_LIMIT", "20"))
SIGNAL_RATE_WINDOW_S = float(os.environ.get("FELUND_SIGNAL_RATE_WINDOW_S", "10.0"))
SIGNAL_POLL_LIMIT = 200  # max rows returned per GET /v1/signal

CLEANUP_INTERVAL_S = 60  # how often the background cleanup task runs

# ── In-memory rooms: circle_hint → set of live WebSocket connections ──────────

_rooms: Dict[str, Set[web.WebSocketResponse]] = defaultdict(set)

# ── WebSocket → node_id reverse map (used to suppress self-echo on broadcast) ─

_ws_nodes: Dict[web.WebSocketResponse, str] = {}

# ── In-memory rate limiter for signaling ──────────────────────────────────────
# node_id → (request_count, window_start)
_signal_rate: Dict[str, tuple] = {}


def _log(message: str) -> None:
    ts = time.strftime("%Y-%m-%d %H:%M:%S", time.gmtime())
    print(f"[relay_ws {ts} UTC] {message}")


# ── CORS middleware ───────────────────────────────────────────────────────────

CORS_HEADERS = {
    "Access-Control-Allow-Origin": "*",
    "Access-Control-Allow-Methods": "GET, POST, DELETE, OPTIONS",
    "Access-Control-Allow-Headers": "Content-Type, X-Felund-Node",
}


@web.middleware
async def cors_middleware(request: web.Request, handler):  # type: ignore[type-arg]
    if request.method == "OPTIONS":
        return web.Response(status=204, headers=CORS_HEADERS)  # type: ignore[arg-type]
    response = await handler(request)
    # WebSocketResponse headers are already sent after prepare(); skip those.
    if not isinstance(response, web.WebSocketResponse):
        response.headers.update(CORS_HEADERS)
    return response


# ── DB helpers ────────────────────────────────────────────────────────────────

def _db(request: web.Request) -> aiosqlite.Connection:
    return request.app["db"]  # type: ignore[return-value]


async def _init_db(db: aiosqlite.Connection) -> None:
    await db.execute("""
        CREATE TABLE IF NOT EXISTS presence (
            circle_hint  TEXT    NOT NULL,
            node_id      TEXT    NOT NULL,
            endpoints    TEXT    NOT NULL DEFAULT '[]',
            capabilities TEXT    NOT NULL DEFAULT '{}',
            observed_at  INTEGER NOT NULL,
            expires_at   INTEGER NOT NULL,
            PRIMARY KEY (circle_hint, node_id)
        )
    """)
    await db.execute("CREATE INDEX IF NOT EXISTS idx_circle  ON presence (circle_hint)")
    await db.execute("CREATE INDEX IF NOT EXISTS idx_expires ON presence (expires_at)")

    await db.execute("""
        CREATE TABLE IF NOT EXISTS relay_messages (
            msg_id      TEXT    NOT NULL PRIMARY KEY,
            circle_hint TEXT    NOT NULL,
            payload     TEXT    NOT NULL,
            created_ts  INTEGER NOT NULL,
            stored_at   INTEGER NOT NULL DEFAULT 0,
            expires_at  INTEGER NOT NULL
        )
    """)
    await db.execute(
        "CREATE INDEX IF NOT EXISTS idx_rmsg_stored ON relay_messages (circle_hint, stored_at)"
    )
    await db.execute(
        "CREATE INDEX IF NOT EXISTS idx_rmsg_exp ON relay_messages (expires_at)"
    )

    await db.execute("""
        CREATE TABLE IF NOT EXISTS signal_messages (
            id          INTEGER PRIMARY KEY AUTOINCREMENT,
            session_id  TEXT    NOT NULL,
            from_node   TEXT    NOT NULL,
            to_node     TEXT    NOT NULL,
            type        TEXT    NOT NULL,
            payload     TEXT    NOT NULL,
            created_at  INTEGER NOT NULL,
            expires_at  INTEGER NOT NULL
        )
    """)
    await db.execute(
        "CREATE INDEX IF NOT EXISTS idx_sig_to ON signal_messages (to_node, id)"
    )
    await db.execute(
        "CREATE INDEX IF NOT EXISTS idx_sig_exp ON signal_messages (expires_at)"
    )
    await db.commit()


def _check_signal_rate(node_id: str) -> bool:
    """Return True if the request is within rate limits, False if throttled."""
    now = time.time()
    entry = _signal_rate.get(node_id)
    if entry is not None:
        count, window_start = entry
        if now - window_start < SIGNAL_RATE_WINDOW_S:
            if count >= SIGNAL_RATE_LIMIT:
                return False
            _signal_rate[node_id] = (count + 1, window_start)
        else:
            _signal_rate[node_id] = (1, now)
    else:
        _signal_rate[node_id] = (1, now)
    return True


# ── Response helpers ──────────────────────────────────────────────────────────

def _ok(data: dict, status: int = 200) -> web.Response:
    return web.Response(
        status=status,
        content_type="application/json",
        body=json.dumps(data, separators=(",", ":"), ensure_ascii=False),
    )


def _err(message: str, status: int = 400) -> web.Response:
    return web.Response(
        status=status,
        content_type="application/json",
        body=json.dumps({"ok": False, "detail": message}),
    )


# ── Route: health ─────────────────────────────────────────────────────────────

async def route_health(request: web.Request) -> web.Response:
    return _ok({"ok": True, "version": VERSION, "time": int(time.time())})


# ── Route: register presence ──────────────────────────────────────────────────

async def route_register(request: web.Request) -> web.Response:
    try:
        data: dict = await request.json()
    except Exception:
        return _err("Invalid JSON body")

    node_id = data.get("node_id", "")
    circle_hint = data.get("circle_hint", "")
    if not isinstance(node_id, str) or len(node_id) < 8:
        return _err("node_id must be a string (8+ chars)")
    if not isinstance(circle_hint, str) or len(circle_hint) < 8:
        return _err("circle_hint must be a string (8+ chars)")

    ttl_s = data.get("ttl_s", 120)
    ttl_s = min(max(int(ttl_s) if isinstance(ttl_s, (int, float)) else 120, 30), 600)

    endpoints = json.dumps(data.get("endpoints", []))
    capabilities = json.dumps(
        data.get("capabilities", {"relay": False, "transport": ["tcp"]})
    )
    now = int(time.time())
    expires_at = now + ttl_s

    db = _db(request)
    await db.execute(
        """INSERT OR REPLACE INTO presence
           (circle_hint, node_id, endpoints, capabilities, observed_at, expires_at)
           VALUES (?, ?, ?, ?, ?, ?)""",
        (circle_hint, node_id, endpoints, capabilities, now, expires_at),
    )
    await db.commit()

    remote_ip = (
        request.headers.get("X-Forwarded-For", "").split(",")[0].strip()
        or request.headers.get("X-Real-IP", "")
        or (request.remote or "")
    ).strip()
    observed_host = (
        {"host": remote_ip, "family": "ipv6" if ":" in remote_ip else "ipv4"}
        if remote_ip
        else None
    )

    return _ok({
        "ok": True,
        "server_time": now,
        "expires_at": expires_at,
        "observed_host": observed_host,
    })


# ── Route: unregister presence ────────────────────────────────────────────────

async def route_unregister(request: web.Request) -> web.Response:
    try:
        data: dict = await request.json()
    except Exception:
        return _err("Invalid JSON body")

    node_id = data.get("node_id", "")
    circle_hint = data.get("circle_hint", "")
    if not isinstance(node_id, str) or not isinstance(circle_hint, str):
        return _err("node_id and circle_hint required")

    db = _db(request)
    await db.execute(
        "DELETE FROM presence WHERE circle_hint = ? AND node_id = ?",
        (circle_hint, node_id),
    )
    await db.commit()
    return _ok({"ok": True, "removed": True})


# ── Route: peer lookup ────────────────────────────────────────────────────────

async def route_peers(request: web.Request) -> web.Response:
    circle_hint = request.rel_url.query.get("circle_hint", "")
    if len(circle_hint) < 8:
        return _err("circle_hint required (8+ chars)")

    limit_raw = request.rel_url.query.get("limit", "50")
    limit = min(max(int(limit_raw) if limit_raw.isdigit() else 50, 1), MAX_LIMIT)
    exclude_node = request.headers.get("X-Felund-Node")

    now = int(time.time())
    db = _db(request)

    if exclude_node:
        async with db.execute(
            "SELECT * FROM presence"
            " WHERE circle_hint = ? AND expires_at > ? AND node_id != ?"
            " ORDER BY observed_at DESC LIMIT ?",
            (circle_hint, now, exclude_node, limit),
        ) as cur:
            rows = await cur.fetchall()
    else:
        async with db.execute(
            "SELECT * FROM presence"
            " WHERE circle_hint = ? AND expires_at > ?"
            " ORDER BY observed_at DESC LIMIT ?",
            (circle_hint, now, limit),
        ) as cur:
            rows = await cur.fetchall()

    peers = [
        {
            "node_id": row["node_id"],
            "circle_hint": row["circle_hint"],
            "endpoints": json.loads(row["endpoints"]),
            "capabilities": json.loads(row["capabilities"]),
            "observed_at": row["observed_at"],
            "expires_at": row["expires_at"],
        }
        for row in rows
    ]
    return _ok({"ok": True, "peers": peers})


# ── Route: push messages ──────────────────────────────────────────────────────

async def route_messages_post(request: web.Request) -> web.Response:
    try:
        data: dict = await request.json()
    except Exception:
        return _err("Invalid JSON body")

    circle_hint = data.get("circle_hint", "")
    if not isinstance(circle_hint, str) or len(circle_hint) < 8:
        return _err("circle_hint required")

    messages = data.get("messages", [])
    if not isinstance(messages, list) or len(messages) > 50:
        return _err("messages must be an array (max 50)")

    now = int(time.time())
    expires = now + 30 * 24 * 3600
    db = _db(request)

    stored = 0
    valid: list = []
    for i, msg in enumerate(messages):
        if not isinstance(msg, dict):
            return _err(f"messages[{i}] must be an object")
        msg_id = msg.get("msg_id", "")
        if not isinstance(msg_id, str) or len(msg_id) < 8 or len(msg_id) > 64:
            return _err(f"messages[{i}].msg_id is invalid")
        created_ts = msg.get("created_ts", 0)
        if not isinstance(created_ts, int) or created_ts < 0:
            return _err(f"messages[{i}].created_ts must be a non-negative integer")

        payload = json.dumps(msg, separators=(",", ":"), ensure_ascii=False)
        cur = await db.execute(
            "INSERT OR IGNORE INTO relay_messages"
            " (msg_id, circle_hint, payload, created_ts, stored_at, expires_at)"
            " VALUES (?, ?, ?, ?, ?, ?)",
            (msg_id, circle_hint, payload, created_ts, now, expires),
        )
        if cur.rowcount > 0:
            stored += 1
            valid.append(msg)

    await db.commit()

    # Broadcast to live WS subscribers, skipping the sender's own connection
    # (identified by X-Felund-Node) to avoid redundant self-echo.
    if valid and circle_hint in _rooms:
        sender_node = request.headers.get("X-Felund-Node", "")
        frame = json.dumps({"t": "MESSAGES", "messages": valid, "server_time": now})
        dead: Set[web.WebSocketResponse] = set()
        for ws in list(_rooms[circle_hint]):
            if sender_node and _ws_nodes.get(ws) == sender_node:
                continue
            try:
                await ws.send_str(frame)
            except Exception:
                dead.add(ws)
        _rooms[circle_hint] -= dead

    return _ok({"ok": True, "stored": stored, "server_time": now})


# ── Route: pull messages ──────────────────────────────────────────────────────

async def route_messages_get(request: web.Request) -> web.Response:
    circle_hint = request.rel_url.query.get("circle_hint", "")
    if len(circle_hint) < 8:
        return _err("circle_hint required")

    since_raw = request.rel_url.query.get("since", "0")
    limit_raw = request.rel_url.query.get("limit", "200")
    since = int(since_raw) if since_raw.lstrip("-").isdigit() else 0
    limit = min(max(int(limit_raw) if limit_raw.isdigit() else 200, 1), 500)

    now = int(time.time())
    db = _db(request)

    async with db.execute(
        "SELECT payload FROM relay_messages"
        " WHERE circle_hint = ? AND stored_at > ? AND expires_at > ?"
        " ORDER BY stored_at ASC, msg_id ASC LIMIT ?",
        (circle_hint, since, now, limit),
    ) as cur:
        rows = await cur.fetchall()

    messages = []
    for row in rows:
        try:
            decoded = json.loads(row["payload"])
            if isinstance(decoded, dict):
                messages.append(decoded)
        except Exception:
            pass

    return _ok({"ok": True, "messages": messages, "server_time": now})


# ── Route: WebRTC signaling POST ─────────────────────────────────────────────

async def route_signal_post(request: web.Request) -> web.Response:
    try:
        data: dict = await request.json()
    except Exception:
        return _err("Invalid JSON body")

    session_id = data.get("session_id", "")
    from_node = data.get("from_node_id", "")
    to_node = data.get("to_node_id", "")
    circle_hint = data.get("circle_hint", "")
    sig_type = data.get("type", "")
    payload = data.get("payload", "")

    if not isinstance(session_id, str) or not (8 <= len(session_id) <= 256):
        return _err("session_id must be a string (8–256 chars)")
    if not isinstance(from_node, str) or len(from_node) < 8:
        return _err("from_node_id required (8+ chars)")
    if not isinstance(to_node, str) or len(to_node) < 8:
        return _err("to_node_id required (8+ chars)")
    if not isinstance(circle_hint, str) or len(circle_hint) < 8:
        return _err("circle_hint required (8+ chars)")
    _VALID_SIGNAL_TYPES = {
        "offer", "answer", "candidate",
        "media-offer", "media-answer", "media-candidate",
    }
    if sig_type not in _VALID_SIGNAL_TYPES:
        return _err("type must be offer, answer, candidate, media-offer, media-answer, or media-candidate")
    if not isinstance(payload, str) or len(payload) > 65536:
        return _err("payload must be a string (max 64 KB)")

    if not _check_signal_rate(from_node):
        _log(f"signal POST 429 from={from_node[:8]} session={session_id[:16]} type={sig_type}")
        resp = _err(
            f"Rate limit exceeded — retry after {int(SIGNAL_RATE_WINDOW_S)}s", status=429
        )
        resp.headers["Retry-After"] = str(int(SIGNAL_RATE_WINDOW_S))
        return resp

    ttl_raw = data.get("ttl_s", None)
    if sig_type in ("candidate", "media-candidate"):
        max_ttl = SIGNAL_TTL_CANDIDATE_S
    else:
        max_ttl = SIGNAL_TTL_OFFER_ANSWER_S
    if isinstance(ttl_raw, (int, float)):
        ttl_s = min(max(int(ttl_raw), 10), max_ttl)
    else:
        ttl_s = max_ttl

    now = int(time.time())
    expires_at = now + ttl_s

    db = _db(request)
    await db.execute(
        """INSERT INTO signal_messages
           (session_id, from_node, to_node, type, payload, created_at, expires_at)
           VALUES (?, ?, ?, ?, ?, ?, ?)""",
        (session_id, from_node, to_node, sig_type, payload, now, expires_at),
    )
    await db.commit()

    _log(
        f"signal POST ok from={from_node[:8]} to={to_node[:8]} type={sig_type} ttl={ttl_s}s"
    )
    return _ok({"ok": True, "server_time": now})


# ── Route: WebRTC signaling GET ───────────────────────────────────────────────

async def route_signal_get(request: web.Request) -> web.Response:
    to_node = request.rel_url.query.get("to_node_id", "")
    if len(to_node) < 8:
        return _err("to_node_id required (8+ chars)")

    since_raw = request.rel_url.query.get("since_id", "0")
    since_id = int(since_raw) if since_raw.lstrip("-").isdigit() else 0

    session_id = request.rel_url.query.get("session_id", "")

    now = int(time.time())
    db = _db(request)

    if session_id:
        async with db.execute(
            "SELECT id, session_id, from_node, to_node, type, payload, created_at"
            " FROM signal_messages"
            " WHERE to_node = ? AND session_id = ? AND id > ? AND expires_at > ?"
            " ORDER BY id ASC LIMIT ?",
            (to_node, session_id, since_id, now, SIGNAL_POLL_LIMIT),
        ) as cur:
            rows = await cur.fetchall()
    else:
        async with db.execute(
            "SELECT id, session_id, from_node, to_node, type, payload, created_at"
            " FROM signal_messages"
            " WHERE to_node = ? AND id > ? AND expires_at > ?"
            " ORDER BY id ASC LIMIT ?",
            (to_node, since_id, now, SIGNAL_POLL_LIMIT),
        ) as cur:
            rows = await cur.fetchall()

    signals = [
        {
            "id": row["id"],
            "session_id": row["session_id"],
            "from_node": row["from_node"],
            "to_node": row["to_node"],
            "type": row["type"],
            "payload": row["payload"],
            "created_at": row["created_at"],
        }
        for row in rows
    ]
    _log(
        f"signal GET to={to_node[:8]} since={since_id} count={len(signals)}"
        + (f" session={session_id[:16]}" if session_id else "")
    )
    return _ok({"ok": True, "signals": signals, "server_time": now})


# ── Route: WebSocket subscription ─────────────────────────────────────────────

async def route_ws(request: web.Request) -> web.WebSocketResponse:
    circle_hint = request.rel_url.query.get("circle_hint", "")
    node_id = request.rel_url.query.get("node_id", "")
    if len(circle_hint) < 8 or not node_id:
        raise web.HTTPBadRequest(text="circle_hint and node_id required")

    ws = web.WebSocketResponse(heartbeat=PING_INTERVAL_S)
    await ws.prepare(request)

    _rooms[circle_hint].add(ws)
    _ws_nodes[ws] = node_id
    _log(f"ws connect circle={circle_hint[:8]} node={node_id[:8]}")

    # Immediately send buffered messages from the last WS_BUFFER_S seconds
    # so the client catches up without a separate HTTP pull.
    db = _db(request)
    now = int(time.time())
    buf_since = now - WS_BUFFER_S
    async with db.execute(
        "SELECT payload FROM relay_messages"
        " WHERE circle_hint = ? AND stored_at > ? AND expires_at > ?"
        " ORDER BY stored_at ASC, msg_id ASC LIMIT 200",
        (circle_hint, buf_since, now),
    ) as cur:
        rows = await cur.fetchall()

    buffered = []
    for row in rows:
        try:
            decoded = json.loads(row["payload"])
            if isinstance(decoded, dict):
                buffered.append(decoded)
        except Exception:
            pass

    if buffered:
        await ws.send_str(
            json.dumps({"t": "MESSAGES", "messages": buffered, "server_time": now})
        )

    try:
        async for msg in ws:
            if msg.type in (aiohttp.WSMsgType.ERROR, aiohttp.WSMsgType.CLOSE):
                break
            # PONG and any other client frames are silently accepted.
    finally:
        _rooms[circle_hint].discard(ws)
        if not _rooms[circle_hint]:
            del _rooms[circle_hint]
        _ws_nodes.pop(ws, None)
        _log(f"ws disconnect circle={circle_hint[:8]} node={node_id[:8]}")

    return ws


# ── Background cleanup task ───────────────────────────────────────────────────

async def _cleanup_task(app: web.Application) -> None:
    """Prune expired DB rows and stale rate-limit buckets every CLEANUP_INTERVAL_S.

    Removing these from the hot-path write handlers avoids redundant DELETE
    queries on every registration, message post, and signal post.
    """
    while True:
        await asyncio.sleep(CLEANUP_INTERVAL_S)
        try:
            now = int(time.time())
            db: aiosqlite.Connection = app["db"]
            await db.execute("DELETE FROM presence WHERE expires_at <= ?", (now,))
            await db.execute("DELETE FROM relay_messages WHERE expires_at <= ?", (now,))
            await db.execute("DELETE FROM signal_messages WHERE expires_at <= ?", (now,))
            await db.commit()

            # Prune rate-limit buckets whose window has long since expired so
            # _signal_rate doesn't grow without bound over the server lifetime.
            cutoff = now - SIGNAL_RATE_WINDOW_S * 2
            stale = [nid for nid, (_, start) in _signal_rate.items() if start < cutoff]
            for nid in stale:
                del _signal_rate[nid]
            if stale:
                _log(f"cleanup: pruned {len(stale)} rate-limit bucket(s)")
        except asyncio.CancelledError:
            raise
        except Exception as exc:
            _log(f"cleanup task error: {exc}")


# ── App factory ───────────────────────────────────────────────────────────────

def make_app(db_path: str) -> web.Application:
    app = web.Application(middlewares=[cors_middleware])
    app["db_path"] = db_path
    app["tasks"] = set()

    async def on_startup(app: web.Application) -> None:
        db = await aiosqlite.connect(app["db_path"])
        db.row_factory = aiosqlite.Row  # type: ignore[assignment]
        await _init_db(db)
        app["db"] = db
        task = asyncio.create_task(_cleanup_task(app))
        app["tasks"].add(task)
        task.add_done_callback(app["tasks"].discard)

    async def on_cleanup(app: web.Application) -> None:
        for circle_ws in list(_rooms.values()):
            for ws in list(circle_ws):
                try:
                    await ws.close()
                except Exception:
                    pass
        _rooms.clear()
        for t in list(app["tasks"]):
            t.cancel()
        if app["tasks"]:
            await asyncio.gather(*app["tasks"], return_exceptions=True)
        await app["db"].close()

    app.on_startup.append(on_startup)
    app.on_cleanup.append(on_cleanup)

    app.router.add_get("/v1/health", route_health)
    app.router.add_post("/v1/register", route_register)
    app.router.add_delete("/v1/register", route_unregister)
    app.router.add_get("/v1/peers", route_peers)
    app.router.add_post("/v1/messages", route_messages_post)
    app.router.add_get("/v1/messages", route_messages_get)
    app.router.add_get("/v1/relay/ws", route_ws)
    app.router.add_post("/v1/signal", route_signal_post)
    app.router.add_get("/v1/signal", route_signal_get)

    return app


# ── Entry point ───────────────────────────────────────────────────────────────

if __name__ == "__main__":
    parser = argparse.ArgumentParser(description="Felund Relay WS Service")
    parser.add_argument("--host", default="0.0.0.0", help="Bind address (default: 0.0.0.0)")
    parser.add_argument("--port", type=int, default=8765, help="Listen port (default: 8765)")
    parser.add_argument(
        "--db",
        default="data/felund.sqlite",
        metavar="PATH",
        help="SQLite database path (default: data/felund.sqlite)",
    )
    args = parser.parse_args()

    Path(args.db).parent.mkdir(parents=True, exist_ok=True)
    _log(f"starting on {args.host}:{args.port} db={args.db}")
    web.run_app(make_app(args.db), host=args.host, port=args.port, print=None)
