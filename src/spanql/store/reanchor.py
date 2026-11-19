"""Re-anchoring: find a span from an old snapshot in its source's current snapshot.

Never rewrites a cell; it only reports whether the referenced text is unchanged, moved,
changed (with a fuzzy candidate) or missing in the current version.
"""

from __future__ import annotations

import difflib
from dataclasses import dataclass
from typing import Any

from spanql.store.db import Span, Store

CONTEXT_BYTES = 48


@dataclass
class Reanchor:
    status: str  # current | unchanged | moved | changed | missing
    old: str
    new: str | None = None
    ratio: float | None = None
    new_text: str | None = None

    def to_dict(self) -> dict[str, Any]:
        return {
            "status": self.status,
            "old": self.old,
            "new": self.new,
            "ratio": self.ratio,
            "new_text": self.new_text,
        }


def _find_all(hay: bytes, needle: bytes) -> list[int]:
    out, i = [], hay.find(needle)
    while i != -1 and needle:
        out.append(i)
        i = hay.find(needle, i + 1)
    return out


def reanchor(store: Store, span: Span | str) -> Reanchor:
    rs = store.resolve(span)
    old = rs.span
    row = store.one(
        "SELECT src.current_snapshot FROM snapshot s JOIN source src ON src.id = s.source_id"
        " WHERE s.id = ?",
        (old.snapshot_id,),
    )
    cur_id = row["current_snapshot"] if row else None
    if cur_id is None:
        return Reanchor("missing", old.id)
    if cur_id == old.snapshot_id:
        return Reanchor("current", old.id, old.id, 1.0, rs.text)
    old_data = store.snapshot_text(old.snapshot_id)
    new_data = store.snapshot_text(cur_id)
    needle = old_data[old.start : old.end]
    before = old_data[max(0, old.start - CONTEXT_BYTES) : old.start]
    after = old_data[old.end : old.end + CONTEXT_BYTES]

    with_ctx = _find_all(new_data, before + needle + after)
    if len(with_ctx) == 1:
        s = with_ctx[0] + len(before)
        new = Span(cur_id, s, s + len(needle))
        status = "unchanged" if s == old.start else "moved"
        return Reanchor(status, old.id, new.id, 1.0, needle.decode("utf-8", "replace"))
    exact = _find_all(new_data, needle)
    if len(exact) == 1:
        new = Span(cur_id, exact[0], exact[0] + len(needle))
        return Reanchor("moved", old.id, new.id, 1.0, needle.decode("utf-8", "replace"))
    if len(exact) > 1:
        # pick the occurrence nearest the old offset (deterministic), report as moved
        s = min(exact, key=lambda x: (abs(x - old.start), x))
        new = Span(cur_id, s, s + len(needle))
        return Reanchor("moved", old.id, new.id, 1.0, needle.decode("utf-8", "replace"))

    # fuzzy: best matching line window of the same line count near the old position
    old_text = needle.decode("utf-8", "replace")
    n_lines = old_text.count("\n") + 1
    new_lines = new_data.decode("utf-8", "replace").split("\n")
    offsets = [0]
    for ln in new_lines:
        offsets.append(offsets[-1] + len(ln.encode("utf-8")) + 1)
    best: tuple[float, int, int] = (0.0, 0, 0)
    for i in range(0, max(1, len(new_lines) - n_lines + 1)):
        cand = "\n".join(new_lines[i : i + n_lines])
        r = difflib.SequenceMatcher(None, old_text, cand, autojunk=False).ratio()
        if r > best[0]:
            best = (r, i, i + n_lines)
    if best[0] >= 0.6:
        s = offsets[best[1]]
        e = offsets[best[2]] - 1
        e = min(e, len(new_data))
        new = Span(cur_id, s, e)
        return Reanchor(
            "changed", old.id, new.id, round(best[0], 3), new_data[s:e].decode("utf-8", "replace")
        )
    return Reanchor("missing", old.id, ratio=round(best[0], 3))
