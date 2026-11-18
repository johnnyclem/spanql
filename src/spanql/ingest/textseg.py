"""Segmenters for prose (Markdown/plain text) and generic line/chunk-oriented files."""

from __future__ import annotations

import re

from ..blocks import Block

_HEADING = re.compile(rb"^(#{1,6})[ \t]+(.+?)[ \t]*#*[ \t]*$")
_LIST = re.compile(rb"^[ \t]*(?:[-*+]|\d{1,9}[.)])[ \t]+\S")
_FENCE = re.compile(rb"^[ \t]{0,3}(`{3,}|~{3,})")

MAX_CHUNK_LINES = 40


def line_spans(data: bytes) -> list[tuple[int, int]]:
    """(start, end) of every line's content, excluding the ``\\n``."""
    spans, pos = [], 0
    nl = data.find(b"\n")
    while nl != -1:
        spans.append((pos, nl))
        pos = nl + 1
        nl = data.find(b"\n", pos)
    if pos < len(data):
        spans.append((pos, len(data)))
    return spans


def _trim(data: bytes, start: int, end: int) -> tuple[int, int]:
    while start < end and data[start] in b" \t\r":
        start += 1
    while end > start and data[end - 1] in b" \t\r":
        end -= 1
    return start, end


def segment_prose(data: bytes, *, markdown: bool) -> list[Block]:
    """Paragraphs, plus headings / list items / fenced code when ``markdown``."""
    lines = line_spans(data)
    blocks: list[Block] = []
    stack: list[tuple[int, str]] = []  # (level, title)
    cur: tuple[str, int, int] | None = None  # (kind, first_line, last_line)
    fence: tuple[bytes, int] | None = None  # (marker, first_line)

    def path() -> str | None:
        return " > ".join(t for _, t in stack) or None

    def emit(kind: str, first: int, last: int, p: str | None) -> None:
        s, e = _trim(data, lines[first][0], lines[last][1])
        if e > s:
            text = data[s:e].decode("utf-8", "replace")
            # index the heading path with the text so "recommendation"/"deployment" in a heading finds its section
            blocks.append(Block(kind, s, e, path=p, is_unit=True, fts=(p + "\n" + text) if p else text))

    def flush() -> None:
        nonlocal cur
        if cur is not None:
            emit(cur[0], cur[1], cur[2], path())
            cur = None

    for i, (s, e) in enumerate(lines):
        line = data[s:e]
        if fence is not None:
            m = _FENCE.match(line)
            if m and m.group(1)[:1] == fence[0][:1] and len(m.group(1)) >= len(fence[0]) and not line.strip(b" \t`~"):
                emit("code_block", fence[1], i, path())
                fence = None
            continue
        if not line.strip():
            flush()
            continue
        if markdown:
            m = _FENCE.match(line)
            if m:
                flush()
                fence = (m.group(1), i)
                continue
            m = _HEADING.match(line)
            if m:
                flush()
                level, title = len(m.group(1)), m.group(2).decode("utf-8", "replace").strip()
                while stack and stack[-1][0] >= level:
                    stack.pop()
                stack.append((level, title))
                emit("heading", i, i, path())
                continue
            if _LIST.match(line):
                flush()
                cur = ("list_item", i, i)
                continue
        if cur is None:
            cur = ("paragraph", i, i)
        else:
            cur = (cur[0], cur[1], i)
    if fence is not None:  # unterminated fence runs to EOF
        emit("code_block", fence[1], len(lines) - 1, path())
    flush()
    return blocks


def segment_lines(data: bytes, *, fts: bool = True) -> list[Block]:
    """One block per non-blank line (grep-style unit for code and config files)."""
    blocks = []
    for s, e in line_spans(data):
        s, e = _trim(data, s, e)
        if e > s:
            blocks.append(Block("line", s, e, is_unit=True, fts=data[s:e].decode("utf-8", "replace") if fts else None))
    return blocks


def segment_chunks(data: bytes, *, max_lines: int = MAX_CHUNK_LINES) -> list[Block]:
    """Blank-line-separated chunks (capped) used as FTS units for code we cannot parse."""
    blocks: list[Block] = []
    lines = line_spans(data)
    first = None
    for i, (s, e) in enumerate(lines + [(len(data), len(data))]):
        blank = i >= len(lines) or not data[s:e].strip()
        if not blank and first is None:
            first = i
        if first is not None and (blank or i - first + 1 >= max_lines):
            last = i - 1 if blank else i
            cs, ce = _trim(data, lines[first][0], lines[last][1])
            if ce > cs:
                blocks.append(Block("chunk", cs, ce, fts=data[cs:ce].decode("utf-8", "replace")))
            first = None
    return blocks
