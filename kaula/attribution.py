"""The attribution algorithm — the only hard part.

Input: ``path:line`` at some revision (default HEAD).
Output: a prompt, with a confidence level.

Confidence is never inflated on a heuristic. A provenance tool that confidently
misattributes is worse than one that says "I don't know", so ``exact`` is
reported only when a line's content hashes to exactly one recorded agent edit.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Dict, List, Optional

from . import db, gitutil, hashing

# Confidence levels, weakest to strongest meaning.
UNATTRIBUTED = "unattributed"
HUMAN = "human"
COMMIT = "commit"
AMBIGUOUS = "ambiguous"
EXACT = "exact"


@dataclass
class Attribution:
    confidence: str
    path: str
    line: int
    reason: str = ""
    uncommitted: bool = False
    prompt: Optional[Dict] = None          # prompts row as dict
    session: Optional[Dict] = None         # sessions row as dict
    edit: Optional[Dict] = None            # edits row as dict
    commit_sha: Optional[str] = None
    commit_subject: Optional[str] = None
    other_sessions: List[Dict] = field(default_factory=list)
    also_touched: List[str] = field(default_factory=list)
    install_date_ms: Optional[int] = None


def why_line(conn, cwd: str, path_rel: str, line: int, rev: Optional[str] = None) -> Attribution:
    # Stage 1 — line → commit (blame the working tree, so dirty files just work).
    blame = gitutil.blame_line(path_rel, line, cwd, rev=rev)
    if blame is None:
        return Attribution(UNATTRIBUTED, path_rel, line,
                           reason="could not blame that line (untracked or out of range)")

    target_hash = hashing.line_hash(blame.content)

    if blame.uncommitted:
        return _attribute_uncommitted(conn, path_rel, line, target_hash)

    # Stage 2 — commit → candidate sessions.
    sessions = _sessions_for_commit(conn, cwd, blame.sha)

    if not sessions:
        installed = db.installed_at(conn)
        # Commit times are second-granular; require a full second of daylight
        # before calling a line "pre-install" so a commit made moments after
        # install isn't misreported.
        if blame.committed_at and installed and blame.committed_at < installed - 1000:
            return Attribution(UNATTRIBUTED, path_rel, line,
                               reason="line predates the kaula install",
                               commit_sha=blame.sha, commit_subject=blame.summary,
                               install_date_ms=installed)
        return Attribution(HUMAN, path_rel, line,
                           reason="commit is not linked to any kaula session",
                           commit_sha=blame.sha, commit_subject=blame.summary)

    # Stage 3 — commit → specific edit → prompt (match by content).
    matches = _matching_edits(conn, path_rel, target_hash, sessions) if target_hash else []

    if len(matches) == 1:
        return _finalise(conn, EXACT, path_rel, line, blame, matches[0], sessions,
                         reason="line content traced to a specific agent edit")

    if len(matches) > 1:
        # Stage 4 — disambiguate by surrounding context; confidence stays ambiguous.
        best = _disambiguate(conn, cwd, path_rel, line, matches)
        att = _finalise(conn, AMBIGUOUS, path_rel, line, blame, best, sessions,
                        reason="duplicated line content; resolved by surrounding context")
        att.other_sessions = _distinct_sessions(conn, matches, exclude=best["session_id"])
        return att

    # No content match. Stage 2 still gave us the commit's session(s).
    if len(sessions) == 1:
        att = Attribution(COMMIT, path_rel, line,
                          reason="commit came from this session, but this exact line was "
                                 "likely edited by a human afterwards or reformatted",
                          commit_sha=blame.sha, commit_subject=blame.summary)
        att.session = dict(_session_row(conn, sessions[0]))
        att.also_touched = _also_touched(conn, sessions[0], path_rel)
        return att

    att = Attribution(COMMIT, path_rel, line,
                      reason="commit came from these sessions, but this exact line did not "
                             "match any recorded edit",
                      commit_sha=blame.sha, commit_subject=blame.summary)
    att.other_sessions = [dict(_session_row(conn, s)) for s in sessions]
    return att


def why_commit(conn, cwd: str, sha: str) -> Dict:
    """All sessions behind a commit (``kaula why <sha>``)."""
    full = gitutil.rev_parse(sha, cwd) or sha
    sessions = _sessions_for_commit(conn, cwd, full)
    return {
        "sha": full,
        "subject": gitutil.commit_subject(full, cwd),
        "sessions": [dict(_session_row(conn, s)) for s in sessions],
    }


# --------------------------------------------------------------------------
# stages
# --------------------------------------------------------------------------

def _sessions_for_commit(conn, cwd: str, sha: str) -> List[str]:
    rows = conn.execute(
        "SELECT session_id FROM commit_sessions WHERE sha=?", (sha,)
    ).fetchall()
    if rows:
        return [r["session_id"] for r in rows]
    # Miss: history may have been rewritten. Resolve by patch-id.
    pid = gitutil.patch_id(sha, cwd)
    if not pid:
        return []
    rows = conn.execute(
        "SELECT DISTINCT cs.session_id FROM commit_sessions cs "
        "JOIN commits c ON c.sha = cs.sha WHERE c.patch_id=?",
        (pid,),
    ).fetchall()
    return [r["session_id"] for r in rows]


def _matching_edits(conn, path_rel: str, target_hash: Optional[str],
                    sessions: List[str]) -> List[Dict]:
    if not target_hash or not sessions:
        return []
    placeholders = ",".join("?" for _ in sessions)
    rows = conn.execute(
        f"SELECT * FROM edits WHERE path=? AND session_id IN ({placeholders})",
        (path_rel, *sessions),
    ).fetchall()
    out = []
    for row in rows:
        if target_hash in hashing.decode_hashes(row["line_hashes"]):
            out.append(dict(row))
    return out


def _attribute_uncommitted(conn, path_rel: str, line: int,
                           target_hash: Optional[str]) -> Attribution:
    """Dirty working tree: match against any recorded edit for this path."""
    if not target_hash:
        return Attribution(HUMAN, path_rel, line, uncommitted=True,
                           reason="uncommitted line with no content to hash")
    rows = conn.execute("SELECT * FROM edits WHERE path=?", (path_rel,)).fetchall()
    matches = [dict(r) for r in rows if target_hash in hashing.decode_hashes(r["line_hashes"])]
    if len(matches) == 1:
        att = _finalise(conn, EXACT, path_rel, line, None, matches[0],
                        [matches[0]["session_id"]],
                        reason="uncommitted line traced to a specific agent edit")
        att.uncommitted = True
        return att
    if len(matches) > 1:
        best = max(matches, key=lambda e: e["created_at"])
        att = _finalise(conn, AMBIGUOUS, path_rel, line, None, best,
                        [best["session_id"]],
                        reason="uncommitted duplicated line; reporting the most recent edit")
        att.uncommitted = True
        att.other_sessions = _distinct_sessions(conn, matches, exclude=best["session_id"])
        return att
    return Attribution(HUMAN, path_rel, line, uncommitted=True,
                       reason="uncommitted line not matched to any agent edit")


def _disambiguate(conn, cwd: str, path_rel: str, line: int, matches: List[Dict]) -> Dict:
    """Score each candidate by context overlap of the ±3 surrounding lines."""
    context = _surrounding_hashes(cwd, path_rel, line)
    best = matches[0]
    best_score = -1
    for edit in matches:
        edit_hashes = set(hashing.decode_hashes(edit["line_hashes"]))
        score = sum(1 for h in context if h in edit_hashes)
        if score > best_score or (score == best_score and edit["created_at"] > best["created_at"]):
            best, best_score = edit, score
    return best


def _surrounding_hashes(cwd: str, path_rel: str, line: int) -> List[str]:
    import os

    full = os.path.join(cwd, path_rel)
    try:
        with open(full, "r", encoding="utf-8", errors="replace") as fh:
            lines = fh.read().splitlines()
    except OSError:
        return []
    out = []
    for n in range(line - 3, line + 4):
        if n == line or n < 1 or n > len(lines):
            continue
        h = hashing.line_hash(lines[n - 1])
        if h:
            out.append(h)
    return out


# --------------------------------------------------------------------------
# helpers
# --------------------------------------------------------------------------

def _finalise(conn, confidence: str, path_rel: str, line: int, blame,
              edit: Dict, sessions: List[str], reason: str) -> Attribution:
    att = Attribution(confidence, path_rel, line, reason=reason)
    att.edit = edit
    if blame is not None:
        att.commit_sha = blame.sha
        att.commit_subject = blame.summary
    att.session = dict(_session_row(conn, edit["session_id"]))
    if edit.get("prompt_id") is not None:
        prow = conn.execute(
            "SELECT * FROM prompts WHERE prompt_id=?", (edit["prompt_id"],)
        ).fetchone()
        if prow:
            att.prompt = dict(prow)
    att.also_touched = _also_touched(conn, edit["session_id"], path_rel)
    return att


def _session_row(conn, session_id: str):
    return conn.execute(
        "SELECT * FROM sessions WHERE session_id=?", (session_id,)
    ).fetchone()


def _distinct_sessions(conn, edits: List[Dict], exclude: Optional[str]) -> List[Dict]:
    seen = set()
    out = []
    for e in edits:
        sid = e["session_id"]
        if sid == exclude or sid in seen:
            continue
        seen.add(sid)
        row = _session_row(conn, sid)
        if row:
            out.append(dict(row))
    return out


def _also_touched(conn, session_id: str, path_rel: str) -> List[str]:
    rows = conn.execute(
        "SELECT DISTINCT path FROM edits WHERE session_id=? AND path<>? ORDER BY path",
        (session_id, path_rel),
    ).fetchall()
    return [r["path"] for r in rows]
