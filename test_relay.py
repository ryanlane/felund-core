#!/usr/bin/env python3
"""
Felund relay API integration test.

Simulates 1 host + 3 clients all exchanging messages through the relay server.

Tests
-----
1. Health check
2. Sequential pushes (one per node)
3. Concurrent pushes (all 4 nodes push simultaneously — stress-tests SQLite locking)
4. Full pull: every node pulls with since=0 and verifies it receives all 4 messages
5. MAC verification: each received message is verified with the shared secret
6. Cursor test: push 2 more messages, pull with a cursor set BEFORE those pushes,
   verify only the new messages are returned

Usage
-----
    python test_relay.py                          # uses https://felund.com/api
    python test_relay.py http://localhost:8000    # local dev server
    python test_relay.py https://felund.com/api --verbose
"""
from __future__ import annotations

import hashlib
import hmac as _hmac
import json
import secrets
import sys
import threading
import time
import urllib.error
import urllib.request
from dataclasses import dataclass, field
from typing import List, Optional


# ── Crypto (mirrors Python client & JS client exactly) ────────────────────────

def sha256_hex(data: bytes) -> str:
    return hashlib.sha256(data).hexdigest()


def hmac_hex(key_hex: str, message: str) -> str:
    key = bytes.fromhex(key_hex)
    return _hmac.new(key, message.encode("utf-8"), hashlib.sha256).hexdigest()


def circle_hint_for(circle_id: str) -> str:
    return sha256_hex(circle_id.encode("utf-8"))[:16]


def make_mac(
    secret_hex: str,
    msg_id: str,
    circle_id: str,
    channel_id: str,
    author_node_id: str,
    display_name: str,
    created_ts: int,
    text: str,
) -> str:
    payload = f"{msg_id}|{circle_id}|{channel_id}|{author_node_id}|{display_name}|{created_ts}|{text}"
    return hmac_hex(secret_hex, payload)


# ── HTTP helper ───────────────────────────────────────────────────────────────

def api_call(
    method: str,
    url: str,
    body: Optional[dict] = None,
    node_id: str = "",
    timeout: int = 15,
) -> dict:
    raw = json.dumps(body, separators=(",", ":")).encode() if body is not None else None
    headers: dict = {"content-type": "application/json"}
    if node_id:
        headers["X-Felund-Node"] = node_id
    req = urllib.request.Request(url=url, data=raw, headers=headers, method=method)
    with urllib.request.urlopen(req, timeout=timeout) as resp:
        return json.loads(resp.read().decode("utf-8"))


# ── Simulated node ────────────────────────────────────────────────────────────

@dataclass
class Node:
    name: str
    node_id: str
    secret_hex: str
    circle_id: str
    hint: str
    msg_id: str
    text: str
    created_ts: int
    mac: str
    push_ok: bool = False
    push_ms: float = 0.0
    push_error: str = ""
    pull_msgs: List[dict] = field(default_factory=list)
    pull_ms: float = 0.0
    pull_error: str = ""


def make_node(name: str, secret_hex: str, circle_id: str, hint: str) -> Node:
    node_id = sha256_hex(secrets.token_bytes(16))[:24]
    created_ts = int(time.time())
    rand = secrets.token_hex(8)
    msg_id = sha256_hex(f"{node_id}|{created_ts}|{rand}".encode())[:32]
    text = f"Test message from {name} at t={created_ts}"
    mac = make_mac(secret_hex, msg_id, circle_id, "general", node_id, name, created_ts, text)
    return Node(
        name=name,
        node_id=node_id,
        secret_hex=secret_hex,
        circle_id=circle_id,
        hint=hint,
        msg_id=msg_id,
        text=text,
        created_ts=created_ts,
        mac=mac,
    )


# ── Per-node push / pull operations ──────────────────────────────────────────

def push_node(node: Node, api_base: str) -> None:
    url = f"{api_base}/v1/messages"
    body = {
        "circle_hint": node.hint,
        "messages": [{
            "msg_id": node.msg_id,
            "circle_id": node.circle_id,
            "channel_id": "general",
            "author_node_id": node.node_id,
            "display_name": node.name,
            "created_ts": node.created_ts,
            "text": node.text,
            "mac": node.mac,
        }],
    }
    t0 = time.monotonic()
    try:
        resp = api_call("POST", url, body, node_id=node.node_id)
        node.push_ms = (time.monotonic() - t0) * 1000
        if resp.get("ok"):
            node.push_ok = True
        else:
            node.push_error = f"server returned ok=false: {resp}"
    except Exception as exc:
        node.push_ms = (time.monotonic() - t0) * 1000
        node.push_error = str(exc)


def pull_node(node: Node, api_base: str, since: int = 0) -> None:
    params = f"circle_hint={node.hint}&since={since}&limit=200"
    url = f"{api_base}/v1/messages?{params}"
    t0 = time.monotonic()
    try:
        resp = api_call("GET", url, node_id=node.node_id)
        node.pull_ms = (time.monotonic() - t0) * 1000
        node.pull_msgs = resp.get("messages", [])
    except Exception as exc:
        node.pull_ms = (time.monotonic() - t0) * 1000
        node.pull_error = str(exc)


# ── Result helpers ────────────────────────────────────────────────────────────

PASS = "PASS"
FAIL = "FAIL"
WARN = "WARN"

_COLOURS = {PASS: "\033[32m", FAIL: "\033[31m", WARN: "\033[33m"}
_RESET = "\033[0m"


def coloured(status: str, text: str) -> str:
    c = _COLOURS.get(status, "")
    return f"{c}{text}{_RESET}" if sys.stdout.isatty() else text


def status_line(label: str, status: str, detail: str = "") -> None:
    tag = f"[{status}]"
    tag_col = coloured(status, tag)
    detail_str = f"  {detail}" if detail else ""
    print(f"  {tag_col:<20} {label}{detail_str}")


# ── Test sections ─────────────────────────────────────────────────────────────

def section(title: str) -> None:
    print(f"\n── {title} {'─' * max(0, 55 - len(title))}")


def run_test(api_base: str, verbose: bool = False) -> bool:
    api_base = api_base.rstrip("/")
    overall = True

    print(f"\nFelund relay integration test")
    print(f"Target: {api_base}")
    print("=" * 60)

    # ── 1. Health check ───────────────────────────────────────────
    section("1 / 6  Health check")
    try:
        h = api_call("GET", f"{api_base}/v1/health")
        version = h.get("version", "?")
        server_time = h.get("time", 0)
        status_line("GET /v1/health", PASS, f"version={version}  server_time={server_time}")
    except Exception as exc:
        status_line("GET /v1/health", FAIL, str(exc))
        print("\nCannot reach server — aborting test.")
        return False

    # ── 2. Create shared circle ────────────────────────────────────
    section("2 / 6  Create circle")
    secret_hex = secrets.token_hex(32)
    circle_id = sha256_hex(secret_hex.encode())[:24]
    hint = circle_hint_for(circle_id)
    status_line("circle created", PASS, f"circle_id={circle_id}  hint={hint}")

    nodes: List[Node] = [
        make_node("host",     secret_hex, circle_id, hint),
        make_node("client-1", secret_hex, circle_id, hint),
        make_node("client-2", secret_hex, circle_id, hint),
        make_node("client-3", secret_hex, circle_id, hint),
    ]
    for n in nodes:
        status_line(n.name, PASS, f"node={n.node_id[:14]}  msg={n.msg_id[:14]}")

    # ── 3. Sequential pushes ───────────────────────────────────────
    section("3 / 6  Sequential pushes (one at a time)")
    for n in nodes:
        push_node(n, api_base)
        n.push_ok = False  # reset so concurrent test is fresh

    seq_ok = all(not n.push_error for n in nodes)
    for n in nodes:
        st = PASS if not n.push_error else FAIL
        status_line(n.name, st, f"{n.push_ms:.0f} ms  {n.push_error or ''}")
    if not seq_ok:
        overall = False

    # Reset push state for the concurrent round
    for n in nodes:
        n.push_ok = False
        n.push_error = ""
        n.push_ms = 0.0

    # ── 4. Concurrent pushes ───────────────────────────────────────
    section("4 / 6  Concurrent pushes (all 4 nodes simultaneously)")
    threads = [threading.Thread(target=push_node, args=(n, api_base)) for n in nodes]
    t_start = time.monotonic()
    for t in threads:
        t.start()
    for t in threads:
        t.join()
    total_ms = (time.monotonic() - t_start) * 1000

    conc_ok = all(not n.push_error for n in nodes)
    for n in nodes:
        st = PASS if not n.push_error else FAIL
        status_line(n.name, st, f"{n.push_ms:.0f} ms  {n.push_error or ''}")
    status_line(
        "wall-clock total",
        PASS if conc_ok else FAIL,
        f"{total_ms:.0f} ms  (sequential would be ~{sum(n.push_ms for n in nodes):.0f} ms)",
    )
    if not conc_ok:
        overall = False

    # ── 5. Pull + MAC verification ─────────────────────────────────
    section("5 / 6  Pull & MAC verification (since=0)")
    expected_ids = {n.msg_id for n in nodes}

    pull_threads = [threading.Thread(target=pull_node, args=(n, api_base, 0)) for n in nodes]
    for t in pull_threads:
        t.start()
    for t in pull_threads:
        t.join()

    pull_overall = True
    for n in nodes:
        if n.pull_error:
            status_line(n.name, FAIL, f"pull error: {n.pull_error}")
            pull_overall = False
            overall = False
            continue

        our_msgs = [m for m in n.pull_msgs if m.get("circle_id") == circle_id]
        received_ids = {m["msg_id"] for m in our_msgs}
        missing = expected_ids - received_ids

        mac_fails: List[str] = []
        for m in our_msgs:
            if m["msg_id"] not in expected_ids:
                continue  # older msgs from prior test runs, skip
            expected_mac = make_mac(
                secret_hex,
                m["msg_id"], m["circle_id"], m["channel_id"],
                m["author_node_id"], m["display_name"], m["created_ts"], m["text"],
            )
            if expected_mac != m.get("mac", ""):
                mac_fails.append(m["msg_id"][:10])

        extra = len(our_msgs) - len(expected_ids)
        notes: List[str] = [f"{n.pull_ms:.0f} ms", f"got={len(our_msgs)}"]
        if extra > 0:
            notes.append(f"(+{extra} from prior runs — OK)")
        if missing:
            notes.append(f"MISSING={[i[:10] for i in missing]}")
        if mac_fails:
            notes.append(f"BAD_MAC={mac_fails}")

        st = FAIL if (missing or mac_fails or n.pull_error) else PASS
        if st == FAIL:
            pull_overall = False
            overall = False
        status_line(n.name, st, "  ".join(notes))

    if verbose:
        print()
        for n in nodes:
            print(f"  --- {n.name} received {len(n.pull_msgs)} message(s) ---")
            for m in n.pull_msgs:
                if m.get("circle_id") != circle_id:
                    continue
                print(f"      from={m.get('display_name','?'):12} text={m.get('text','')[:50]}")

    # ── 6. Cursor test ─────────────────────────────────────────────
    section("6 / 6  Cursor test (new messages appear after cursor)")

    # Record server time as the cursor BEFORE pushing new messages
    h2 = api_call("GET", f"{api_base}/v1/health")
    cursor = int(h2.get("time", 0))
    status_line("cursor captured", PASS, f"cursor={cursor}")

    # Wait 1 s to guarantee stored_at > cursor
    time.sleep(1)

    cursor_nodes = [
        make_node("cursor-A", secret_hex, circle_id, hint),
        make_node("cursor-B", secret_hex, circle_id, hint),
    ]
    cthreads = [threading.Thread(target=push_node, args=(cn, api_base)) for cn in cursor_nodes]
    for t in cthreads:
        t.start()
    for t in cthreads:
        t.join()

    push_status = PASS if all(not cn.push_error for cn in cursor_nodes) else FAIL
    status_line("push 2 new msgs", push_status, "  ".join(
        f"{cn.name}={cn.push_ms:.0f}ms {'ERR:'+cn.push_error if cn.push_error else 'ok'}"
        for cn in cursor_nodes
    ))
    if push_status == FAIL:
        overall = False

    expected_new = {cn.msg_id for cn in cursor_nodes}

    cursor_ok = True
    for n in nodes[:2]:  # test from two original nodes' perspectives
        pull_node(n, api_base, since=cursor)
        if n.pull_error:
            status_line(f"{n.name} (since={cursor})", FAIL, n.pull_error)
            cursor_ok = False
            continue

        our_new = {m["msg_id"] for m in n.pull_msgs if m.get("circle_id") == circle_id}
        missing_new = expected_new - our_new
        old_leaked = expected_ids & our_new  # original msgs should NOT appear

        notes = [f"got={len(n.pull_msgs)}"]
        st = PASS
        if missing_new:
            notes.append(f"MISSING_NEW={[i[:10] for i in missing_new]}")
            st = FAIL
        if old_leaked:
            # This means the cursor didn't filter — server bug
            notes.append(f"OLD_MSGS_LEAKED={[i[:10] for i in old_leaked]}")
            st = FAIL

        status_line(f"{n.name} (since={cursor})", st, "  ".join(notes))
        if st == FAIL:
            cursor_ok = False

    if not cursor_ok:
        overall = False

    # ── Summary ───────────────────────────────────────────────────
    print()
    print("=" * 60)
    final = PASS if overall else FAIL
    print(f"  Overall result: {coloured(final, final)}")
    print("=" * 60)
    print()

    if not overall:
        print("Troubleshooting tips:")
        if not conc_ok:
            print("  • Concurrent push failures often mean SQLite BUSY errors.")
            print("    Check that PRAGMA busy_timeout is set in rendezvous.php.")
            print("    On some shared hosts, SQLite file locking is unreliable —")
            print("    consider switching to a MySQL/MariaDB backend.")
        if not pull_overall:
            print("  • Missing messages on pull: check that the rendezvous server")
            print("    uses stored_at (server time) not created_ts (client time)")
            print("    for the since-cursor filter.")
        print()

    return overall


if __name__ == "__main__":
    args = [a for a in sys.argv[1:] if not a.startswith("--")]
    verbose = "--verbose" in sys.argv or "-v" in sys.argv
    target = args[0] if args else "https://felund.com/api"
    ok = run_test(target, verbose=verbose)
    sys.exit(0 if ok else 1)
