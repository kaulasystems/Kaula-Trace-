"""Spool writer + drain worker.

Hooks are in the interactive path (p99 < 50ms, must never block the agent), so
they never touch SQLite inline. Instead a hook appends one already-redacted,
already-hashed JSON line to a spool file and returns. The drain worker folds the
spool into the database later — opportunistically after each hook, and always
before any read command so queries see fresh data.

Idempotency is content-based: plugin config and repo config can double-register
a hook, so the same event fires twice. Prompts dedupe on ``(session_id,
text_hash)``; edits on ``(session_id, path, new_hash, prompt_id)``.
"""

from __future__ import annotations

import json
import os
import time
import uuid
from typing import Any, Dict, List, Optional

from . import config, db

try:  # POSIX advisory locking; degrade gracefully elsewhere.
    import fcntl
except ImportError:  # pragma: no cover - non-posix
    fcntl = None  # type: ignore


def append(event: Dict[str, Any]) -> None:
    """Spool one event as its own file, written atomically.

    Each event lands in a uniquely-named file via write-temp-then-rename. That
    keeps the hot path a single small write while making the drain race-free:
    the drain only ever sees, reads, and deletes *complete* files, so it can
    never unlink a file a concurrent hook is still writing (which would silently
    drop the event).
    """
    config.ensure_dirs()
    event.setdefault("v", 1)
    ts = event.setdefault("ts", int(time.time() * 1000))
    line = json.dumps(event, separators=(",", ":"), ensure_ascii=False)
    stem = f"{ts:013d}-{os.getpid()}-{uuid.uuid4().hex}"
    spool = config.spool_dir()
    tmp = spool / (stem + ".tmp")
    final = spool / (stem + ".jsonl")
    with open(tmp, "w", encoding="utf-8") as fh:
        fh.write(line + "\n")
    os.replace(tmp, final)


def _lock():
    """Best-effort exclusive drain lock. Returns a handle or None."""
    if fcntl is None:
        return None
    fh = open(config.lock_path(), "w")
    try:
        fcntl.flock(fh.fileno(), fcntl.LOCK_EX)
    except OSError:
        fh.close()
        return None
    return fh


def _unlock(fh) -> None:
    if fh is None:
        return
    try:
        if fcntl is not None:
            fcntl.flock(fh.fileno(), fcntl.LOCK_UN)
    finally:
        fh.close()


def drain() -> int:
    """Fold every spooled event into the database. Returns events applied.

    Events across all spool files are ordered by their capture timestamp so an
    edit binds to the prompt that was in effect when it happened.
    """
    spool = config.spool_dir()
    if not spool.exists():
        return 0
    files = sorted(p for p in spool.iterdir() if p.suffix == ".jsonl")
    if not files:
        return 0

    lock = _lock()
    try:
        events: List[Dict[str, Any]] = []
        for path in files:
            try:
                text = path.read_text(encoding="utf-8")
            except OSError:
                continue
            for raw in text.splitlines():
                raw = raw.strip()
                if not raw:
                    continue
                try:
                    events.append(json.loads(raw))
                except json.JSONDecodeError:
                    continue
        events.sort(key=lambda e: (e.get("ts", 0)))

        conn = db.connect()
        applied = 0
        try:
            for ev in events:
                if _apply(conn, ev):
                    applied += 1
            conn.commit()
        finally:
            conn.close()

        # Only remove files once their contents are committed.
        for path in files:
            try:
                os.remove(path)
            except OSError:
                pass
        return applied
    finally:
        _unlock(lock)


def _apply(conn, ev: Dict[str, Any]) -> bool:
    kind = ev.get("type")
    if kind == "session-start":
        return _apply_session_start(conn, ev)
    if kind == "prompt":
        return _apply_prompt(conn, ev)
    if kind == "edit":
        return _apply_edit(conn, ev)
    if kind == "session-end":
        return _apply_session_end(conn, ev)
    return False


def _ensure_session(conn, ev: Dict[str, Any]) -> Optional[str]:
    sid = ev.get("session_id")
    if not sid:
        return None
    row = conn.execute(
        "SELECT session_id FROM sessions WHERE session_id=?", (sid,)
    ).fetchone()
    if row:
        return sid
    conn.execute(
        "INSERT OR IGNORE INTO sessions"
        "(session_id, harness, harness_version, model, repo_root, started_at)"
        " VALUES(?,?,?,?,?,?)",
        (
            sid,
            ev.get("harness", "unknown"),
            ev.get("harness_version"),
            ev.get("model"),
            ev.get("repo_root", ""),
            ev.get("ts", 0),
        ),
    )
    return sid


def _apply_session_start(conn, ev: Dict[str, Any]) -> bool:
    sid = _ensure_session(conn, ev)
    if not sid:
        return False
    # Backfill metadata that may only appear at session-start.
    conn.execute(
        "UPDATE sessions SET "
        "harness=COALESCE(?, harness), "
        "harness_version=COALESCE(?, harness_version), "
        "model=COALESCE(?, model), "
        "repo_root=CASE WHEN repo_root='' THEN ? ELSE repo_root END "
        "WHERE session_id=?",
        (
            ev.get("harness"),
            ev.get("harness_version"),
            ev.get("model"),
            ev.get("repo_root", ""),
            sid,
        ),
    )
    return True


def _apply_prompt(conn, ev: Dict[str, Any]) -> bool:
    sid = _ensure_session(conn, ev)
    if not sid:
        return False
    text_hash = ev.get("text_hash")
    if not text_hash:
        return False
    dup = conn.execute(
        "SELECT prompt_id FROM prompts WHERE session_id=? AND text_hash=?",
        (sid, text_hash),
    ).fetchone()
    if dup:
        return False  # double-registered identical prompt
    seq_row = conn.execute(
        "SELECT COALESCE(MAX(seq), -1) + 1 AS n FROM prompts WHERE session_id=?",
        (sid,),
    ).fetchone()
    seq = seq_row["n"]
    text = ev.get("text")
    cur = conn.execute(
        "INSERT INTO prompts(session_id, seq, text, text_hash, created_at)"
        " VALUES(?,?,?,?,?)",
        (sid, seq, text, text_hash, ev.get("ts", 0)),
    )
    pid = cur.lastrowid
    if text:
        conn.execute(
            "INSERT INTO prompts_fts(text, prompt_id) VALUES(?, ?)", (text, pid)
        )
    return True


def _current_prompt_id(conn, sid: str) -> Optional[int]:
    row = conn.execute(
        "SELECT prompt_id FROM prompts WHERE session_id=? ORDER BY seq DESC LIMIT 1",
        (sid,),
    ).fetchone()
    return row["prompt_id"] if row else None


def _apply_edit(conn, ev: Dict[str, Any]) -> bool:
    sid = _ensure_session(conn, ev)
    if not sid:
        return False
    new_hash = ev.get("new_hash")
    path = ev.get("path")
    if not new_hash or not path:
        return False
    prompt_id = _current_prompt_id(conn, sid)
    dup = conn.execute(
        "SELECT edit_id FROM edits WHERE session_id=? AND path=? AND new_hash=?"
        " AND IFNULL(prompt_id,-1)=IFNULL(?,-1)",
        (sid, path, new_hash, prompt_id),
    ).fetchone()
    if dup:
        return False
    conn.execute(
        "INSERT INTO edits"
        "(session_id, prompt_id, tool, path, new_text, new_hash, line_hashes, created_at)"
        " VALUES(?,?,?,?,?,?,?,?)",
        (
            sid,
            prompt_id,
            ev.get("tool", "Edit"),
            path,
            ev.get("new_text"),
            new_hash,
            ev.get("line_hashes", "[]"),
            ev.get("ts", 0),
        ),
    )
    return True


def _apply_session_end(conn, ev: Dict[str, Any]) -> bool:
    sid = ev.get("session_id")
    if not sid:
        return False
    conn.execute(
        "UPDATE sessions SET ended_at=? WHERE session_id=? AND ended_at IS NULL",
        (ev.get("ts", 0), sid),
    )
    return True
