"""Span identity: (snapshot, byte start, byte end) over canonical UTF-8 text."""

from __future__ import annotations

import hashlib
import re
from dataclasses import dataclass

SNAPSHOT_PREFIX = 8
_SPAN_RE = re.compile(r"^sp:([0-9a-f]{4,64}):(\d+):(\d+)$")


def sha256_hex(data: bytes) -> str:
    return hashlib.sha256(data).hexdigest()


@dataclass(frozen=True, slots=True)
class Span:
    """Half-open byte range into a snapshot's canonical text."""

    snapshot_id: str  # full sha256 of the canonical text
    start: int
    end: int
    text_sha256: str  # sha256 of the covered bytes (corruption guard)

    @property
    def id(self) -> str:
        return f"sp:{self.snapshot_id[:SNAPSHOT_PREFIX]}:{self.start}:{self.end}"

    def __len__(self) -> int:
        return self.end - self.start


def parse_span_id(text: str) -> tuple[str, int, int]:
    """Parse ``sp:<prefix>:<start>:<end>`` into (snapshot prefix, start, end)."""
    m = _SPAN_RE.match(text.strip())
    if not m:
        raise ValueError(f"not a span id: {text!r} (expected sp:<snapshot>:<start>:<end>)")
    start, end = int(m.group(2)), int(m.group(3))
    if end < start:
        raise ValueError(f"span end before start: {text!r}")
    return m.group(1), start, end
