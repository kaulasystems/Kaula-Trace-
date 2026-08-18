"""Thin wrappers over the git plumbing kaula relies on.

All calls are read-only and confined to a repo working directory. Nothing here
touches the network.
"""

from __future__ import annotations

import subprocess
from dataclasses import dataclass
from typing import List, Optional

ZERO_SHA = "0" * 40


def _run(args: List[str], cwd: str, stdin: Optional[bytes] = None) -> Optional[str]:
    try:
        proc = subprocess.run(
            ["git", *args],
            cwd=cwd,
            input=stdin,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            check=False,
        )
    except (OSError, ValueError):
        return None
    if proc.returncode != 0:
        return None
    return proc.stdout.decode("utf-8", "replace")


def repo_root(cwd: str) -> Optional[str]:
    out = _run(["rev-parse", "--show-toplevel"], cwd)
    return out.strip() if out else None


def rev_parse(rev: str, cwd: str) -> Optional[str]:
    out = _run(["rev-parse", "--verify", "--quiet", rev], cwd)
    return out.strip() if out else None


def head_sha(cwd: str) -> Optional[str]:
    return rev_parse("HEAD", cwd)


def patch_id(sha: str, cwd: str) -> Optional[str]:
    """Stable patch-id for a commit — survives rebase/amend/cherry-pick/squash."""
    diff = _run(["diff-tree", "--root", "-p", sha], cwd)
    if not diff:
        return None
    out = _run(["patch-id", "--stable"], cwd, stdin=diff.encode("utf-8"))
    if not out:
        return None
    parts = out.split()
    return parts[0] if parts else None


def changed_files(sha: str, cwd: str) -> List[str]:
    out = _run(
        ["diff-tree", "--no-commit-id", "--name-only", "--root", "-r", sha], cwd
    )
    if not out:
        return []
    return [line.strip() for line in out.splitlines() if line.strip()]


def commit_time_ms(rev: str, cwd: str) -> Optional[int]:
    out = _run(["show", "-s", "--format=%ct", rev], cwd)
    if not out:
        return None
    try:
        return int(out.strip()) * 1000
    except ValueError:
        return None


def commit_subject(sha: str, cwd: str) -> Optional[str]:
    out = _run(["show", "-s", "--format=%s", sha], cwd)
    return out.strip() if out else None


def is_dirty(path_rel: str, cwd: str) -> bool:
    out = _run(["status", "--porcelain", "--", path_rel], cwd)
    return bool(out and out.strip())


@dataclass
class BlameLine:
    sha: str
    content: str
    committed_at: Optional[int]  # unix ms
    summary: Optional[str]

    @property
    def uncommitted(self) -> bool:
        return self.sha == ZERO_SHA


def blame_line(path_rel: str, line: int, cwd: str, rev: Optional[str] = None) -> Optional[BlameLine]:
    """Blame a single line via porcelain output.

    With no ``rev`` this blames the working tree, so uncommitted lines surface
    as the all-zero sha (``Not Committed Yet``) and flow to the uncommitted
    matcher downstream.
    """
    args = ["blame", "-L", f"{line},{line}", "--porcelain"]
    if rev:
        args.append(rev)
    args += ["--", path_rel]
    out = _run(args, cwd)
    if out is None:
        return None
    return _parse_porcelain(out)


def _parse_porcelain(out: str) -> Optional[BlameLine]:
    lines = out.splitlines()
    if not lines:
        return None
    header = lines[0].split()
    if not header:
        return None
    sha = header[0]
    content = ""
    committed_at: Optional[int] = None
    summary: Optional[str] = None
    for ln in lines[1:]:
        if ln.startswith("\t"):
            content = ln[1:]
            break
        if ln.startswith("committer-time "):
            try:
                committed_at = int(ln.split(" ", 1)[1]) * 1000
            except ValueError:
                committed_at = None
        elif ln.startswith("summary "):
            summary = ln.split(" ", 1)[1]
    return BlameLine(sha=sha, content=content, committed_at=committed_at, summary=summary)
