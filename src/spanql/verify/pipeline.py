"""Run all verifier checks, in order, over one formula."""

from __future__ import annotations

from ..engine.engine import Engine, Evaluation
from ..engine.resolve import HandleTable
from .literals import check_literals
from .relevance import RelevanceChecker


def verify_formula(
    engine: Engine,
    text: str,
    handles: HandleTable,
    *,
    question: str | None = None,
    relevance: RelevanceChecker | None = None,
    strict_literals: bool = True,
    pins: dict[str, str] | None = None,
    question_id: str | None = None,
    model: dict | None = None,
) -> Evaluation:
    """V1 parse+type -> V4 literals -> V2 references -> V3 extraction -> V5 support -> V6 relevance.

    Raises a :class:`~spanql.errors.SpanqlError` (its ``check`` says which V-check failed).
    Cheap static checks run before any query touches the store.
    """
    formula, _ = engine.check(text)  # V1
    if strict_literals:
        check_literals(formula, text, question)  # V4
    ev = engine.evaluate(text, handles, formula=formula, pins=pins, question_id=question_id, model=model)  # V2/V3/V5
    if relevance is not None and question and ev.kind == "answer":
        relevance.check(question, ev, handles)  # V6
    return ev
