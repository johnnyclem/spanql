"""Neutral data model shared by segmenters (ingest) and the writer (store)."""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any


@dataclass(slots=True)
class Block:
    """A retrieval unit: a span plus optional typed side data.

    ``parent``/``cell['row']`` are indexes into the same block list; the writer
    turns them into block ids.
    """

    kind: str  # paragraph|heading|list_item|code_block|log_record|table_row|table_cell|line|chunk|module|class|function|method
    start: int
    end: int
    path: str | None = None
    page: int | None = None
    is_unit: bool = False  # the default counting unit for FIND
    fts: str | None = None  # text to index in FTS5 (None = not indexed)
    record: dict[str, Any] | None = None  # log_record side data
    row: dict[str, Any] | None = None  # table_row side data
    cell: dict[str, Any] | None = None  # table_cell side data
    symbol: dict[str, Any] | None = None  # code_symbol side data


@dataclass(slots=True)
class Document:
    """Result of converting + segmenting one source file."""

    data: bytes  # canonical UTF-8 text
    blocks: list[Block] = field(default_factory=list)
    segmentation: str = "text@1"
    offset_map: bytes | None = None
