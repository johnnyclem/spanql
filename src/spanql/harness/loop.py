"""The agent loop (HARNESS.md section 1): a bounded, fixed-toolset pipeline, not an open-ended agent.

retrieve -> prompt -> formula -> verify (V1-V6) -> [retry with the exact error] -> render or refuse.
The model never runs commands, never reads files, and never writes a final value.
"""

from __future__ import annotations

import json
import secrets
import string
import time
from dataclasses import dataclass, field
from datetime import date

from ..engine import Engine, Evaluation, HandleTable
from ..errors import SpanqlError
from ..formula.gbnf import build_gbnf
from ..retrieve import Retrieval, retrieve
from ..store import Store, utc_now
from ..verify import RelevanceChecker, verify_formula
from .client import ClientError, ModelClient, extract_formula
from .prompts import SYSTEM, build_user_prompt, retry_message


class HarnessError(Exception):
    """The question could not be processed at all (e.g. no model configured, endpoint down)."""


@dataclass(slots=True)
class HarnessConfig:
    max_attempts: int = 3
    candidate_token_budget: int = 8192
    relevance_check: bool = True
    k: int = 40
    max_seconds: float = 30.0
    max_tokens: int = 700


@dataclass(slots=True)
class AskResult:
    status: str  # grounded | not_found | ambiguous
    text: str
    run_id: str
    attempts: int
    cells: list[dict] = field(default_factory=list)
    message: str | None = None
    conflicts: list[dict] = field(default_factory=list)
    search_report: dict = field(default_factory=dict)
    errors: list[dict] = field(default_factory=list)  # one verifier failure per failed attempt
    elapsed_ms: int = 0

    def to_json(self) -> dict:
        return {
            "answer": {"text": self.text, "cells": [c["id"] for c in self.cells]}, "status": self.status,
            "attempts": self.attempts, "run_id": self.run_id, "message": self.message, "conflicts": self.conflicts,
            "search_report": self.search_report, "errors": self.errors, "elapsed_ms": self.elapsed_ms,
            "cells": self.cells,
        }


def _rid() -> str:
    return "r_" + "".join(secrets.choice(string.ascii_letters + string.digits) for _ in range(8))


class Harness:
    def __init__(self, store: Store, client: ModelClient | None, config: HarnessConfig | None = None,
                 engine: Engine | None = None):
        self.store = store
        self.client = client
        self.config = config or HarnessConfig()
        self.engine = engine or Engine(store)

    # -------------------------------------------------------------------- ask
    def ask(self, question: str, *, scope: list[str] | None = None, conversation_id: str | None = None,
            today: date | None = None) -> AskResult:
        if not question or not question.strip():
            raise HarnessError("empty question")
        t0 = time.monotonic()
        run_id = _rid()
        today = today or date.today()
        if not self.store.list_sources():
            return self._record(AskResult(
                "not_found", "I couldn't find this in your sources: nothing has been ingested yet. "
                             "Run `spanql ingest <path>` first.", run_id, 0,
                message="no sources ingested"), question, {}, t0, conversation_id, scope)
        if self.client is None:
            raise HarnessError("no model is configured; set [model] endpoint in config.toml (see docs/DEPLOYMENT.md)")

        cfg = self.config
        ret = retrieve(self.store, question, scope=scope, k=cfg.k, token_budget=cfg.candidate_token_budget, today=today)
        user_prompt = build_user_prompt(question, ret, today)
        messages = [{"role": "system", "content": SYSTEM}, {"role": "user", "content": user_prompt}]
        grammar = build_gbnf(len(ret.candidates))
        relevance = RelevanceChecker(self.client, self.store) if cfg.relevance_check else None
        attempts: list[dict] = []
        errors: list[dict] = []
        result: AskResult | None = None

        for n in range(1, cfg.max_attempts + 1):
            if n > 1 and time.monotonic() - t0 > cfg.max_seconds:
                break
            try:
                raw = self.client.complete(messages, grammar=grammar, max_tokens=cfg.max_tokens)
            except ClientError as e:
                raise HarnessError(str(e)) from e
            formula = extract_formula(raw)
            rec: dict = {"attempt": n, "raw": raw, "formula": formula}
            attempts.append(rec)
            try:
                ev = verify_formula(self.engine, formula, ret.handles, question=question, relevance=relevance,
                                    question_id=run_id, model={"id": self.client.model_id, "formula_attempt": n})
            except SpanqlError as e:
                rec["error"] = e.to_json()
                errors.append({"attempt": n, **e.to_json()})
                messages += [{"role": "assistant", "content": raw},
                             {"role": "user", "content": retry_message(e.feedback())}]
                continue
            except ClientError as e:  # relevance check could not reach the model
                raise HarnessError(str(e)) from e
            rec["outcome"] = ev.kind
            result = self._finish(ev, ret, run_id, n, errors)
            break

        if result is None:
            result = self._exhausted(ret, run_id, len(attempts), errors)
        record = {"user_prompt": user_prompt, "attempts": attempts, "model": self.client.model_id,
                  "retrieval": {**ret.report(), "candidates_full": [c.to_json() for c in ret.candidates],
                                "date_hints": ret.date_hints}}
        return self._record(result, question, record, t0, conversation_id, scope)

    # --------------------------------------------------------------- outcomes
    def _finish(self, ev: Evaluation, ret: Retrieval, run_id: str, attempt: int, errors: list[dict]) -> AskResult:
        report = ret.report()
        if ev.kind == "answer":
            cells = [c.to_json() for c in ev.cells]
            self.store.save_cells(cells)
            return AskResult("grounded", ev.render(), run_id, attempt, cells=cells, search_report=report, errors=errors)
        if ev.kind == "ambiguous":
            conflicts = [self._span_info(s) for s in ev.spans]
            lines = [ev.message or "The sources conflict."] + [
                f"- {c['source']} {c['lines']}: \"{c['text']}\"" for c in conflicts]
            return AskResult("ambiguous", "\n".join(lines), run_id, attempt, message=ev.message, conflicts=conflicts,
                             search_report=report, errors=errors)
        return AskResult("not_found", self._refusal_text(ev.message, ret), run_id, attempt, message=ev.message,
                         search_report=report, errors=errors)

    def _exhausted(self, ret: Retrieval, run_id: str, attempts: int, errors: list[dict]) -> AskResult:
        report = ret.report()
        report["attempt_errors"] = [{"attempt": e["attempt"], "check": e["check"], "message": e["message"]}
                                    for e in errors]
        checks = ", ".join(sorted({e["check"] for e in errors})) or "none"
        msg = f"No formula that passes verification could be built from your sources (failed checks: {checks})."
        return AskResult("not_found", self._refusal_text(msg, ret), run_id, attempts, message=msg,
                         search_report=report, errors=errors)

    def _refusal_text(self, message: str | None, ret: Retrieval) -> str:
        terms = ", ".join(f'"{t}"' for t in ret.terms[:8]) or "your question"
        out = [f"I couldn't find this in your sources. I searched {ret.sources_searched} source(s) "
               f"({ret.blocks_searched} blocks) for {terms}."]
        if message:
            out.append(message)
        near = ret.candidates[:3] or ret.near_misses[:3]
        if near:
            out.append("Closest passages:")
            out += [f"- {c.uri} {c.location()}: \"{c.excerpt[:140]}\"" for c in near]
        return "\n".join(out)

    def _span_info(self, span) -> dict:
        loc = self.store.locate(span)
        text = " ".join(self.store.span_text(span).split())
        return {"span": span.id, "source": loc.sources[0] if loc.sources else None,
                "lines": f"L{loc.line_start}" if loc.line_start == loc.line_end else f"L{loc.line_start}-L{loc.line_end}",
                "text": text if len(text) <= 160 else text[:159] + "…"}

    # ---------------------------------------------------------------- records
    def _record(self, result: AskResult, question: str, record: dict, t0: float, conversation_id, scope) -> AskResult:
        result.elapsed_ms = int((time.monotonic() - t0) * 1000)
        record = {**record, "conversation_id": conversation_id, "scope": scope, "text": result.text,
                  "errors": result.errors}
        with self.store.transaction() as db:
            db.execute(
                "INSERT INTO run(id, created_at, question, status, attempts, elapsed_ms, record) VALUES (?,?,?,?,?,?,?)",
                (result.run_id, utc_now(), question, result.status, result.attempts, result.elapsed_ms,
                 json.dumps(record, ensure_ascii=False)))
        return result


def list_runs(store: Store, limit: int = 20) -> list[dict]:
    rows = store.db.execute(
        "SELECT id, created_at, question, status, attempts, elapsed_ms FROM run ORDER BY created_at DESC, rowid DESC "
        "LIMIT ?", (limit,)).fetchall()
    return [dict(r) for r in rows]


def evaluate_formula(store: Store, formula: str, handles: dict[str, str] | None = None, *, question: str | None = None,
                     verify: bool = True, save: bool = True) -> Evaluation:
    """Power-user / test entry (``POST /v1/evaluate``): evaluate a hand-written formula.

    ``verify`` applies the V4 literal policy; V1-V3 and V5 always apply.
    """
    engine = Engine(store)
    table = HandleTable.from_ids(store, handles or {})
    ev = verify_formula(engine, formula, table, question=question, strict_literals=verify)
    if save and ev.cells:
        store.save_cells([c.to_json() for c in ev.cells])
    return ev
