"""Normalization (canonical UTF-8 text + offset map) and char/byte offset helpers."""

from __future__ import annotations

import re
import unicodedata
from bisect import bisect_right

_LINE_RE = re.compile(rb"\r\n|\r|\n")


def decode(raw: bytes) -> str:
    """Decode bytes as UTF-8 (BOM stripped); fall back to cp1252 then latin-1."""
    if raw.startswith(b"\xef\xbb\xbf"):
        raw = raw[3:]
    for enc in ("utf-8", "cp1252"):
        try:
            return raw.decode(enc)
        except UnicodeDecodeError:
            continue
    return raw.decode("latin-1")


def normalize(raw: bytes) -> tuple[str, list[tuple[int, int]] | None]:
    """Return canonical text (UTF-8, ``\\n`` line endings, NFC) and an offset map.

    The offset map lists ``(canonical_line_start_byte, raw_line_start_byte)`` for every line,
    or ``None`` when canonical bytes equal raw bytes (identity map).
    """
    bom = 3 if raw.startswith(b"\xef\xbb\xbf") else 0
    lines: list[tuple[int, bytes]] = []  # (raw start, raw line content)
    pos = bom
    for m in _LINE_RE.finditer(raw, bom):
        lines.append((pos, raw[pos : m.start()]))
        pos = m.end()
    lines.append((pos, raw[pos:]))

    out: list[str] = []
    mapping: list[tuple[int, int]] = []
    canon_pos = 0
    for i, (raw_start, content) in enumerate(lines):
        text = unicodedata.normalize("NFC", decode(content)) if content else ""
        mapping.append((canon_pos, raw_start))
        out.append(text)
        canon_pos += len(text.encode("utf-8")) + (1 if i < len(lines) - 1 else 0)
    canonical = "\n".join(out)
    if canonical.encode("utf-8") == raw:
        return canonical, None
    return canonical, mapping


def raw_offset(offset_map: list[tuple[int, int]] | None, canonical_offset: int) -> int:
    """Map a canonical byte offset to the raw file (line-start precision + column delta)."""
    if not offset_map:
        return canonical_offset
    starts = [c for c, _ in offset_map]
    i = max(0, bisect_right(starts, canonical_offset) - 1)
    c, r = offset_map[i]
    return r + (canonical_offset - c)


class ByteIndex:
    """Convert character offsets in ``text`` to UTF-8 byte offsets."""

    def __init__(self, text: str):
        self.text = text
        self.ascii = text.isascii()
        self._cum: list[int] | None = None

    def __call__(self, char_offset: int) -> int:
        if self.ascii:
            return char_offset
        if self._cum is None:
            cum = [0]
            total = 0
            for ch in self.text:
                o = ord(ch)
                total += 1 if o < 0x80 else 2 if o < 0x800 else 3 if o < 0x10000 else 4
                cum.append(total)
            self._cum = cum
        return self._cum[char_offset]


def line_spans(text: str) -> list[tuple[int, int]]:
    """Char (start, end) of each line, excluding the newline."""
    spans = []
    pos = 0
    for line in text.split("\n"):
        spans.append((pos, pos + len(line)))
        pos += len(line) + 1
    return spans


def strip_span(text: str, start: int, end: int) -> tuple[int, int]:
    """Shrink [start, end) to exclude leading/trailing whitespace."""
    while start < end and text[start].isspace():
        start += 1
    while end > start and text[end - 1].isspace():
        end -= 1
    return start, end
