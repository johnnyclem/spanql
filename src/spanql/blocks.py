"""Small shared data types."""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any


@dataclass
class BlockSpec:
    """A block produced by a converter/segmenter, addressed by canonical byte offsets."""

    kind: str
    start: int
    end: int
    path: str | None = None
    page: int | None = None
    log: dict[str, Any] | None = None  # ts, level, logger, message_start, message_end, fields
    row: dict[str, Any] | None = None  # table_id, row, fields
    cell: dict[str, Any] | None = None  # table_id, row, col, header, row_index (index into blocks)
    symbol: dict[str, Any] | None = None  # lang, kind, name, qualified_name, parent_index, calls
    index_text: bool = True  # include in FTS
    fts_text: str | None = None  # override indexed text (e.g. "header: value" for rows)


@dataclass
class Converted:
    """Output of a converter: canonical text, offset map and blocks."""

    text: str
    blocks: list[BlockSpec]
    converter: str
    offset_map: list[tuple[int, int]] | None = None  # (canonical line start, raw byte offset)
    meta: dict[str, Any] = field(default_factory=dict)
