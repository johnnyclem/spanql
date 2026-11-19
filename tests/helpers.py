from __future__ import annotations

from spanql.engine.handles import HandleTable, handle_from_span
from spanql.store.db import Span, Store


def line_span(store: Store, uri: str, needle: str) -> str:
    """Span id of the full line in ``uri`` containing ``needle``."""
    row = store.get_source(uri)
    assert row is not None, uri
    data = store.snapshot_text(row["current_snapshot"])
    i = data.index(needle.encode())
    s = data.rfind(b"\n", 0, i) + 1
    e = data.find(b"\n", i)
    e = len(data) if e == -1 else e
    return Span(row["current_snapshot"], s, e).id


def handles(store: Store, *specs: tuple[str, str]) -> HandleTable:
    return {
        i: handle_from_span(store, i, line_span(store, u, n)) for i, (u, n) in enumerate(specs, 1)
    }


Q3 = ("demo/reports/q3-2026.md", "$4.82 million")
Q2 = ("demo/reports/q2-2026.md", "$4.31M")
