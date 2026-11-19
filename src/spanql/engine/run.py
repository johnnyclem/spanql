"""Evaluate a formula end to end: parse → type-check → static verifier checks → evaluate →
grounding checks → cells (with provenance) → rendered answer."""

from __future__ import annotations

import base64
import hashlib
import json
import re
from collections.abc import Callable
from dataclasses import dataclass, field
from typing import Any

from spanql import ENGINE_VERSION
from spanql.engine.errors import EngineError, Problem
from spanql.engine.evaluator import EvalContext, Evaluator
from spanql.engine.handles import HandleTable
from spanql.engine.typecheck import typecheck
from spanql.engine.values import Value, display
from spanql.formula.ast import (
    Ambiguous,
    Answer,
    ArgRefError,
    Formula,
    NotFound,
    expand_arg_refs,
    unparse,
    walk,
)
from spanql.formula.ast import HandleRef as HRef
from spanql.formula.parser import FormulaSyntaxError, parse
from spanql.store.db import SpanError, Store, now_iso
from spanql.verify.checks import check_literals, check_template, is_count_question

Relevance = Callable[[str, str], bool]  # (question, span text) -> relevant?
_PLACEHOLDER = re.compile(r"\{(\d+)\}")


@dataclass
class Outcome:
    status: str  # grounded | not_found | ambiguous | error
    formula: str
    cells: list[dict[str, Any]] = field(default_factory=list)
    answer: str | None = None
    reason: str | None = None
    handles: list[str] = field(default_factory=list)  # handles cited by NOT_FOUND/AMBIGUOUS
    problems: list[Problem] = field(default_factory=list)
    warnings: list[Problem] = field(default_factory=list)
    queries: list[dict[str, Any]] = field(default_factory=list)

    @property
    def ok(self) -> bool:
        return self.status in ("grounded", "not_found", "ambiguous")

    def to_dict(self) -> dict[str, Any]:
        return {
            "status": self.status,
            "formula": self.formula,
            "answer": self.answer,
            "reason": self.reason,
            "handles": self.handles,
            "cells": self.cells,
            "problems": [p.to_dict() for p in self.problems],
            "warnings": [p.to_dict() for p in self.warnings],
        }

    def feedback(self) -> str:
        """Retry feedback for the model: the specific verifier errors."""
        return "\n".join(f"- {p}" for p in self.problems)


def cell_id(formula: str, handles_used: dict[str, str], inputs: list[dict[str, Any]]) -> str:
    key = json.dumps(
        [
            formula,
            handles_used,
            sorted(i.get("consumed_span") or i.get("id") or i.get("query", "") for i in inputs),
        ]
    )
    digest = hashlib.sha256(key.encode()).digest()
    return "c_" + base64.b32encode(digest).decode().lower().rstrip("=")[:10]


def render(template: str | None, values: list[Value]) -> str:
    if template is None:
        return "; ".join(display(v) for v in values)
    return _PLACEHOLDER.sub(lambda m: display(values[int(m.group(1))]), template)


def evaluate_formula(
    store: Store,
    formula: str | Formula,
    handles: HandleTable,
    *,
    question: str | None = None,
    relevance: Relevance | None = None,
    model_info: dict[str, Any] | None = None,
    question_id: str | None = None,
    save: bool = False,
) -> Outcome:
    text = formula if isinstance(formula, str) else unparse(formula)
    try:
        f = parse(text) if isinstance(formula, str) else formula
    except FormulaSyntaxError as exc:
        return Outcome(
            "error",
            text,
            problems=[
                Problem(
                    "V1",
                    exc.message,
                    "write exactly one formula: ANSWER(...), NOT_FOUND(...) or AMBIGUOUS(...)",
                    exc.pos,
                )
            ],
        )
    canonical = unparse(f)

    # V2 for refusals: cited handles must exist
    if isinstance(f, NotFound | Ambiguous):
        cited = f.searched if isinstance(f, NotFound) else f.handles
        bad = [h for h in cited if h not in handles]
        problems = typecheck(f)
        if bad:
            problems.append(
                Problem("V2", f"handles not in candidate list: {', '.join(f'${h}' for h in bad)}")
            )
        if problems:
            return Outcome("error", canonical, problems=problems)
        status = "not_found" if isinstance(f, NotFound) else "ambiguous"
        return Outcome(
            status, canonical, reason=f.reason, handles=[handles[h].span.id for h in cited]
        )

    assert isinstance(f, Answer)
    try:
        f = expand_arg_refs(f)  # [n] -> the n-th earlier value; each cell stays self-contained
    except ArgRefError as exc:
        return Outcome("error", canonical, problems=[Problem("V1", exc.message, pos=exc.pos)])
    problems = typecheck(f)
    lit_err, warnings = check_literals(f, question)
    problems += lit_err + check_template(f, question)
    if problems:
        return Outcome("error", canonical, problems=problems, warnings=warnings)

    ctx = EvalContext(store, handles, count_question=is_count_question(question))
    ev = Evaluator(ctx)
    values: list[Value] = []
    for expr in f.exprs:
        try:
            values.append(ev.eval(expr))
        except EngineError as err:
            problems.append(err.problem)
        except SpanError as err:
            problems.append(Problem("V2", str(err), pos=expr.pos))
    if problems:
        return Outcome(
            "error", canonical, problems=problems, warnings=warnings, queries=ctx.queries
        )

    for expr, val in zip(f.exprs, values, strict=True):
        if not val.grounded:
            problems.append(
                Problem(
                    "V4",
                    f"{unparse(expr)} doesn't use any source text",
                    "reference a $n quote or a FIND(...) query",
                    expr.pos,
                )
            )
        if val.type == "SpanSet":
            problems.append(Problem("V1", "an answer can't be a bare set of matches", pos=expr.pos))

    if relevance is not None and question and not problems:
        checked: set[str] = set()
        for val in values:
            for inp in val.inputs:
                if inp.get("kind") != "span" or inp["id"] in checked:
                    continue
                checked.add(inp["id"])
                if not relevance(question, inp["text"]):
                    problems.append(
                        Problem(
                            "V6",
                            f"{inp.get('handle') or inp['id']} ({inp['text'][:60]!r}) does not "
                            "appear to be about what the question asks",
                            "pick a candidate that answers the question, or NOT_FOUND",
                        )
                    )
    if problems:
        return Outcome(
            "error", canonical, problems=problems, warnings=warnings, queries=ctx.queries
        )

    cells = []
    when = now_iso()
    for i, (expr, val) in enumerate(zip(f.exprs, values, strict=True)):
        ftxt = unparse(expr)
        used = sorted({n.handle for n in walk(expr) if isinstance(n, HRef)})
        handles_used = {f"${h}": handles[h].span.id for h in used}
        cid = cell_id(ftxt, handles_used, val.inputs)
        cell = {
            "id": cid,
            "index": i,
            "formula": ftxt,
            "handles": handles_used,
            "value": val.to_json(),
            "status": "grounded",
            "inputs": val.inputs,
            "steps": val.steps,
            "evaluated_at": when,
            "engine_version": ENGINE_VERSION,
            "question": question,
            "warnings": [w.to_dict() for w in warnings if _within(w, expr)],
        }
        if model_info:
            cell["model"] = model_info
        cells.append(cell)
        if save:
            store.save_cell(cell, question_id)
    answer = render(f.template, values)
    return Outcome(
        "grounded", canonical, cells=cells, answer=answer, warnings=warnings, queries=ctx.queries
    )


def _within(p: Problem, expr: Any) -> bool:
    return True if p.pos is None else any(getattr(n, "pos", -1) == p.pos for n in walk(expr))
