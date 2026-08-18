"""Commit binding: link commits to the sessions that produced their edits.

A session is linked to a commit if any of its edits touch a path in the
commit's changed-file set **and** the edit timestamp falls between the previous
commit's time and this commit's time. This over-links slightly (a session
editing files across two commits links to both) — that's fine, resolution
happens at attribution time by content.

Both the SHA and the stable patch-id are stored: the SHA resolves fast, and the
patch-id survives rebase/amend/cherry-pick/squash when the SHA does not.
"""

from __future__ import annotations

from typing import Optional

from . import db, gitutil, spool


def bind_head(cwd: str) -> Optional[str]:
    """Record HEAD and link candidate sessions. Returns the bound SHA."""
    spool.drain()  # ensure the edits we're about to bind are in the db
    root = gitutil.repo_root(cwd)
    if not root:
        return None
    sha = gitutil.head_sha(cwd)
    if not sha:
        return None
    _bind_sha(cwd, root, sha)
    return sha


def _bind_sha(cwd: str, root: str, sha: str) -> None:
    pid = gitutil.patch_id(sha, cwd) or ""
    committed_at = gitutil.commit_time_ms(sha, cwd) or 0
    conn = db.connect()
    try:
        conn.execute(
            "INSERT OR IGNORE INTO commits(sha, patch_id, repo_root, committed_at)"
            " VALUES(?,?,?,?)",
            (sha, pid, root, committed_at),
        )
        conn.execute(
            "UPDATE commits SET patch_id=?, committed_at=? WHERE sha=?",
            (pid, committed_at, sha),
        )

        files = gitutil.changed_files(sha, cwd)
        if not files:
            conn.commit()
            return
        prev_ms = gitutil.commit_time_ms(sha + "^", cwd) or 0
        # git commit times are second-granular, so an edit made in the same
        # wall-clock second as the commit can carry a larger ms value than the
        # truncated commit time. Extend the upper bound to the end of that
        # second so those edits still fall inside the window.
        this_ms = (committed_at or _now_ms()) + 999

        placeholders = ",".join("?" for _ in files)
        rows = conn.execute(
            f"SELECT DISTINCT session_id FROM edits "
            f"WHERE path IN ({placeholders}) AND created_at > ? AND created_at <= ?",
            (*files, prev_ms, this_ms),
        ).fetchall()
        for row in rows:
            conn.execute(
                "INSERT OR IGNORE INTO commit_sessions(sha, session_id) VALUES(?, ?)",
                (sha, row["session_id"]),
            )
        conn.commit()
    finally:
        conn.close()


def rewrite(cwd: str, stdin_text: str) -> None:
    """Handle git's post-rewrite: carry session links onto the new SHAs.

    stdin lines are ``<old-sha> <new-sha>`` (amend/rebase). The new commit has
    the same patch-id, so we record it and copy the old commit's session links.
    """
    root = gitutil.repo_root(cwd)
    if not root:
        return
    conn = db.connect()
    try:
        for line in stdin_text.splitlines():
            parts = line.split()
            if len(parts) < 2:
                continue
            old_sha, new_sha = parts[0], parts[1]
            pid = gitutil.patch_id(new_sha, cwd) or ""
            committed_at = gitutil.commit_time_ms(new_sha, cwd) or 0
            conn.execute(
                "INSERT OR IGNORE INTO commits(sha, patch_id, repo_root, committed_at)"
                " VALUES(?,?,?,?)",
                (new_sha, pid, root, committed_at),
            )
            # Prefer copying by old sha; fall back to patch-id match.
            src = conn.execute(
                "SELECT session_id FROM commit_sessions WHERE sha=?", (old_sha,)
            ).fetchall()
            if not src and pid:
                src = conn.execute(
                    "SELECT DISTINCT cs.session_id FROM commit_sessions cs "
                    "JOIN commits c ON c.sha = cs.sha WHERE c.patch_id=?",
                    (pid,),
                ).fetchall()
            for row in src:
                conn.execute(
                    "INSERT OR IGNORE INTO commit_sessions(sha, session_id) VALUES(?, ?)",
                    (new_sha, row["session_id"]),
                )
        conn.commit()
    finally:
        conn.close()


def _now_ms() -> int:
    import time

    return int(time.time() * 1000)
