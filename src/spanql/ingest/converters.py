"""Converters for documents and files: plain text, Markdown, CSV/TSV, JSON/YAML/TOML, HTML, PDF.

Each converter returns a :class:`~spanql.blocks.Converted` whose blocks address the canonical
text by UTF-8 byte offsets.
"""

from __future__ import annotations

import io
import re
import unicodedata
from html.parser import HTMLParser

from spanql.blocks import BlockSpec, Converted
from spanql.ingest.text import ByteIndex, line_spans, normalize, strip_span

MAX_BLOCK_CHARS = 1500
CHUNK_LINES = 40

_HEADING_RE = re.compile(r"^(#{1,6})\s+(.*?)\s*#*\s*$")
_FENCE_RE = re.compile(r"^\s*(```|~~~)")
_LIST_RE = re.compile(r"^\s*(?:[-*+]|\d+[.)])\s+")
_TABLE_ROW_RE = re.compile(r"^\s*\|.*\|\s*$")
_TABLE_SEP_RE = re.compile(r"^\s*\|?\s*:?-{2,}:?\s*(\|\s*:?-{2,}:?\s*)*\|?\s*$")


def _split_long(
    text: str, start: int, end: int, kind: str, path: str | None, bi: ByteIndex
) -> list[BlockSpec]:
    """Emit one block, or several line-aligned blocks if it's longer than MAX_BLOCK_CHARS."""
    start, end = strip_span(text, start, end)
    if start >= end:
        return []
    if end - start <= MAX_BLOCK_CHARS:
        return [BlockSpec(kind, bi(start), bi(end), path=path)]
    out: list[BlockSpec] = []
    cur = start
    while cur < end:
        cut = min(end, cur + MAX_BLOCK_CHARS)
        if cut < end:
            nl = text.rfind("\n", cur, cut)
            if nl > cur:
                cut = nl
            else:
                sp = text.rfind(" ", cur, cut)
                cut = sp if sp > cur else cut
        s, e = strip_span(text, cur, cut)
        if s < e:
            out.append(BlockSpec(kind, bi(s), bi(e), path=path))
        cur = max(cut, cur + 1)
    return out


def convert_plain(raw: bytes) -> Converted:
    text, omap = normalize(raw)
    bi = ByteIndex(text)
    blocks: list[BlockSpec] = []
    for s, e in _paragraphs(text):
        blocks.extend(_split_long(text, s, e, "paragraph", None, bi))
    return Converted(text, blocks, "plain/1", omap)


def _paragraphs(text: str) -> list[tuple[int, int]]:
    out = []
    start = None
    for s, e in line_spans(text):
        blank = not text[s:e].strip()
        if blank and start is not None:
            out.append((start, s - 1))
            start = None
        elif not blank and start is None:
            start = s
    if start is not None:
        out.append((start, len(text)))
    return out


def convert_markdown(raw: bytes) -> Converted:
    text, omap = normalize(raw)
    bi = ByteIndex(text)
    blocks: list[BlockSpec] = []
    heads: list[str] = []
    lines = line_spans(text)
    para_start: int | None = None
    para_kind = "paragraph"
    i = 0
    table_n = 0

    def path() -> str | None:
        return " > ".join(heads) if heads else None

    def flush(upto: int) -> None:
        nonlocal para_start
        if para_start is not None:
            blocks.extend(_split_long(text, para_start, upto, para_kind, path(), bi))
            para_start = None

    while i < len(lines):
        s, e = lines[i]
        line = text[s:e]
        if _FENCE_RE.match(line):
            flush(s)
            fence = _FENCE_RE.match(line).group(1)  # type: ignore[union-attr]
            j = i + 1
            while j < len(lines) and not text[lines[j][0] : lines[j][1]].strip().startswith(fence):
                j += 1
            end = lines[min(j, len(lines) - 1)][1]
            blocks.extend(_split_long(text, s, end, "code_fence", path(), bi))
            i = j + 1
            continue
        m = _HEADING_RE.match(line)
        if m:
            flush(s)
            level = len(m.group(1))
            heads[:] = [*heads[: level - 1], m.group(2)]
            blocks.append(BlockSpec("heading", bi(s), bi(e), path=path()))
            i += 1
            continue
        if (
            _TABLE_ROW_RE.match(line)
            and i + 1 < len(lines)
            and _TABLE_SEP_RE.match(text[lines[i + 1][0] : lines[i + 1][1]])
        ):
            flush(s)
            j = i + 2
            while j < len(lines) and _TABLE_ROW_RE.match(text[lines[j][0] : lines[j][1]]):
                j += 1
            blocks.extend(_markdown_table(text, lines, i, j, f"md{table_n}", bi, path()))
            table_n += 1
            i = j
            continue
        if not line.strip():
            flush(s)
        elif _LIST_RE.match(line):
            flush(s)
            para_start, para_kind = s, "list_item"
        elif para_start is None:
            para_start, para_kind = s, "paragraph"
        i += 1
    flush(len(text))
    return Converted(text, blocks, "markdown/1", omap)


def _md_cells(text: str, s: int, e: int) -> list[tuple[int, int]]:
    line = text[s:e]
    first = line.index("|")
    last = line.rindex("|")
    cells = []
    cur = first + 1
    for k in range(first + 1, last + 1):
        if line[k] == "|" and (k == 0 or line[k - 1] != "\\"):
            cs, ce = strip_span(text, s + cur, s + k)
            cells.append((cs, ce))
            cur = k + 1
    return cells


def _markdown_table(
    text: str,
    lines: list[tuple[int, int]],
    hi: int,
    end: int,
    table_id: str,
    bi: ByteIndex,
    path: str | None,
) -> list[BlockSpec]:
    headers = [text[a:b] for a, b in _md_cells(text, *lines[hi])]
    blocks = [BlockSpec("table_header", bi(lines[hi][0]), bi(lines[hi][1]), path=path)]
    for r, li in enumerate(range(hi + 2, end), start=1):
        s, e = lines[li]
        cells = _md_cells(text, s, e)
        fields = {
            (headers[c] if c < len(headers) else f"col{c + 1}"): text[a:b]
            for c, (a, b) in enumerate(cells)
        }
        row_index = len(blocks)
        blocks.append(
            BlockSpec(
                "table_row",
                bi(s),
                bi(e),
                path=f"{table_id} row {r}",
                row={"table_id": table_id, "row": r, "fields": fields},
                fts_text=" ; ".join(f"{k}: {v}" for k, v in fields.items()),
            )
        )
        for c, (a, b) in enumerate(cells):
            header = headers[c] if c < len(headers) else f"col{c + 1}"
            blocks.append(
                BlockSpec(
                    "table_cell",
                    bi(a),
                    bi(b),
                    path=f"{table_id} row {r} [{header}]",
                    cell={
                        "table_id": table_id,
                        "row": r,
                        "col": c + 1,
                        "header": header,
                        "row_index": row_index,
                    },
                    index_text=False,
                )
            )
    return blocks


def _scan_delimited(text: str, delim: str) -> list[list[tuple[int, int, str]]]:
    """RFC 4180-ish scanner returning rows of (char start, char end, value) per cell.

    The span of a quoted cell excludes the surrounding quotes; doubled quotes inside are part
    of the span and unescaped in ``value``.
    """
    rows: list[list[tuple[int, int, str]]] = []
    row: list[tuple[int, int, str]] = []
    i, n = 0, len(text)
    while i <= n:
        if i < n and text[i] == '"':
            j = i + 1
            buf = io.StringIO()
            while j < n:
                if text[j] == '"':
                    if j + 1 < n and text[j + 1] == '"':
                        buf.write('"')
                        j += 2
                        continue
                    break
                buf.write(text[j])
                j += 1
            row.append((i + 1, j, buf.getvalue()))
            j += 1
            while j < n and text[j] not in (delim, "\n"):
                j += 1
        else:
            j = i
            while j < n and text[j] not in (delim, "\n"):
                j += 1
            a, b = strip_span(text, i, j)
            row.append((a, b, text[a:b]))
        if j >= n or text[j] == "\n":
            if any(v for _, _, v in row) or len(row) > 1:
                rows.append(row)
            row = []
            if j >= n:
                break
        i = j + 1
    return rows


def convert_delimited(raw: bytes, delim: str = ",") -> Converted:
    text, omap = normalize(raw)
    bi = ByteIndex(text)
    rows = _scan_delimited(text, delim)
    blocks: list[BlockSpec] = []
    if not rows:
        return Converted(text, blocks, f"delimited/1{delim!r}", omap)
    header_cells = rows[0]
    headers = [v or f"col{c + 1}" for c, (_, _, v) in enumerate(header_cells)]
    blocks.append(BlockSpec("table_header", bi(header_cells[0][0]), bi(header_cells[-1][1])))
    for r, row in enumerate(rows[1:], start=1):
        fields = {
            (headers[c] if c < len(headers) else f"col{c + 1}"): v
            for c, (_, _, v) in enumerate(row)
        }
        rs = row[0][0]
        re_ = row[-1][1]
        # widen row span to cover quotes
        while rs > 0 and text[rs - 1] == '"':
            rs -= 1
        while re_ < len(text) and text[re_] == '"':
            re_ += 1
        row_index = len(blocks)
        blocks.append(
            BlockSpec(
                "table_row",
                bi(rs),
                bi(re_),
                path=f"row {r}",
                row={"table_id": "t0", "row": r, "fields": fields},
                fts_text=" ; ".join(f"{k}: {v}" for k, v in fields.items()),
            )
        )
        for c, (a, b, _) in enumerate(row):
            header = headers[c] if c < len(headers) else f"col{c + 1}"
            blocks.append(
                BlockSpec(
                    "table_cell",
                    bi(a),
                    bi(b),
                    path=f"row {r} [{header}]",
                    cell={
                        "table_id": "t0",
                        "row": r,
                        "col": c + 1,
                        "header": header,
                        "row_index": row_index,
                    },
                    index_text=False,
                )
            )
    return Converted(
        text, blocks, "delimited/1", omap, meta={"columns": headers, "rows": len(rows) - 1}
    )


def convert_lines(raw: bytes, name: str = "structured/1") -> Converted:
    """JSON/YAML/TOML/INI and unknown text formats: line-aligned chunks split at blank lines
    and top-level keys, so quotes stay small and line numbers stay meaningful."""
    text, omap = normalize(raw)
    bi = ByteIndex(text)
    lines = line_spans(text)
    blocks: list[BlockSpec] = []
    start: int | None = None
    count = 0
    for s, e in lines:
        line = text[s:e]
        toplevel = bool(line) and not line[0].isspace() and line.strip() not in {"}", "]", "},"}
        if start is not None and (
            not line.strip() or count >= CHUNK_LINES or (toplevel and count >= 8)
        ):
            blocks.extend(_split_long(text, start, s, "chunk", None, bi))
            start, count = None, 0
        if line.strip():
            if start is None:
                start = s
            count += 1
    if start is not None:
        blocks.extend(_split_long(text, start, len(text), "chunk", None, bi))
    return Converted(text, blocks, name, omap)


class _HTMLText(HTMLParser):
    BLOCK = {
        "p",
        "div",
        "br",
        "li",
        "tr",
        "h1",
        "h2",
        "h3",
        "h4",
        "h5",
        "h6",
        "pre",
        "section",
        "article",
        "header",
        "footer",
        "table",
        "ul",
        "ol",
        "blockquote",
        "td",
        "th",
    }
    SKIP = {"script", "style", "noscript", "template"}

    def __init__(self) -> None:
        super().__init__(convert_charrefs=True)
        self.parts: list[str] = []
        self.skip = 0

    def handle_starttag(self, tag: str, attrs: list[tuple[str, str | None]]) -> None:
        if tag in self.SKIP:
            self.skip += 1
        elif tag in self.BLOCK:
            self.parts.append("\n\n" if tag in {"p", "div", "table", "section"} else "\n")
            if tag.startswith("h") and len(tag) == 2:
                self.parts.append("#" * int(tag[1]) + " ")

    def handle_endtag(self, tag: str) -> None:
        if tag in self.SKIP:
            self.skip = max(0, self.skip - 1)
        elif tag in self.BLOCK:
            self.parts.append("\n")

    def handle_data(self, data: str) -> None:
        if not self.skip:
            self.parts.append(re.sub(r"[ \t\r\f\v]+", " ", data))


def convert_html(raw: bytes) -> Converted:
    from spanql.ingest.text import decode

    p = _HTMLText()
    p.feed(decode(raw))
    text = re.sub(r"\n[ \t]+", "\n", "".join(p.parts))
    text = re.sub(r"\n{3,}", "\n\n", text).strip() + "\n"
    conv = convert_markdown(text.encode("utf-8"))
    # Extracted text has no line-level correspondence with the raw HTML.
    return Converted(conv.text, conv.blocks, "html/1", None, meta={"derived": True})


def convert_pdf(raw: bytes) -> Converted:
    """Lightweight PDF text extraction via the optional ``pypdf`` extra (Docling is M8)."""
    try:
        from pypdf import PdfReader
    except ImportError as exc:  # pragma: no cover - depends on extra
        raise RuntimeError("PDF support needs the 'pdf' extra: pip install 'spanql[pdf]'") from exc
    reader = PdfReader(io.BytesIO(raw))
    parts: list[str] = []
    page_ranges: list[tuple[int, int, int]] = []
    pos = 0
    for n, page in enumerate(reader.pages, start=1):
        t = (page.extract_text() or "").replace("\r\n", "\n").replace("\r", "\n").strip()
        t = unicodedata.normalize("NFC", t)
        parts.append(t)
        page_ranges.append((pos, pos + len(t), n))
        pos += len(t) + 2
    text = "\n\n".join(parts)
    bi = ByteIndex(text)
    blocks: list[BlockSpec] = []
    for ps, pe, n in page_ranges:
        sub = text[ps:pe]
        for s, e in _paragraphs(sub):
            for b in _split_long(text, ps + s, ps + e, "paragraph", f"p.{n}", bi):
                b.page = n
                blocks.append(b)
    return Converted(text, blocks, "pdf-pypdf/1", None, meta={"pages": len(page_ranges)})
