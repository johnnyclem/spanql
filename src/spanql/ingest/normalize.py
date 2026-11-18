"""Canonical text: UTF-8, ``\\n`` line endings, NFC, no BOM.

Citations are always in canonical offsets. The offset map lets a UI open the
original file at the right place. It is ``None`` when the raw file is already
canonical (the common case), otherwise a zlib-compressed array of per-line
anchors.
"""

from __future__ import annotations

import re
import struct
import unicodedata
import zlib
from bisect import bisect_right
from dataclasses import dataclass

_EOL = re.compile(rb"\r\n|\n|\r")
# canon_start, raw_start, canon_len, raw_len (content only, no EOL), opaque (bytes differ => no linear map)
_ANCHOR = struct.Struct("<QQQQB")


@dataclass(slots=True)
class Normalized:
    data: bytes
    offset_map: bytes | None


def normalize(raw: bytes) -> Normalized:
    bom = 3 if raw.startswith(b"\xef\xbb\xbf") else 0
    body = raw[bom:]
    # fast path: valid UTF-8, no CR, already NFC
    if b"\r" not in body and not bom:
        try:
            text = body.decode("utf-8")
        except UnicodeDecodeError:
            pass
        else:
            if text.isascii() or unicodedata.is_normalized("NFC", text):
                return Normalized(body, None)

    out: list[bytes] = []
    anchors: list[bytes] = []
    identity = bom == 0
    canon_pos = 0
    pos = 0
    for m in _EOL.finditer(body):
        line = body[pos : m.start()]
        canon = _canon_line(line)
        anchors.append(_ANCHOR.pack(canon_pos, bom + pos, len(canon), len(line), canon != line))
        if canon != line or m.group() != b"\n":
            identity = False
        out.append(canon + b"\n")
        canon_pos += len(canon) + 1
        pos = m.end()
    tail = body[pos:]
    if tail:
        canon = _canon_line(tail)
        anchors.append(_ANCHOR.pack(canon_pos, bom + pos, len(canon), len(tail), canon != tail))
        if canon != tail:
            identity = False
        out.append(canon)
    data = b"".join(out)
    if identity:
        return Normalized(data, None)
    return Normalized(data, zlib.compress(b"".join(anchors), 6))


def _canon_line(line: bytes) -> bytes:
    text = line.decode("utf-8", "replace")
    return unicodedata.normalize("NFC", text).encode("utf-8")


def canonical_to_raw(offset_map: bytes | None, offset: int, *, end: bool = False) -> int:
    """Map a canonical byte offset to a raw-file byte offset.

    Inside a line whose bytes changed (NFC, invalid UTF-8) there is no linear
    map, so the offset snaps to the start of that line (or its end when ``end``).
    """
    if offset_map is None:
        return offset
    raw = zlib.decompress(offset_map)
    n = len(raw) // _ANCHOR.size
    starts = [struct.unpack_from("<Q", raw, i * _ANCHOR.size)[0] for i in range(n)]
    i = max(0, bisect_right(starts, offset) - 1)
    canon_start, raw_start, canon_len, raw_len, opaque = _ANCHOR.unpack_from(raw, i * _ANCHOR.size)
    rel = min(offset - canon_start, canon_len)
    if not opaque:
        return raw_start + rel
    return raw_start + raw_len if end else raw_start
