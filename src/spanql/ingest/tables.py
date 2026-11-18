"""CSV/TSV and JSON-array tables. Cells are cited as bytes *and* as (row, column).

Scanning is done on bytes: delimiters, quotes and brackets are ASCII, so byte
offsets are exact without any char<->byte conversion.
"""

from __future__ import annotations

import json
import re
from dataclasses import dataclass

from ..blocks import Block

MAX_DEPTH = 64
_NUMBERISH = re.compile(r"^[+-]?[\d,]*\.?\d+%?$")


@dataclass(slots=True)
class RawCell:
    start: int  # content span (inside quotes for quoted cells)
    end: int
    value: str  # unescaped value
    outer_start: int  # including quotes
    outer_end: int


def parse_csv(data: bytes, delim: bytes = b",") -> list[list[RawCell]]:
    """Tokenize CSV (RFC 4180: quotes, ``""`` escapes, embedded newlines) with byte offsets."""
    n = len(data)
    special = re.compile(re.escape(delim) + rb"|\n")
    rows: list[list[RawCell]] = []
    row: list[RawCell] = []
    i = 0
    while i <= n:
        if i < n and data[i : i + 1] == b'"':
            j, buf = i + 1, bytearray()
            while True:
                k = data.find(b'"', j)
                if k == -1:  # unterminated: take the rest
                    buf += data[j:n]
                    cell = RawCell(i + 1, n, buf.decode("utf-8", "replace"), i, n)
                    i = n
                    break
                if data[k + 1 : k + 2] == b'"':
                    buf += data[j : k + 1]
                    j = k + 2
                    continue
                buf += data[j:k]
                cell = RawCell(i + 1, k, buf.decode("utf-8", "replace"), i, k + 1)
                i = k + 1
                # lenient: stray characters between the closing quote and the delimiter
                m = special.search(data, i)
                i = m.start() if m else n
                break
        else:
            m = special.search(data, i)
            k = m.start() if m else n
            end = k
            if end > i and data[end - 1 : end] == b"\r":
                end -= 1
            cell = RawCell(i, end, data[i:end].decode("utf-8", "replace"), i, end)
            i = k
        row.append(cell)
        if i >= n:
            break
        if data[i : i + 1] == b"\n":
            rows.append(row)
            row = []
            i += 1
            if i >= n:
                break
        else:  # delimiter
            i += 1
            if i >= n:
                row.append(RawCell(n, n, "", n, n))
                break
    if row:
        rows.append(row)
    return [r for r in rows if not (len(r) == 1 and r[0].value.strip() == "")]


def _has_header(first: list[RawCell]) -> bool:
    vals = [c.value.strip() for c in first]
    return bool(vals) and all(v and not _NUMBERISH.match(v) for v in vals)


def segment_csv(data: bytes, *, delim: bytes = b",") -> list[Block]:
    rows = parse_csv(data, delim)
    if not rows:
        return []
    if _has_header(rows[0]):
        headers = [c.value.strip() for c in rows[0]]
        body = rows[1:]
        first_row_no = 2
    else:
        headers = [f"col{i + 1}" for i in range(max(len(r) for r in rows))]
        body = rows
        first_row_no = 1
    seen: dict[str, int] = {}
    for i, h in enumerate(headers):  # de-duplicate header names
        if h in seen:
            seen[h] += 1
            headers[i] = f"{h}_{seen[h]}"
        else:
            seen[h] = 1
    blocks: list[Block] = []
    for ridx, row in enumerate(body):
        row_no = first_row_no + ridx
        fields = {headers[ci] if ci < len(headers) else f"col{ci + 1}": c.value for ci, c in enumerate(row)}
        row_start, row_end = row[0].outer_start, row[-1].outer_end
        row_block = len(blocks)
        blocks.append(
            Block(
                "table_row", row_start, row_end, path=f"row {row_no}", is_unit=True,
                fts="\n".join(f"{k} {v}" for k, v in fields.items()),
                row={"table_id": 0, "row": row_no, "fields": fields},
            )
        )
        for ci, c in enumerate(row):
            header = headers[ci] if ci < len(headers) else f"col{ci + 1}"
            blocks.append(
                Block(
                    "table_cell", c.start, c.end, path=f'row {row_no}, column "{header}"',
                    cell={"table_id": 0, "row": row_no, "col": ci + 1, "header": header, "row_block": row_block},
                )
            )
    return blocks


# ------------------------------------------------------------------ JSON


class _Scanner:
    """Minimal offset-tracking JSON scanner (validates structure; values via ``json.loads``)."""

    def __init__(self, data: bytes):
        self.d = data
        self.n = len(data)

    def ws(self, i: int) -> int:
        d = self.d
        while i < self.n and d[i] in b" \t\r\n":
            i += 1
        return i

    def string_end(self, i: int) -> int:
        """``i`` at the opening quote; returns index after the closing quote."""
        j = i + 1
        while True:
            k = self.d.find(b'"', j)
            if k == -1:
                raise ValueError("unterminated string")
            bs, m = 0, k - 1
            while m > i and self.d[m] == 0x5C:
                bs += 1
                m -= 1
            if bs % 2 == 0:
                return k + 1
            j = k + 1

    def value(self, i: int, depth: int = 0):
        """Returns (end, node). node = ('obj', [(key, key_node, val_node)]) | ('arr', [nodes]) | ('scalar', kind)."""
        if depth > MAX_DEPTH:
            raise ValueError("too deeply nested")
        i = self.ws(i)
        if i >= self.n:
            raise ValueError("unexpected end")
        c = self.d[i : i + 1]
        if c == b"{":
            start = i
            members, i = [], self.ws(i + 1)
            if self.d[i : i + 1] == b"}":
                return i + 1, (start, i + 1, "obj", [])
            while True:
                i = self.ws(i)
                if self.d[i : i + 1] != b'"':
                    raise ValueError("expected object key")
                kend = self.string_end(i)
                key = json.loads(self.d[i:kend])
                i = self.ws(kend)
                if self.d[i : i + 1] != b":":
                    raise ValueError("expected ':'")
                vstart = self.ws(i + 1)
                i, node = self.value(vstart, depth + 1)
                members.append((key, vstart, i, node))
                i = self.ws(i)
                nxt = self.d[i : i + 1]
                i += 1
                if nxt == b"}":
                    return i, (start, i, "obj", members)
                if nxt != b",":
                    raise ValueError("expected ',' or '}'")
        if c == b"[":
            start = i
            items, i = [], self.ws(i + 1)
            if self.d[i : i + 1] == b"]":
                return i + 1, (start, i + 1, "arr", [])
            while True:
                i, node = self.value(i, depth + 1)
                items.append(node)
                i = self.ws(i)
                nxt = self.d[i : i + 1]
                i += 1
                if nxt == b"]":
                    return i, (start, i, "arr", items)
                if nxt != b",":
                    raise ValueError("expected ',' or ']'")
        if c == b'"':
            end = self.string_end(i)
            return end, (i, end, "str", None)
        m = re.compile(rb"-?\d+(?:\.\d+)?(?:[eE][+-]?\d+)?|true|false|null").match(self.d, i)
        if not m:
            raise ValueError(f"unexpected character at {i}")
        return m.end(), (i, m.end(), "lit", None)


def segment_json(data: bytes) -> list[Block] | None:
    """Top-level array of objects -> rows + scalar cells. Returns None when not a table."""
    sc = _Scanner(data)
    try:
        i = sc.ws(0)
        if data[i : i + 1] != b"[":
            return None
        end, root = sc.value(i)
        if sc.ws(end) != len(data):
            return None
    except (ValueError, RecursionError):
        return None
    _, _, kind, items = root
    if kind != "arr" or not items or any(n[2] != "obj" for n in items):
        return None
    blocks: list[Block] = []
    for ridx, (s, e, _, members) in enumerate(items, start=1):
        try:
            fields = json.loads(data[s:e])
        except ValueError:
            return None
        row_block = len(blocks)
        blocks.append(
            Block(
                "table_row", s, e, path=f"row {ridx}", is_unit=True,
                fts="\n".join(f"{k} {v}" for k, v in fields.items() if not isinstance(v, (dict, list))),
                row={"table_id": 0, "row": ridx, "fields": fields},
            )
        )
        for col, (key, vs, ve, node) in enumerate(members, start=1):
            vkind = node[2]
            if vkind in ("obj", "arr") or data[vs:ve] == b"null":
                continue
            cs, ce = (vs + 1, ve - 1) if vkind == "str" else (vs, ve)
            blocks.append(
                Block("table_cell", cs, ce, path=f'row {ridx}, column "{key}"',
                      cell={"table_id": 0, "row": ridx, "col": col, "header": key, "row_block": row_block})
            )
    return blocks
