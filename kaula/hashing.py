"""Content hashing for position-independent attribution.

Line numbers recorded at edit time are worthless later — everything above a
line shifts as the file grows. Content hashes survive. So attribution matches
*content*, not position.

Normalisation, applied before hashing every line:

* strip trailing whitespace
* drop lines that are empty or whitespace-only
* do **not** lowercase

The same normalisation must be used when recording an edit and when hashing a
blamed line at query time, or the two will never meet.
"""

from __future__ import annotations

import hashlib
import json
from typing import List, Optional


def sha256_hex(text: str) -> str:
    return hashlib.sha256(text.encode("utf-8")).hexdigest()


def normalise_line(line: str) -> Optional[str]:
    """Return the normalised line, or ``None`` if it should be dropped."""
    stripped = line.rstrip()
    if stripped.strip() == "":
        return None
    return stripped


def line_hashes(text: str) -> List[str]:
    """Per-line sha256 of ``text`` after normalisation, empty lines dropped."""
    out: List[str] = []
    for raw in text.splitlines():
        norm = normalise_line(raw)
        if norm is None:
            continue
        out.append(sha256_hex(norm))
    return out


def line_hash(line: str) -> Optional[str]:
    """sha256 of a single normalised line, or ``None`` if it normalises away."""
    norm = normalise_line(line)
    if norm is None:
        return None
    return sha256_hex(norm)


def encode_hashes(hashes: List[str]) -> str:
    return json.dumps(hashes, separators=(",", ":"))


def decode_hashes(blob: str) -> List[str]:
    if not blob:
        return []
    try:
        value = json.loads(blob)
    except json.JSONDecodeError:
        return []
    if isinstance(value, list):
        return [h for h in value if isinstance(h, str)]
    return []
