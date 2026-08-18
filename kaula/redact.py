"""Redaction. Runs before any write — secrets land in prompts constantly.

Redacted spans become ``[REDACTED:<kind>]``. Content hashes are always computed
over the *pre-redaction* text (see callers), so redacting never moves a hash and
tightening the rules later never breaks existing attribution.

Rules (defaults, extendable via ``~/.kaula/redact.toml``):

* high-entropy strings ≥ 20 chars matching common key shapes (AWS, GitHub PAT,
  JWT, private-key headers, ``sk-``-style prefixes)
* values assigned to identifiers matching ``(?i)(secret|token|password|api[_-]?key)``
* anything matching a user-supplied deny-list regex
"""

from __future__ import annotations

import re
from pathlib import Path
from typing import List, Pattern, Tuple

from . import config

# (kind, compiled pattern). Order matters: more specific shapes first so the
# label is meaningful. Each pattern's group 0 is the span replaced.
_BUILTIN: List[Tuple[str, Pattern[str]]] = [
    ("private-key",
     re.compile(r"-----BEGIN (?:RSA |EC |OPENSSH |DSA |PGP )?PRIVATE KEY-----"
                r".*?-----END (?:RSA |EC |OPENSSH |DSA |PGP )?PRIVATE KEY-----",
                re.DOTALL)),
    ("aws-access-key", re.compile(r"\b(?:AKIA|ASIA)[0-9A-Z]{16}\b")),
    ("github-pat", re.compile(r"\bgh[pousr]_[A-Za-z0-9]{20,}\b")),
    ("github-pat", re.compile(r"\bgithub_pat_[A-Za-z0-9_]{20,}\b")),
    ("openai-key", re.compile(r"\bsk-[A-Za-z0-9_-]{20,}\b")),
    ("slack-token", re.compile(r"\bxox[baprs]-[A-Za-z0-9-]{10,}\b")),
    ("jwt", re.compile(r"\beyJ[A-Za-z0-9_-]{10,}\.[A-Za-z0-9_-]{10,}\.[A-Za-z0-9_-]{10,}\b")),
]

# Values assigned to secret-shaped identifiers:  secret = "…", API_KEY: '…', token=…
_SECRET_IDENT = re.compile(
    r"(?i)\b(?:secret|token|password|passwd|api[_-]?key|access[_-]?key)\b"
    r"\s*[:=]\s*"
    r"""(?P<q>['"]?)(?P<val>[^\s'"]{6,})(?P=q)"""
)


def _load_deny_regexes() -> List[Pattern[str]]:
    """User deny-list regexes from redact.toml.

    Parsed with a tiny hand-rolled reader so we keep the zero-dependency
    promise (``tomllib`` is 3.11+ only and this file is deliberately trivial).
    Expected form::

        deny = [
          "regex one",
          "regex two",
        ]
    """
    path = config.redact_path()
    if not path.exists():
        return []
    patterns: List[Pattern[str]] = []
    try:
        text = path.read_text(encoding="utf-8")
    except OSError:
        return []
    in_deny = False
    for raw in text.splitlines():
        line = raw.strip()
        if not line or line.startswith("#"):
            continue
        if line.startswith("deny"):
            in_deny = "[" in line
            after = line.split("[", 1)[1] if "[" in line else ""
            for tok in _extract_quoted(after):
                patterns.append(_safe_compile(tok))
            if "]" in after:
                in_deny = False
            continue
        if in_deny:
            for tok in _extract_quoted(line):
                patterns.append(_safe_compile(tok))
            if "]" in line:
                in_deny = False
    return [p for p in patterns if p is not None]


def _extract_quoted(fragment: str) -> List[str]:
    return re.findall(r'"((?:[^"\\]|\\.)*)"', fragment)


def _safe_compile(pattern: str):
    try:
        return re.compile(pattern)
    except re.error:
        return None


def redact(text: str) -> str:
    """Return ``text`` with secret-shaped spans replaced by ``[REDACTED:<kind>]``."""
    if not text:
        return text
    out = text
    for kind, pat in _BUILTIN:
        out = pat.sub(f"[REDACTED:{kind}]", out)
    out = _SECRET_IDENT.sub(_replace_assignment, out)
    for pat in _load_deny_regexes():
        out = pat.sub("[REDACTED:custom]", out)
    return out


def _replace_assignment(m: "re.Match[str]") -> str:
    # Preserve the identifier and separator; only mask the value.
    whole = m.group(0)
    val = m.group("val")
    idx = whole.rfind(val)
    if idx == -1:
        return whole
    return whole[:idx] + "[REDACTED:assigned-secret]" + whole[idx + len(val):]


def ensure_default_config() -> Path:
    """Write a documented default redact.toml if none exists. Returns its path."""
    path = config.redact_path()
    if path.exists():
        return path
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(
        "# kaula redaction rules\n"
        "# Built-in shapes (AWS, GitHub PAT, JWT, private keys, sk-/xox- prefixes,\n"
        "# and secret/token/password/api_key assignments) are always applied.\n"
        "# Add your own regexes to the deny list below; each match becomes\n"
        "# [REDACTED:custom]. text_hash is computed pre-redaction, so editing\n"
        "# these rules never invalidates existing attribution.\n"
        "\n"
        "deny = [\n"
        "  # \"AKIA[0-9A-Z]{16}\",\n"
        "]\n",
        encoding="utf-8",
    )
    return path
