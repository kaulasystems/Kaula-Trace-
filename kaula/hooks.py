"""Hook entry points: ``kaula hook <harness> <event>``.

Invoked by Claude Code / Cursor / git in the interactive path. Every handler:

* reads the harness payload from stdin (JSON), tolerating field drift,
* redacts and hashes **before** anything is written,
* appends one line to the spool and returns exit 0 — always.

Exit 0 is a hard requirement: a non-zero hook can block the agent. Any internal
failure is swallowed and the handler still returns 0.
"""

from __future__ import annotations

import json
import os
import subprocess
import sys
import time
from typing import Any, Dict, List, Optional

from . import config, gitutil, hashing, redact, spool

# Harness aliases accepted on the command line.
_HARNESS = {
    "cc": "claude-code",
    "claude-code": "claude-code",
    "claude": "claude-code",
    "cursor": "cursor",
}


def main(argv: List[str]) -> int:
    if len(argv) < 2:
        return 0  # never block
    harness_arg, event = argv[0], argv[1]
    try:
        if harness_arg == "git":
            return _git_hook(event)
        harness = _HARNESS.get(harness_arg)
        if harness is None:
            return 0
        payload = _read_stdin_json()
        _handle_harness_event(harness, event, payload)
        _spawn_drain()
    except Exception:
        # Capture must never break the agent.
        pass
    return 0


def _read_stdin_json() -> Dict[str, Any]:
    if sys.stdin is None or sys.stdin.isatty():
        return {}
    try:
        raw = sys.stdin.read()
    except Exception:
        return {}
    if not raw.strip():
        return {}
    try:
        data = json.loads(raw)
        return data if isinstance(data, dict) else {}
    except json.JSONDecodeError:
        return {}


def _handle_harness_event(harness: str, event: str, payload: Dict[str, Any]) -> None:
    sid = _session_id(harness, payload)
    if not sid and event != "session-start":
        # Without a session id nothing can be attributed; still exit 0.
        return
    repo_root = _repo_root(harness, payload)

    if event == "session-start":
        spool.append({
            "type": "session-start",
            "harness": harness,
            "session_id": sid,
            "repo_root": repo_root,
            "harness_version": payload.get("harness_version") or payload.get("version"),
            "model": payload.get("model"),
        })
    elif event == "prompt":
        text = _prompt_text(payload)
        if text is None:
            return
        spool.append({
            "type": "prompt",
            "harness": harness,
            "session_id": sid,
            "repo_root": repo_root,
            "text": redact.redact(text),
            "text_hash": hashing.sha256_hex(text),  # pre-redaction
        })
    elif event == "edit":
        for e in _edit_records(payload, repo_root):
            e.update({
                "type": "edit",
                "harness": harness,
                "session_id": sid,
                "repo_root": repo_root,
            })
            spool.append(e)
    elif event == "session-end":
        spool.append({
            "type": "session-end",
            "harness": harness,
            "session_id": sid,
            "repo_root": repo_root,
        })


def _session_id(harness: str, payload: Dict[str, Any]) -> Optional[str]:
    for key in ("session_id", "conversation_id", "sessionId", "conversationId"):
        val = payload.get(key)
        if val:
            return str(val)
    return None


def _repo_root(harness: str, payload: Dict[str, Any]) -> str:
    cwd = payload.get("cwd")
    if not cwd:
        roots = payload.get("workspace_roots") or payload.get("workspaceRoots")
        if isinstance(roots, list) and roots:
            cwd = roots[0]
    if not cwd:
        cwd = os.getcwd()
    root = gitutil.repo_root(cwd)
    return root or cwd


def _prompt_text(payload: Dict[str, Any]) -> Optional[str]:
    for key in ("prompt", "text", "message", "content", "user_prompt"):
        val = payload.get(key)
        if isinstance(val, str) and val:
            return val
    return None


def _edit_records(payload: Dict[str, Any], repo_root: str) -> List[Dict[str, Any]]:
    """Extract inserted content from a tool payload, tolerating shapes.

    Handles Claude Code Edit / Write / MultiEdit and Cursor afterFileEdit.
    """
    tool = payload.get("tool_name") or payload.get("toolName") or payload.get("tool") or "Edit"
    tool_input = payload.get("tool_input") or payload.get("toolInput") or payload
    file_path = (
        tool_input.get("file_path")
        or tool_input.get("filePath")
        or tool_input.get("path")
        or payload.get("file_path")
    )
    if not file_path:
        return []

    inserted = _inserted_text(tool_input)
    if not inserted:
        return []

    rel = _rel_path(file_path, repo_root)
    return [{
        "tool": tool,
        "path": rel,
        "new_text": redact.redact(inserted),
        "new_hash": hashing.sha256_hex(inserted),  # pre-redaction
        "line_hashes": hashing.encode_hashes(hashing.line_hashes(inserted)),
    }]


def _inserted_text(tool_input: Dict[str, Any]) -> str:
    # Write-style: whole file content.
    for key in ("content", "new_text", "newText", "text"):
        val = tool_input.get(key)
        if isinstance(val, str) and val:
            return val
    # Edit-style: single replacement.
    for key in ("new_string", "newString"):
        val = tool_input.get(key)
        if isinstance(val, str) and val:
            return val
    # MultiEdit / Cursor: list of edits.
    edits = tool_input.get("edits")
    if isinstance(edits, list):
        parts: List[str] = []
        for e in edits:
            if not isinstance(e, dict):
                continue
            for key in ("new_string", "newString", "new_text", "newText", "text"):
                v = e.get(key)
                if isinstance(v, str) and v:
                    parts.append(v)
                    break
        if parts:
            return "\n".join(parts)
    return ""


def _rel_path(file_path: str, repo_root: str) -> str:
    try:
        rel = os.path.relpath(file_path, repo_root)
    except ValueError:
        rel = file_path
    return rel.replace(os.sep, "/")


def _spawn_drain() -> None:
    """Fire-and-forget a detached drain so data lands promptly. Best-effort."""
    try:
        subprocess.Popen(
            [sys.executable, "-m", "kaula", "drain"],
            stdout=subprocess.DEVNULL,
            stderr=subprocess.DEVNULL,
            stdin=subprocess.DEVNULL,
            start_new_session=True,
        )
    except Exception:
        pass


# --------------------------------------------------------------------------
# git hooks
# --------------------------------------------------------------------------

def _git_hook(event: str) -> int:
    from . import binding
    try:
        cwd = os.getcwd()
        if event == "post-commit":
            binding.bind_head(cwd)
        elif event == "post-rewrite":
            raw = ""
            if sys.stdin is not None and not sys.stdin.isatty():
                raw = sys.stdin.read()
            binding.rewrite(cwd, raw)
    except Exception:
        pass
    return 0
