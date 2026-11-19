"""The bounded agent loop (docs/HARNESS.md §1)."""

from __future__ import annotations

import time
import uuid
from collections.abc import Callable
from dataclasses import dataclass, field
from typing import Any

from spanql.config import HarnessConfig
from spanql.engine.run import Outcome, evaluate_formula
from spanql.formula.gbnf import build_gbnf
from spanql.harness.backends import Backend, BackendError
from spanql.harness.prompts import build_messages
from spanql.retrieve.retriever import Retrieval, Retriever
from spanql.store.db import Store

Event = Callable[[str, dict[str, Any]], None]


@dataclass
class Attempt:
    n: int
    formula: str
    status: str
    problems: list[dict[str, Any]]
    seconds: float
    raw: str | None = None

    def to_dict(self) -> dict[str, Any]:
        return {
            "n": self.n,
            "formula": self.formula,
            "status": self.status,
            "problems": self.problems,
            "seconds": round(self.seconds, 3),
        }


@dataclass
class AskResult:
    run_id: str
    question: str
    status: str  # grounded | not_found | ambiguous | error (the backend failed; no answer)
    answer: str
    cells: list[dict[str, Any]] = field(default_factory=list)
    formula: str | None = None
    attempts: list[Attempt] = field(default_factory=list)
    search_report: dict[str, Any] = field(default_factory=dict)
    candidates: list[dict[str, Any]] = field(default_factory=list)
    reason: str | None = None
    refusal_by: str | None = None  # model | harness
    conflicting: list[str] = field(default_factory=list)
    model: dict[str, Any] = field(default_factory=dict)
    seconds: float = 0.0
    error: str | None = None

    def to_dict(self) -> dict[str, Any]:
        return {
            "run_id": self.run_id,
            "question": self.question,
            "status": self.status,
            "answer": {"text": self.answer, "cells": [c["id"] for c in self.cells]},
            "cells": self.cells,
            "formula": self.formula,
            "attempts": len(self.attempts),
            "attempt_log": [a.to_dict() for a in self.attempts],
            "search_report": self.search_report,
            "candidates": self.candidates,
            "reason": self.reason,
            "refusal_by": self.refusal_by,
            "conflicting": self.conflicting,
            "model": self.model,
            "seconds": round(self.seconds, 3),
            "error": self.error,
        }


class Harness:
    def __init__(self, store: Store, backend: Backend, cfg: HarnessConfig | None = None):
        self.store = store
        self.backend = backend
        self.cfg = cfg or HarnessConfig()
        self.retriever = Retriever(store, self.cfg.max_candidates, self.cfg.candidate_token_budget)

    def ask(
        self,
        question: str,
        *,
        scope: list[str] | None = None,
        history: list[dict[str, str]] | None = None,
        prior_cells: list[dict[str, Any]] | None = None,
        on_event: Event | None = None,
    ) -> AskResult:
        t0 = time.monotonic()
        run_id = "r_" + uuid.uuid4().hex[:12]
        emit = on_event or (lambda kind, data: None)
        model_info = {"backend": self.backend.name, "id": self.backend.model}

        retrieval = self.retriever.search(question, scope=scope)
        emit("retrieval", retrieval.to_dict())
        attempts: list[Attempt] = []
        outcome: Outcome | None = None
        feedback: str | None = None
        last_formula: str | None = None
        k = self.cfg.max_candidates
        backend_error: str | None = None

        for n in range(1, max(1, self.cfg.max_attempts) + 1):
            if time.monotonic() - t0 > self.cfg.wall_clock_s and attempts:
                break
            grammar = build_gbnf(list(retrieval.handles)) if self.backend.supports_grammar else None
            messages = build_messages(
                question,
                retrieval.handles,
                retrieval.hints,
                history=history,
                feedback=feedback,
                last_formula=last_formula,
                prior_cells=prior_cells,
            )
            ta = time.monotonic()
            try:
                # deterministic first try; a little more exploration on retries so a repeated
                # rejected formula isn't regenerated verbatim
                temp = None if n == 1 else min(0.3 * (n - 1), 0.8)
                gen = self.backend.generate(
                    messages,
                    grammar=grammar,
                    question=question,
                    handles=retrieval.handles,
                    hints=retrieval.hints,
                    temperature=temp,
                )
            except BackendError as exc:
                backend_error = str(exc)
                emit("error", {"message": backend_error})
                break
            relevance = self.backend.relevant if self.cfg.relevance_check else None
            outcome = evaluate_formula(
                self.store,
                gen.text,
                retrieval.handles,
                question=question,
                relevance=relevance,
                model_info={**model_info, "formula_attempt": n},
                question_id=run_id,
            )
            att = Attempt(
                n,
                gen.text,
                outcome.status,
                [p.to_dict() for p in outcome.problems],
                time.monotonic() - ta,
                raw=str(gen.raw) if gen.raw is not None else None,
            )
            attempts.append(att)
            emit("attempt", att.to_dict())
            if outcome.ok:
                break
            emit("verifier", {"attempt": n, "problems": att.problems})
            feedback, last_formula = outcome.feedback(), gen.text
            # widen retrieval between attempts when the problem might be missing context
            if any(p.code in ("V2", "V5", "V6") for p in outcome.problems):
                k = min(k * 2, 40)
                retrieval = self._widen(question, scope, k, retrieval)

        result = self._finish(
            run_id, question, retrieval, attempts, outcome, model_info, backend_error
        )
        result.seconds = time.monotonic() - t0
        if result.status == "grounded":
            for cell in result.cells:
                self.store.save_cell(cell, run_id)
                emit("cell", cell)
        emit("answer", result.to_dict())
        record = result.to_dict()
        record["attempt_log"] = [dict(a.to_dict(), raw=a.raw) for a in attempts]
        self.store.save_run(run_id, question, result.status, record)
        return result

    def _widen(self, question: str, scope: list[str] | None, k: int, prev: Retrieval) -> Retrieval:
        wider = self.retriever.search(question, scope=scope, k=k)
        # keep existing handle numbers stable; append new candidates after them
        merged = dict(prev.handles)
        known = {h.span for h in merged.values()}
        for h in wider.handles.values():
            if h.span not in known:
                h.n = len(merged) + 1
                merged[h.n] = h
        wider.handles = merged
        return wider

    def _finish(
        self,
        run_id: str,
        question: str,
        retrieval: Retrieval,
        attempts: list[Attempt],
        outcome: Outcome | None,
        model_info: dict[str, Any],
        backend_error: str | None,
    ) -> AskResult:
        report = dict(retrieval.report)
        report["attempts"] = len(attempts)
        cands = [h.to_dict() for h in retrieval.handles.values()]
        base: dict[str, Any] = {
            "run_id": run_id,
            "question": question,
            "attempts": attempts,
            "search_report": report,
            "candidates": cands,
            "model": model_info,
        }
        if outcome is not None and outcome.status == "grounded":
            return AskResult(
                status="grounded",
                answer=outcome.answer or "",
                cells=outcome.cells,
                formula=outcome.formula,
                **base,
            )
        if outcome is not None and outcome.status == "ambiguous":
            text = (
                f"Your sources disagree: {outcome.reason} "
                f"See {len(outcome.handles)} conflicting passages."
            )
            return AskResult(
                status="ambiguous",
                answer=text,
                formula=outcome.formula,
                reason=outcome.reason,
                refusal_by="model",
                conflicting=outcome.handles,
                **base,
            )
        if outcome is not None and outcome.status == "not_found":
            reason = outcome.reason or ""
            return AskResult(
                status="not_found",
                answer=self._refusal_text(reason, report),
                formula=outcome.formula,
                reason=reason,
                refusal_by="model",
                **base,
            )
        if backend_error:
            # an unreachable or broken model is an operational error, not "the answer isn't in
            # your sources": reporting it as a refusal would make an outage look like a fact
            return AskResult(
                status="error",
                answer=f"No answer: the model backend failed ({backend_error}). "
                "This says nothing about your sources; check the model endpoint and retry.",
                formula=attempts[-1].formula if attempts else None,
                reason=f"model backend failed: {backend_error}",
                error=backend_error,
                **base,
            )
        if attempts:
            last = attempts[-1].problems
            reason = (
                f"no formula passed verification after {len(attempts)} attempt(s); last "
                f"error: {last[0]['message'] if last else 'unknown'}"
            )
        else:
            reason = "no attempts were made"
        report["last_problems"] = attempts[-1].problems if attempts else []
        return AskResult(
            status="not_found",
            answer=self._refusal_text(reason, report),
            formula=attempts[-1].formula if attempts else None,
            reason=reason,
            refusal_by="harness",
            **base,
        )

    @staticmethod
    def _refusal_text(reason: str, report: dict[str, Any]) -> str:
        kw = ", ".join(f'"{w}"' for w in report.get("keywords", [])[:6]) or "(no keywords)"
        return (
            f"I couldn't find this in your sources. I searched "
            f"{report.get('blocks_searched', 0)} passages in "
            f"{report.get('sources_searched', 0)} sources for {kw}. {reason}".strip()
        )
