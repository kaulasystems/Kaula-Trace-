"""Command-line surface.

    kaula init                     install git hooks, write harness config
    kaula hook <harness> <event>   internal; reads stdin
    kaula why <path>:<line>        primary command
    kaula why <sha>                all sessions behind a commit
    kaula search <query>           FTS over prompt text
    kaula sessions [--since 7d]    recent sessions and what they touched
    kaula doctor                   detect double-registered hooks, stale config
    kaula purge --before <date>    delete prompt/edit text, keep hashes
"""

from __future__ import annotations

import argparse
import json
import os
import re
import sys
import time
from datetime import datetime, timezone
from typing import List, Optional

from . import __version__, attribution, config, db, hooks, install, spool


def main(argv: Optional[List[str]] = None) -> int:
    argv = list(sys.argv[1:] if argv is None else argv)

    # `hook` is dispatched before argparse: it must tolerate anything and exit 0.
    if argv and argv[0] == "hook":
        return hooks.main(argv[1:])

    parser = _build_parser()
    args = parser.parse_args(argv)
    if not getattr(args, "func", None):
        parser.print_help()
        return 0
    return args.func(args)


def _build_parser() -> argparse.ArgumentParser:
    p = argparse.ArgumentParser(prog="kaula", description="local-first prompt↔code provenance")
    p.add_argument("--version", action="version", version=f"kaula {__version__}")
    sub = p.add_subparsers(dest="command")

    pi = sub.add_parser("init", help="install git hooks and harness config")
    pi.add_argument("--project", action="store_true",
                    help="write Claude Code hooks to ./.claude/settings.json instead of ~/.claude")
    pi.set_defaults(func=_cmd_init)

    pw = sub.add_parser("why", help="which prompt produced this line (or commit)")
    pw.add_argument("target", help="<path>:<line> or a commit sha")
    pw.add_argument("--rev", help="revision to blame against (default: working tree)")
    pw.add_argument("--json", action="store_true")
    pw.add_argument("--verbose", action="store_true", help="print the full session transcript")
    pw.set_defaults(func=_cmd_why)

    ps = sub.add_parser("search", help="full-text search over prompt text")
    ps.add_argument("query", nargs="+")
    ps.add_argument("--json", action="store_true")
    ps.add_argument("--limit", type=int, default=20)
    ps.set_defaults(func=_cmd_search)

    pse = sub.add_parser("sessions", help="recent sessions and what they touched")
    pse.add_argument("--since", default=None, help="e.g. 7d, 24h, 30m, or YYYY-MM-DD")
    pse.add_argument("--json", action="store_true")
    pse.set_defaults(func=_cmd_sessions)

    pd = sub.add_parser("doctor", help="detect double-registered hooks and stale config")
    pd.add_argument("--json", action="store_true")
    pd.set_defaults(func=_cmd_doctor)

    pp = sub.add_parser("purge", help="delete prompt/edit text, keep hashes")
    pp.add_argument("--before", required=True, help="YYYY-MM-DD or e.g. 30d")
    pp.add_argument("--yes", action="store_true", help="skip confirmation")
    pp.set_defaults(func=_cmd_purge)

    pdr = sub.add_parser("drain", help="internal: fold the spool into the database")
    pdr.set_defaults(func=_cmd_drain)

    return p


# --------------------------------------------------------------------------
# commands
# --------------------------------------------------------------------------

def _cmd_init(args) -> int:
    log = install.init(os.getcwd(), project=args.project)
    print("kaula initialised\n")
    for line in log:
        print("  " + line)
    print("\nOpen a new Claude Code or Cursor session to start capturing.")
    return 0


def _cmd_why(args) -> int:
    spool.drain()
    cwd = os.getcwd()
    conn = db.connect()
    try:
        target = args.target
        if _looks_like_sha(target):
            result = attribution.why_commit(conn, cwd, target)
            if args.json:
                print(json.dumps(result, indent=2))
            else:
                _print_commit(result)
            return 0

        path_rel, line = _parse_path_line(target, cwd)
        if line is None:
            print("usage: kaula why <path>:<line>   or   kaula why <sha>", file=sys.stderr)
            return 2
        att = attribution.why_line(conn, cwd, path_rel, line, rev=args.rev)
        if args.json:
            print(json.dumps(_att_to_dict(att), indent=2))
        else:
            _print_attribution(att, conn, verbose=args.verbose)
        return 0
    finally:
        conn.close()


def _cmd_search(args) -> int:
    spool.drain()
    query = " ".join(args.query)
    conn = db.connect()
    try:
        try:
            rows = conn.execute(
                "SELECT f.prompt_id, p.session_id, p.created_at, "
                "       snippet(prompts_fts, 0, '[', ']', '…', 12) AS snip, "
                "       s.harness, s.model "
                "FROM prompts_fts f "
                "JOIN prompts p ON p.prompt_id = f.prompt_id "
                "JOIN sessions s ON s.session_id = p.session_id "
                "WHERE prompts_fts MATCH ? "
                "ORDER BY p.created_at DESC LIMIT ?",
                (query, args.limit),
            ).fetchall()
        except Exception as exc:  # FTS syntax error on user input
            print(f"search error: {exc}", file=sys.stderr)
            return 2
        if args.json:
            print(json.dumps([dict(r) for r in rows], indent=2))
            return 0
        if not rows:
            print("no matching prompts")
            return 0
        for r in rows:
            print(f"  {_fmt_ts(r['created_at'])}  ·  {r['harness']}  ·  {r['model'] or '?'}"
                  f"  ·  session {_short(r['session_id'])}")
            print(f"    {r['snip']}\n")
        return 0
    finally:
        conn.close()


def _cmd_sessions(args) -> int:
    spool.drain()
    cutoff = _parse_since(args.since) if args.since else 0
    conn = db.connect()
    try:
        rows = conn.execute(
            "SELECT * FROM sessions WHERE started_at >= ? ORDER BY started_at DESC",
            (cutoff,),
        ).fetchall()
        out = []
        for s in rows:
            counts = conn.execute(
                "SELECT COUNT(*) AS n FROM prompts WHERE session_id=?", (s["session_id"],)
            ).fetchone()["n"]
            paths = [r["path"] for r in conn.execute(
                "SELECT DISTINCT path FROM edits WHERE session_id=? ORDER BY path",
                (s["session_id"],)).fetchall()]
            out.append({"session": dict(s), "prompts": counts, "paths": paths})
        if args.json:
            print(json.dumps(out, indent=2))
            return 0
        if not out:
            print("no sessions recorded yet")
            return 0
        for item in out:
            s = item["session"]
            print(f"  session {_short(s['session_id'])}  ·  {_fmt_ts(s['started_at'])}  ·  "
                  f"{s['harness']}  ·  {s['model'] or '?'}")
            print(f"    {item['prompts']} prompt(s)"
                  + (f"  ·  edited {', '.join(item['paths'])}" if item["paths"] else ""))
            print()
        return 0
    finally:
        conn.close()


def _cmd_doctor(args) -> int:
    report = _doctor_report()
    if args.json:
        print(json.dumps(report, indent=2))
        return 0
    print("kaula doctor\n")
    for check in report["checks"]:
        icon = {"ok": "✓", "warn": "!", "error": "✗"}.get(check["status"], "?")
        print(f"  [{icon}] {check['name']}: {check['detail']}")
    return 0 if all(c["status"] != "error" for c in report["checks"]) else 1


def _cmd_purge(args) -> int:
    before = _parse_before(args.before)
    if before is None:
        print("could not parse --before (use YYYY-MM-DD or e.g. 30d)", file=sys.stderr)
        return 2
    spool.drain()
    conn = db.connect()
    try:
        n_p = conn.execute(
            "SELECT COUNT(*) AS n FROM prompts WHERE created_at < ? AND text IS NOT NULL",
            (before,),
        ).fetchone()["n"]
        n_e = conn.execute(
            "SELECT COUNT(*) AS n FROM edits WHERE created_at < ? AND new_text IS NOT NULL",
            (before,),
        ).fetchone()["n"]
        if not args.yes:
            when = _fmt_ts(before)
            resp = input(f"Purge text from {n_p} prompt(s) and {n_e} edit(s) before {when}? "
                         "Hashes are kept. [y/N] ")
            if resp.strip().lower() not in ("y", "yes"):
                print("aborted")
                return 1
        ids = [r["prompt_id"] for r in conn.execute(
            "SELECT prompt_id FROM prompts WHERE created_at < ? AND text IS NOT NULL",
            (before,)).fetchall()]
        conn.execute("UPDATE prompts SET text=NULL WHERE created_at < ?", (before,))
        conn.execute("UPDATE edits SET new_text=NULL WHERE created_at < ?", (before,))
        for pid in ids:
            conn.execute("DELETE FROM prompts_fts WHERE prompt_id=?", (pid,))
        conn.commit()
        print(f"purged text from {n_p} prompt(s) and {n_e} edit(s); hashes retained")
        return 0
    finally:
        conn.close()


def _cmd_drain(args) -> int:
    spool.drain()
    return 0


# --------------------------------------------------------------------------
# doctor internals
# --------------------------------------------------------------------------

def _doctor_report() -> dict:
    checks = []
    # Database reachable + schema.
    try:
        conn = db.connect()
        ver = db.get_meta(conn, "schema_version")
        installed = db.installed_at(conn)
        n_sessions = conn.execute("SELECT COUNT(*) AS n FROM sessions").fetchone()["n"]
        conn.close()
        if ver == str(db.SCHEMA_VERSION):
            checks.append(_chk("database", "ok",
                               f"schema v{ver}, {n_sessions} session(s), installed {_fmt_ts(installed)}"))
        else:
            checks.append(_chk("database", "warn",
                               f"unexpected schema version {ver} (expected {db.SCHEMA_VERSION})"))
    except Exception as exc:
        checks.append(_chk("database", "error", f"cannot open db: {exc}"))

    # Spool backlog.
    spool_dir = config.spool_dir()
    backlog = 0
    if spool_dir.exists():
        for f in spool_dir.iterdir():
            if f.suffix == ".jsonl":
                try:
                    backlog += sum(1 for _ in f.open())
                except OSError:
                    pass
    checks.append(_chk("spool", "ok" if backlog == 0 else "warn",
                       "empty" if backlog == 0 else f"{backlog} event(s) not yet drained"))

    # Redaction config.
    checks.append(_chk("redaction", "ok" if config.redact_path().exists() else "warn",
                       str(config.redact_path()) if config.redact_path().exists()
                       else "no redact.toml (defaults still apply)"))

    # Double-registered Claude Code hooks.
    checks.append(_dup_claude_check())

    # Git hooks in the current repo.
    checks.append(_git_hook_check())

    return {"version": __version__, "checks": checks}


def _dup_claude_check():
    from pathlib import Path
    counts = {}
    candidates = [Path.home() / ".claude" / "settings.json",
                  Path(os.getcwd()) / ".claude" / "settings.json"]
    seen_paths = set()
    for path in candidates:
        if not path.exists():
            continue
        # ~/.claude and ./.claude can resolve to the same file; count it once.
        resolved = path.resolve()
        if resolved in seen_paths:
            continue
        seen_paths.add(resolved)
        try:
            data = json.loads(path.read_text(encoding="utf-8"))
        except (json.JSONDecodeError, OSError):
            continue
        for event, entries in (data.get("hooks") or {}).items():
            for e in entries if isinstance(entries, list) else []:
                for h in (e.get("hooks") or []) if isinstance(e, dict) else []:
                    cmd = h.get("command", "") if isinstance(h, dict) else ""
                    if "kaula hook" in cmd:
                        counts[cmd] = counts.get(cmd, 0) + 1
    dupes = {c: n for c, n in counts.items() if n > 1}
    if not counts:
        return _chk("cc hooks", "warn", "no kaula hooks registered (run `kaula init`)")
    if dupes:
        return _chk("cc hooks", "warn",
                    "double-registered: " + ", ".join(f"{c} ×{n}" for c, n in dupes.items()))
    return _chk("cc hooks", "ok", f"{len(counts)} hook(s) registered, no duplicates")


def _git_hook_check():
    from pathlib import Path
    from . import gitutil
    root = gitutil.repo_root(os.getcwd())
    if not root:
        return _chk("git hooks", "warn", "not inside a git repo")
    problems = []
    installed = []
    for name in ("post-commit", "post-rewrite"):
        path = Path(root) / ".git" / "hooks" / name
        if not path.exists():
            problems.append(f"{name} missing")
            continue
        text = path.read_text(encoding="utf-8", errors="replace")
        n = text.count(install.MARKER)
        if n == 0:
            problems.append(f"{name} not wired")
        elif n > 1:
            problems.append(f"{name} has {n} kaula blocks")
        else:
            installed.append(name)
    if problems:
        return _chk("git hooks", "warn", "; ".join(problems)
                    + (f" (ok: {', '.join(installed)})" if installed else ""))
    return _chk("git hooks", "ok", f"installed: {', '.join(installed)}")


def _chk(name, status, detail):
    return {"name": name, "status": status, "detail": detail}


# --------------------------------------------------------------------------
# output formatting
# --------------------------------------------------------------------------

def _print_attribution(att: attribution.Attribution, conn, verbose: bool = False) -> None:
    tag = "uncommitted · " if att.uncommitted else ""
    print()
    print(f"  {att.path}:{att.line}  ·  confidence: {tag}{att.confidence}")
    print()

    if att.prompt is not None:
        text = att.prompt.get("text")
        print(f'  "{text}"' if text else "  (prompt text purged; hash retained)")
        s = att.session or {}
        meta = "    " + "  ·  ".join(filter(None, [
            _fmt_ts(att.prompt.get("created_at", 0)),
            s.get("harness"),
            s.get("model"),
        ]))
        print(meta)
        seq = att.prompt.get("seq")
        n = conn.execute("SELECT COUNT(*) AS n FROM prompts WHERE session_id=?",
                         (s.get("session_id"),)).fetchone()["n"] if s else 0
        turn = f"turn {seq + 1}/{n}" if seq is not None and n else ""
        print("    " + "  ·  ".join(filter(None, [f"session {_short(s.get('session_id',''))}", turn])))
        print()

    if att.edit is not None:
        print(f"  edit      {att.edit['path']}  ({att.edit['tool']})")
    if att.commit_sha:
        subj = f' "{att.commit_subject}"' if att.commit_subject else ""
        print(f"  commit    {_short(att.commit_sha)}{subj}")
    if att.also_touched:
        print(f"  also edited {', '.join(att.also_touched)}")

    if att.prompt is None:
        print(f"  {att.reason}")
        if att.confidence == attribution.UNATTRIBUTED and att.install_date_ms:
            print(f"  kaula was installed {_fmt_ts(att.install_date_ms)}")

    if att.other_sessions:
        print("  other sessions also edited this line:")
        for s in att.other_sessions:
            print(f"    session {_short(s.get('session_id',''))}  ·  {s.get('harness')}")

    if verbose and att.session:
        _print_transcript(conn, att.session["session_id"])
    print()


def _print_transcript(conn, session_id: str) -> None:
    rows = conn.execute(
        "SELECT seq, text, created_at FROM prompts WHERE session_id=? ORDER BY seq",
        (session_id,),
    ).fetchall()
    print("\n  ── transcript ──")
    for r in rows:
        text = r["text"] or "(purged)"
        print(f"  [{r['seq']}] {_fmt_ts(r['created_at'])}  {text}")


def _print_commit(result: dict) -> None:
    print()
    subj = f' "{result["subject"]}"' if result.get("subject") else ""
    print(f"  commit {_short(result['sha'])}{subj}")
    print()
    if not result["sessions"]:
        print("  no kaula sessions linked to this commit")
        return
    print(f"  {len(result['sessions'])} session(s) behind this commit:")
    for s in result["sessions"]:
        print(f"    session {_short(s['session_id'])}  ·  {_fmt_ts(s['started_at'])}  ·  "
              f"{s['harness']}  ·  {s.get('model') or '?'}")


# --------------------------------------------------------------------------
# parsing / small helpers
# --------------------------------------------------------------------------

_SHA_RE = re.compile(r"^[0-9a-fA-F]{7,40}$")


def _looks_like_sha(target: str) -> bool:
    if ":" in target or "/" in target or "." in target:
        return False
    return bool(_SHA_RE.match(target))


def _parse_path_line(target: str, cwd: str):
    if ":" not in target:
        return target, None
    path_part, _, line_part = target.rpartition(":")
    if not line_part.isdigit():
        return target, None
    path_rel = _to_repo_relative(path_part, cwd)
    return path_rel, int(line_part)


def _to_repo_relative(path: str, cwd: str) -> str:
    from . import gitutil
    root = gitutil.repo_root(cwd)
    abspath = path if os.path.isabs(path) else os.path.join(cwd, path)
    base = root or cwd
    try:
        rel = os.path.relpath(abspath, base)
    except ValueError:
        rel = path
    return rel.replace(os.sep, "/")


def _att_to_dict(att: attribution.Attribution) -> dict:
    return {
        "confidence": att.confidence,
        "path": att.path,
        "line": att.line,
        "uncommitted": att.uncommitted,
        "reason": att.reason,
        "prompt": att.prompt,
        "session": att.session,
        "edit": att.edit,
        "commit": {"sha": att.commit_sha, "subject": att.commit_subject} if att.commit_sha else None,
        "other_sessions": att.other_sessions,
        "also_touched": att.also_touched,
        "install_date_ms": att.install_date_ms,
    }


def _fmt_ts(ms) -> str:
    if not ms:
        return "?"
    try:
        return datetime.fromtimestamp(int(ms) / 1000).strftime("%Y-%m-%d %H:%M")
    except (ValueError, OverflowError, OSError):
        return "?"


def _short(sha: Optional[str]) -> str:
    return (sha or "")[:8]


_DUR_RE = re.compile(r"^(\d+)([dhm])$")


def _parse_since(spec: str) -> int:
    now = int(time.time() * 1000)
    m = _DUR_RE.match(spec.strip())
    if m:
        n = int(m.group(1))
        unit = {"d": 86400, "h": 3600, "m": 60}[m.group(2)]
        return now - n * unit * 1000
    ts = _parse_date(spec)
    return ts if ts is not None else 0


def _parse_before(spec: str) -> Optional[int]:
    m = _DUR_RE.match(spec.strip())
    if m:
        n = int(m.group(1))
        unit = {"d": 86400, "h": 3600, "m": 60}[m.group(2)]
        return int(time.time() * 1000) - n * unit * 1000
    return _parse_date(spec)


def _parse_date(spec: str) -> Optional[int]:
    for fmt in ("%Y-%m-%d", "%Y/%m/%d", "%Y-%m-%dT%H:%M"):
        try:
            dt = datetime.strptime(spec.strip(), fmt).replace(tzinfo=timezone.utc)
            return int(dt.timestamp() * 1000)
        except ValueError:
            continue
    return None
