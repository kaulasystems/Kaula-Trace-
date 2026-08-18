"""``kaula init`` — install git hooks and harness config.

Everything here is idempotent and non-destructive: existing hooks are preserved,
existing settings are merged, and kaula's contributions are fenced with markers
so they can be found again (by ``doctor``) and re-run without duplicating.
"""

from __future__ import annotations

import json
import os
import stat
from pathlib import Path
from typing import Dict, List, Tuple

from . import config, db, gitutil, redact

MARKER = "# >>> kaula >>>"
MARKER_END = "# <<< kaula <<<"

_GIT_HOOKS = {
    "post-commit": "kaula hook git post-commit\n",
    "post-rewrite": "kaula hook git post-rewrite\n",
}

# Claude Code settings.json hook wiring.
_CC_HOOKS = {
    "SessionStart": [{"hooks": [{"type": "command", "command": "kaula hook cc session-start"}]}],
    "UserPromptSubmit": [{"hooks": [{"type": "command", "command": "kaula hook cc prompt"}]}],
    "PostToolUse": [{"matcher": "Edit|Write|MultiEdit",
                     "hooks": [{"type": "command", "command": "kaula hook cc edit"}]}],
    "SessionEnd": [{"hooks": [{"type": "command", "command": "kaula hook cc session-end"}]}],
}

_CURSOR_HOOKS = {
    "version": 1,
    "hooks": {
        "sessionStart": [{"command": "kaula hook cursor session-start"}],
        "beforeSubmitPrompt": [{"command": "kaula hook cursor prompt"}],
        "afterFileEdit": [{"command": "kaula hook cursor edit"}],
        "sessionEnd": [{"command": "kaula hook cursor session-end"}],
    },
}


def init(cwd: str, project: bool = False) -> List[str]:
    """Run the installer. Returns human-readable log lines."""
    log: List[str] = []
    config.ensure_dirs()
    db.connect().close()             # create the database
    redact.ensure_default_config()   # write default redact.toml
    log.append(f"database   {config.db_path()}")
    log.append(f"redaction  {config.redact_path()}")

    root = gitutil.repo_root(cwd)
    if root:
        log += _install_git_hooks(Path(root))
        log += _install_cursor(Path(root))
    else:
        log.append("git        not a git repo — skipped git + cursor hooks")

    log += _install_claude_code(Path(root) if (root and project) else None)
    return log


def _install_git_hooks(root: Path) -> List[str]:
    log = []
    hooks_dir = root / ".git" / "hooks"
    hooks_dir.mkdir(parents=True, exist_ok=True)
    for name, body in _GIT_HOOKS.items():
        path = hooks_dir / name
        block = f"{MARKER}\n{body}{MARKER_END}\n"
        if path.exists():
            text = path.read_text(encoding="utf-8")
            if MARKER in text:
                log.append(f"git hook   {name}: already installed")
                continue
            if not text.endswith("\n"):
                text += "\n"
            path.write_text(text + block, encoding="utf-8")
            log.append(f"git hook   {name}: appended kaula block")
        else:
            path.write_text("#!/bin/sh\n" + block, encoding="utf-8")
            log.append(f"git hook   {name}: created")
        _chmod_x(path)
    return log


def _install_cursor(root: Path) -> List[str]:
    path = root / ".cursor" / "hooks.json"
    path.parent.mkdir(parents=True, exist_ok=True)
    if path.exists():
        try:
            existing = json.loads(path.read_text(encoding="utf-8"))
        except json.JSONDecodeError:
            existing = {}
        merged, changed = _merge_cursor(existing)
        if changed:
            path.write_text(json.dumps(merged, indent=2) + "\n", encoding="utf-8")
            return [f"cursor     {path}: merged kaula hooks"]
        return [f"cursor     {path}: already installed"]
    path.write_text(json.dumps(_CURSOR_HOOKS, indent=2) + "\n", encoding="utf-8")
    return [f"cursor     {path}: created"]


def _merge_cursor(existing: Dict) -> Tuple[Dict, bool]:
    changed = False
    existing.setdefault("version", 1)
    hooks = existing.setdefault("hooks", {})
    for event, entries in _CURSOR_HOOKS["hooks"].items():
        current = hooks.setdefault(event, [])
        for entry in entries:
            if not _has_command(current, entry["command"]):
                current.append(entry)
                changed = True
    return existing, changed


def _install_claude_code(project_root) -> List[str]:
    if project_root is not None:
        path = project_root / ".claude" / "settings.json"
        label = "cc project"
    else:
        path = Path.home() / ".claude" / "settings.json"
        label = "cc user"
    path.parent.mkdir(parents=True, exist_ok=True)
    if path.exists():
        try:
            existing = json.loads(path.read_text(encoding="utf-8"))
        except json.JSONDecodeError:
            existing = {}
    else:
        existing = {}
    merged, changed = _merge_claude(existing)
    if changed:
        path.write_text(json.dumps(merged, indent=2) + "\n", encoding="utf-8")
        return [f"{label}  {path}: merged kaula hooks"]
    return [f"{label}  {path}: already installed"]


def _merge_claude(existing: Dict) -> Tuple[Dict, bool]:
    changed = False
    hooks = existing.setdefault("hooks", {})
    for event, entries in _CC_HOOKS.items():
        current = hooks.setdefault(event, [])
        for entry in entries:
            cmd = entry["hooks"][0]["command"]
            if not _cc_has_command(current, cmd):
                current.append(entry)
                changed = True
    return existing, changed


def _has_command(entries: List, command: str) -> bool:
    for e in entries:
        if isinstance(e, dict) and e.get("command") == command:
            return True
    return False


def _cc_has_command(entries: List, command: str) -> bool:
    for e in entries:
        if not isinstance(e, dict):
            continue
        for h in e.get("hooks", []):
            if isinstance(h, dict) and h.get("command") == command:
                return True
    return False


def _chmod_x(path: Path) -> None:
    try:
        st = os.stat(path)
        os.chmod(path, st.st_mode | stat.S_IXUSR | stat.S_IXGRP | stat.S_IXOTH)
    except OSError:
        pass
