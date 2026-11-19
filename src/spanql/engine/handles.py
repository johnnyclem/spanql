"""Handle tables: the numbered candidate spans ($1..$k) a formula may reference."""

from __future__ import annotations

from dataclasses import dataclass
from typing import Any

from spanql.store.db import Span, Store


@dataclass
class Handle:
    n: int
    span: Span
    text: str
    text_sha256: str
    source: str
    line_start: int
    line_end: int
    block_kind: str | None = None
    path: str | None = None
    page: int | None = None
    score: float | None = None

    def location(self) -> str:
        loc = (
            f"L{self.line_start}"
            if self.line_start == self.line_end
            else (f"L{self.line_start}-L{self.line_end}")
        )
        if self.page:
            loc = f"p.{self.page} · {loc}"
        return loc

    def to_dict(self) -> dict[str, Any]:
        return {
            "handle": f"${self.n}",
            "span": self.span.id,
            "source": self.source,
            "location": self.location(),
            "text": self.text,
            "kind": self.block_kind,
            "path": self.path,
            "score": self.score,
        }


HandleTable = dict[int, Handle]


def handle_from_span(store: Store, n: int, span: Span | str, **extra: Any) -> Handle:
    rs = store.resolve(span)
    return Handle(
        n,
        rs.span,
        rs.text,
        rs.text_sha256,
        rs.source_uri,
        rs.line_start,
        rs.line_end,
        page=rs.page,
        **extra,
    )


def handles_from_ids(store: Store, mapping: dict[str, str]) -> HandleTable:
    """Build a table from {"1": "sp:…", "$2": "sp:…"}."""
    table: HandleTable = {}
    for k, v in mapping.items():
        n = int(str(k).lstrip("$"))
        table[n] = handle_from_span(store, n, v)
    return table
