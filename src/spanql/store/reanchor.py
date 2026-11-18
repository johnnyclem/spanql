"""Re-anchoring: find where a span's text went after its source changed.

Cells always resolve against their original snapshot; re-anchoring only *reports*
where the same text now lives. It never rewrites a cell (ARCHITECTURE.md 1.3).
"""

from __future__ import annotations

from dataclasses import dataclass
from difflib import SequenceMatcher

from ..spans import Span
from .db import Store

CONTEXT = 48
FUZZY_MIN_RATIO = 0.8
MAX_FUZZY_WINDOWS = 20_000


@dataclass(slots=True)
class Reanchor:
    status: str  # unchanged | moved | edited | missing
    source: str
    span: Span | None = None  # location in the source's *current* snapshot
    similarity: float = 0.0
    note: str = ""

    def to_json(self) -> dict:
        return {
            "status": self.status,
            "source": self.source,
            "span": self.span.id if self.span else None,
            "similarity": round(self.similarity, 3),
            "note": self.note,
        }


def reanchor(store: Store, span: Span) -> list[Reanchor]:
    """One result per source that ever held ``span``'s snapshot."""
    rows = store.db.execute(
        """SELECT DISTINCT s.uri FROM source_snapshot ss JOIN source s ON s.id=ss.source_id
           WHERE ss.snapshot_id=? ORDER BY s.uri""",
        (span.snapshot_id,),
    ).fetchall()
    return [_reanchor_one(store, span, r["uri"]) for r in rows]


def _reanchor_one(store: Store, span: Span, uri: str) -> Reanchor:
    src = store.get_source(uri)
    if src is None or src.current_snapshot is None:
        return Reanchor("missing", uri, note="source no longer exists")
    if src.current_snapshot == span.snapshot_id:
        return Reanchor("unchanged", uri, span, 1.0, "source has not changed")

    old = store.snapshot_bytes(span.snapshot_id)
    new = store.snapshot_bytes(src.current_snapshot)
    text = old[span.start : span.end]
    if not text:
        return Reanchor("missing", uri, note="empty span cannot be re-anchored")
    before = old[max(0, span.start - CONTEXT) : span.start]
    after = old[span.end : span.end + CONTEXT]

    # 1. exact text with identical surrounding context
    needle = before + text + after
    hits = _find_all(new, needle)
    if hits:
        pos = min(hits, key=lambda p: abs(p + len(before) - span.start)) + len(before)
        return _found(store, src.current_snapshot, pos, len(text), uri, span, 1.0, "text and context unchanged")

    # 2. exact text; choose the occurrence with the most similar context
    hits = _find_all(new, text)
    if hits:
        def ctx_score(p: int) -> float:
            b = new[max(0, p - CONTEXT) : p]
            a = new[p + len(text) : p + len(text) + CONTEXT]
            return SequenceMatcher(None, before, b).ratio() + SequenceMatcher(None, after, a).ratio()

        pos = max(hits, key=ctx_score)
        note = "text unchanged; surrounding context changed" + (
            f" ({len(hits)} occurrences, chose best context match)" if len(hits) > 1 else ""
        )
        return _found(store, src.current_snapshot, pos, len(text), uri, span, 1.0, note)

    # 3. edited in place: the surrounding context is intact, the value between is not
    best = _between_anchors(text, before, after, new)
    if best is not None:
        start, end, ratio = best
        return _found(store, src.current_snapshot, start, end - start, uri, span, ratio, "text was edited")

    # 4. fuzzy: best line window by similarity
    best = _fuzzy(text, new)
    if best is not None:
        start, end, ratio = best
        return _found(store, src.current_snapshot, start, end - start, uri, span, ratio, "text was edited")
    return Reanchor("missing", uri, note="text not found in the current version")


def _found(store, snapshot_id, pos, length, uri, old: Span, similarity, note) -> Reanchor:
    new_span = store.make_span(snapshot_id, pos, pos + length)
    same_text = new_span.text_sha256 == old.text_sha256
    status = "edited" if not same_text else "moved"
    return Reanchor(status, uri, new_span, similarity, note)


def _find_all(hay: bytes, needle: bytes, limit: int = 1000) -> list[int]:
    out, pos = [], hay.find(needle)
    while pos != -1 and len(out) < limit:
        out.append(pos)
        pos = hay.find(needle, pos + 1)
    return out


def _between_anchors(text: bytes, before: bytes, after: bytes, new: bytes) -> tuple[int, int, float] | None:
    """Locate an edited value by the unchanged text on both sides of it."""
    if not before or not after:
        return None
    old = text.decode("utf-8", "replace")
    for k in (32, 16, 8):
        b, a = before[-k:], after[:k]
        if len(b) < min(k, 8) or len(a) < min(k, 8):
            continue
        best = None
        for bp in _find_all(new, b, 50):
            lo = bp + len(b)
            window = new[lo : lo + 2 * len(text) + 64]
            ap = window.find(a)
            if ap == -1:
                continue
            cand = new[lo : lo + ap]
            ratio = SequenceMatcher(None, old, cand.decode("utf-8", "replace"), autojunk=False).ratio()
            if cand and ratio >= 0.5 and (best is None or ratio > best[2]):
                best = (lo, lo + ap, ratio)
        if best:
            return best
    return None


def _fuzzy(text: bytes, new: bytes) -> tuple[int, int, float] | None:
    """Find the line window of ``new`` most similar to ``text`` (spans longer than 8 KB are skipped)."""
    if len(text) > 8192 or len(text) < 8:
        return None
    n_lines = text.count(b"\n") + 1
    lines = new.split(b"\n")
    starts, pos = [], 0
    for ln in lines:
        starts.append(pos)
        pos += len(ln) + 1
    best: tuple[float, int, int] | None = None
    sm = SequenceMatcher(None, autojunk=False)
    sm.set_seq1(text.decode("utf-8", "replace"))
    windows = 0
    for k in sorted({max(1, n_lines - 1), n_lines, n_lines + 1}):
        for i in range(0, max(0, len(lines) - k + 1)):
            windows += 1
            if windows > MAX_FUZZY_WINDOWS:
                break
            w_start = starts[i]
            w_end = starts[i + k - 1] + len(lines[i + k - 1])
            window = new[w_start:w_end].decode("utf-8", "replace")
            sm.set_seq2(window)
            if sm.real_quick_ratio() < FUZZY_MIN_RATIO or sm.quick_ratio() < FUZZY_MIN_RATIO:
                continue
            r = sm.ratio()
            if r >= FUZZY_MIN_RATIO and (best is None or r > best[0]):
                # tighten to the matched region inside the window
                blocks = [b for b in sm.get_matching_blocks() if b.size > 0]
                lo, hi = blocks[0].b, blocks[-1].b + blocks[-1].size
                prefix = len(window[:lo].encode("utf-8"))
                matched = len(window[lo:hi].encode("utf-8"))
                best = (r, w_start + prefix, w_start + prefix + matched)
    if best is None:
        return None
    return best[1], best[2], best[0]
