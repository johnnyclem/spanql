"""Recompute a stored cell against the current sources.

Cited spans are re-anchored first (exact context, then moved text, then fuzzy), so
an edit elsewhere in a file does not break the answer. Recomputing never rewrites
the old cell: it produces a *new* evaluation and reports what changed.
"""

from __future__ import annotations

from dataclasses import dataclass, field

from ..errors import SpanqlError
from ..store import Store, reanchor
from .engine import Engine, Evaluation
from .resolve import HandleTable


@dataclass(slots=True)
class Recomputed:
    status: str  # ok | failed
    evaluation: Evaluation | None = None
    error: dict | None = None
    reanchored: list[dict] = field(default_factory=list)
    changed: bool = False  # did any displayed value differ from the stored cell?

    def to_json(self) -> dict:
        return {
            "status": self.status, "changed": self.changed, "error": self.error, "reanchored": self.reanchored,
            "cells": [c.to_json() for c in self.evaluation.cells] if self.evaluation else [],
        }


def recompute(engine: Engine, cell_id: str) -> Recomputed:
    store: Store = engine.store
    stored = store.get_cell(cell_id)
    if stored is None:
        raise KeyError(cell_id)
    spans, notes = {}, []
    for handle, span_id in (stored.get("answer_handles") or stored.get("handles") or {}).items():
        old = store.span_from_id(span_id)
        results = reanchor(store, old)
        best = next((r for r in results if r.status == "unchanged"), None) \
            or next((r for r in results if r.status in ("moved", "edited")), None)
        if best is None or best.span is None:
            why = results[0].note if results else "no source holds this span any more"
            notes.append({"handle": handle, "status": "missing", "note": why})
            return Recomputed("failed", error={"message": f"{handle} can no longer be located: {why}"}, reanchored=notes)
        notes.append({"handle": handle, **best.to_json()})
        spans[int(handle.lstrip("$"))] = best.span
    try:
        ev = engine.evaluate(stored["answer_formula"], HandleTable(spans), question_id=stored.get("question_id"),
                             model=stored.get("model"))
    except SpanqlError as e:
        return Recomputed("failed", error=e.to_json(), reanchored=notes)
    old_cells = {c["index"]: c["display"] for c in store.cells_for_question(stored["question_id"])} \
        if stored.get("question_id") else {stored["index"]: stored["display"]}
    changed = any(old_cells.get(c.index) != c.display for c in ev.cells)
    return Recomputed("ok", ev, reanchored=notes, changed=changed)
