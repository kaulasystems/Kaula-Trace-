"""SQLite schema and connection handling.

One file at ``~/.kaula/db.sqlite`` (override ``KAULA_DB``). WAL mode is on so the
hooks' drain worker can write while the CLI reads. The schema is exactly the v0
spec's — the ``chain`` fields are deliberately absent; adding them later is a
migration, carrying them now is dead weight.
"""

from __future__ import annotations

import sqlite3
import time
from typing import Optional

from . import config

SCHEMA_VERSION = 1

_SCHEMA = """
CREATE TABLE IF NOT EXISTS sessions (
  session_id      TEXT PRIMARY KEY,
  harness         TEXT NOT NULL,
  harness_version TEXT,
  model           TEXT,
  repo_root       TEXT NOT NULL,
  started_at      INTEGER NOT NULL,
  ended_at        INTEGER
);

CREATE TABLE IF NOT EXISTS prompts (
  prompt_id   INTEGER PRIMARY KEY,
  session_id  TEXT NOT NULL REFERENCES sessions(session_id),
  seq         INTEGER NOT NULL,
  text        TEXT,
  text_hash   TEXT NOT NULL,
  created_at  INTEGER NOT NULL,
  UNIQUE(session_id, seq)
);
CREATE INDEX IF NOT EXISTS idx_prompts_hash ON prompts(session_id, text_hash);

CREATE TABLE IF NOT EXISTS edits (
  edit_id      INTEGER PRIMARY KEY,
  session_id   TEXT NOT NULL REFERENCES sessions(session_id),
  prompt_id    INTEGER REFERENCES prompts(prompt_id),
  tool         TEXT NOT NULL,
  path         TEXT NOT NULL,
  new_text     TEXT,
  new_hash     TEXT NOT NULL,
  line_hashes  TEXT NOT NULL,
  created_at   INTEGER NOT NULL
);
CREATE INDEX IF NOT EXISTS idx_edits_path ON edits(path);
CREATE INDEX IF NOT EXISTS idx_edits_session ON edits(session_id);

CREATE TABLE IF NOT EXISTS commits (
  sha          TEXT PRIMARY KEY,
  patch_id     TEXT NOT NULL,
  repo_root    TEXT NOT NULL,
  committed_at INTEGER NOT NULL
);
CREATE INDEX IF NOT EXISTS idx_commits_patch ON commits(patch_id);

CREATE TABLE IF NOT EXISTS commit_sessions (
  sha        TEXT NOT NULL REFERENCES commits(sha),
  session_id TEXT NOT NULL REFERENCES sessions(session_id),
  PRIMARY KEY (sha, session_id)
);

CREATE TABLE IF NOT EXISTS meta (
  key   TEXT PRIMARY KEY,
  value TEXT
);

-- Standalone FTS index over prompt text for `kaula search`.
CREATE VIRTUAL TABLE IF NOT EXISTS prompts_fts USING fts5(
  text,
  prompt_id UNINDEXED
);
"""


def connect() -> sqlite3.Connection:
    """Open (creating if needed) the database with WAL and FK enforcement."""
    config.ensure_dirs()
    conn = sqlite3.connect(str(config.db_path()), timeout=15.0)
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA journal_mode=WAL")
    conn.execute("PRAGMA busy_timeout=15000")
    conn.execute("PRAGMA foreign_keys=ON")
    _init(conn)
    return conn


def _init(conn: sqlite3.Connection) -> None:
    conn.executescript(_SCHEMA)
    cur = conn.execute("SELECT value FROM meta WHERE key='schema_version'")
    row = cur.fetchone()
    if row is None:
        now = int(time.time() * 1000)
        conn.execute(
            "INSERT INTO meta(key, value) VALUES('schema_version', ?)",
            (str(SCHEMA_VERSION),),
        )
        conn.execute(
            "INSERT OR IGNORE INTO meta(key, value) VALUES('installed_at', ?)",
            (str(now),),
        )
        conn.commit()


def get_meta(conn: sqlite3.Connection, key: str) -> Optional[str]:
    row = conn.execute("SELECT value FROM meta WHERE key=?", (key,)).fetchone()
    return row["value"] if row else None


def installed_at(conn: sqlite3.Connection) -> int:
    val = get_meta(conn, "installed_at")
    return int(val) if val else 0
