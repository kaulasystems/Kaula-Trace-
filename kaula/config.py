"""Filesystem layout and configuration.

Everything lives under ``KAULA_HOME`` (default ``~/.kaula``). The database
path can be overridden on its own with ``KAULA_DB`` so the spec's contract
(``~/.kaula/db.sqlite``, override ``KAULA_DB``) holds while tests can still
relocate the whole tree with ``KAULA_HOME``.
"""

from __future__ import annotations

import os
from pathlib import Path


def home_dir() -> Path:
    """Root directory for all kaula state."""
    override = os.environ.get("KAULA_HOME")
    if override:
        return Path(override).expanduser()
    return Path.home() / ".kaula"


def db_path() -> Path:
    override = os.environ.get("KAULA_DB")
    if override:
        return Path(override).expanduser()
    return home_dir() / "db.sqlite"


def spool_dir() -> Path:
    return home_dir() / "spool"


def redact_path() -> Path:
    return home_dir() / "redact.toml"


def lock_path() -> Path:
    return home_dir() / "drain.lock"


def ensure_dirs() -> None:
    home_dir().mkdir(parents=True, exist_ok=True)
    spool_dir().mkdir(parents=True, exist_ok=True)
    # The db may be relocated by KAULA_DB; make sure its parent exists too.
    db_path().parent.mkdir(parents=True, exist_ok=True)
