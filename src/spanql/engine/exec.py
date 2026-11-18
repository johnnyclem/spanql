"""Tree-walking evaluator with provenance.

Every node evaluates to a value plus the *steps* and *inputs* that produced it,
so a cell can show its derivation like Excel's Evaluate Formula and highlight the
exact source characters each extractor consumed. Textually identical
sub-expressions are evaluated once (common-subexpression sharing).
"""

from __future__ import annotations

import json
from dataclasses import dataclass
from decimal import Decimal
from typing import Iterable

from ..errors import EmptySupportError, EvalError, ExtractError
from ..formula.ast import BinOp, BoolLit, Call, CellRef, Compare, Neg, Node, Num, Ref, Str
from ..formula.functions import AS_TYPES, QUERY_FUNCTIONS
from ..spans import Span
from ..store import Store
from . import ops
from .extract import extract
from .format import from_json, render
from .query import DEFAULT_ITEMS, MAX_MATERIALIZE, QUERY_TIMEOUT_S, ScanResult, ScanSpec, compile_scan, run_scan
from .values import (BoolV, DateTimeV, DateV, DurationV, Item, Number, SpanSetV, SpanV, Text, Value)

EXTRACTORS = {"NUM": "num", "MONEY": "money", "PCT_OF": "pct", "DATE": "date", "DATETIME": "datetime",
              "DURATION": "duration", "TEXT": "text", "BOOL": "bool"}
MAX_TRACE_SPANS = 50


@dataclass(slots=True)
class R:
    """A value with the steps and inputs that produced it."""

    value: Value
    steps: tuple[dict, ...] = ()
    inputs: tuple[dict, ...] = ()


def _input_key(inp: dict) -> str:
    kind = inp["kind"]
    if kind == "span":
        return "span:" + inp["id"]
    if kind == "cell":
        return "cell:" + inp["id"]
    return "query:" + json.dumps(inp["query"], sort_keys=True) + json.dumps(inp.get("snapshots", {}), sort_keys=True)


def merge_inputs(groups: Iterable[Iterable[dict]]) -> tuple[dict, ...]:
    out: dict[str, dict] = {}
    for g in groups:
        for inp in g:
            k = _input_key(inp)
            cur = out.get(k)
            if cur is None:
                out[k] = inp
            elif inp["kind"] == "span" and inp.get("uses"):
                seen = {(u["op"], u["consumed"]) for u in cur.get("uses", [])}
                uses = list(cur.get("uses", [])) + [u for u in inp["uses"] if (u["op"], u["consumed"]) not in seen]
                out[k] = dict(cur, uses=uses, consumed=uses[0]["consumed"], parsed=uses[0]["parsed"],
                              consumed_range=uses[0]["range"])
    return tuple(out.values())


def merge_steps(groups: Iterable[Iterable[dict]]) -> tuple[dict, ...]:
    out: dict[tuple, dict] = {}
    for g in groups:
        for s in g:
            out.setdefault((s["op"], s["expr"], s["out"]), s)
    return tuple(out.values())


def _clip(s: str, n: int) -> str:
    s = " ".join(s.split())
    return s if len(s) <= n else s[: n - 1] + "…"


class Executor:
    def __init__(
        self,
        store: Store,
        src: str,
        resolved: dict[int, Span],
        *,
        pins: dict[str, str] | None = None,
        prior: list[tuple[str, Value]] | None = None,
        cell_loader=None,
        timeout: float | None = QUERY_TIMEOUT_S,
    ):
        self.store, self.src, self.resolved = store, src, resolved
        # Pin the snapshot set once so every scan in a formula sees the same world.
        self.pins = pins if pins is not None else {s.uri: s.current_snapshot for s in store.list_sources()}
        self.prior = prior if prior is not None else []
        self.cell_loader = cell_loader
        self.timeout = timeout
        self.memo: dict[str, R] = {}
        self.snapshots_used: dict[str, str] = {}
        self._span_inputs: dict[str, dict] = {}

    # ------------------------------------------------------------- helpers
    def text_of(self, node: Node) -> str:
        return self.src[node.pos : node.end]

    def span_input(self, span: Span) -> dict:
        cached = self._span_inputs.get(span.id)
        if cached is not None:
            return cached
        loc = self.store.locate(span)
        inp = {
            "kind": "span", "id": span.id, "text": _clip(self.store.span_text(span), 240),
            "source": loc.sources[0] if loc.sources else None, "snapshot": span.snapshot_id,
            "text_sha256": span.text_sha256, "line": loc.line_start, "line_end": loc.line_end,
            "page": loc.page, "path": loc.path,
        }
        self._span_inputs[span.id] = inp
        return inp

    def result(self, node: Node, value: Value, parts: list[R], op: str | None = None, **extra) -> R:
        step = {"op": op or getattr(node, "name", "?"), "expr": self.text_of(node), "out": render(value, self.store)}
        step.update(extra)
        return R(value, merge_steps([p.steps for p in parts] + [(step,)]), merge_inputs([p.inputs for p in parts]))

    def kw_str(self, node: Call, name: str) -> str | None:
        for k, v in node.kwargs:
            if k == name and isinstance(v, Str):
                return v.value
        return None

    def kw_int(self, node: Call, name: str) -> int | None:
        for k, v in node.kwargs:
            if k == name and isinstance(v, Num):
                if v.value != v.value.to_integral_value():
                    raise EvalError(f"`{name}:` must be a whole number")
                return int(v.value)
        return None

    # ---------------------------------------------------------- evaluation
    def eval(self, node: Node) -> R:
        key = self.text_of(node)
        hit = self.memo.get(key)
        if hit is not None:
            return hit
        r = self._eval(node)
        self.memo[key] = r
        return r

    def _eval(self, node: Node) -> R:
        if isinstance(node, Num):
            return R(Number(node.value))
        if isinstance(node, Str):
            return R(Text(node.value))
        if isinstance(node, BoolLit):
            return R(BoolV(node.value))
        if isinstance(node, Ref):
            span = self.resolved[id(node)]
            return R(SpanV(span), (), (self.span_input(span),))
        if isinstance(node, CellRef):
            return self._cellref(node)
        if isinstance(node, Neg):
            a = self.eval(node.operand)
            return self.result(node, ops.negate(a.value), [a], op="-")
        if isinstance(node, BinOp):
            a, b = self.eval(node.left), self.eval(node.right)
            return self.result(node, ops.binop(node.op, self._plain(a.value), self._plain(b.value)), [a, b], op=node.op)
        if isinstance(node, Compare):
            a, b = self.eval(node.left), self.eval(node.right)
            v = ops.compare(node.op, self._plain(a.value), self._plain(b.value))
            return self.result(node, v, [a, b], op=node.op)
        if isinstance(node, Call):
            return self.call(node)
        raise EvalError(f"cannot evaluate {type(node).__name__}")

    def _plain(self, v: Value) -> Value:
        """Spans read as their text in comparisons (never as numbers or dates)."""
        if isinstance(v, SpanV):
            return Text(self.store.span_text(v.span))
        if isinstance(v, SpanSetV):
            span = self._one(v, "comparison")
            return Text(self.store.span_text(span))
        return v

    def _one(self, v: SpanSetV, what: str) -> Span:
        if v.total == 0:
            raise EmptySupportError(f"{what} got an empty result: the query matched nothing")
        if v.total > 1:
            raise ExtractError(
                f"{what} needs exactly one span but the query matched {v.total}",
                hint="add more filters (FIELD(...) conditions, day:, src:) until it matches exactly one")
        return v.items[0].span

    def _cellref(self, node: CellRef) -> R:
        if isinstance(node.key, int):
            cid, value = self.prior[node.key]
            step = {"op": "ref", "expr": self.text_of(node), "out": render(value, self.store)}
            return R(value, (step,), ({"kind": "cell", "id": cid, "index": node.key},))
        if self.cell_loader is None:
            raise EvalError(f"unknown cell [{node.key}]")
        stored = self.cell_loader(node.key)
        if stored is None:
            raise EvalError(f"unknown cell [{node.key}]")
        value = from_json(stored["value"])
        step = {"op": "ref", "expr": self.text_of(node), "out": render(value, self.store)}
        return R(value, (step,), ({"kind": "cell", "id": node.key},))

    # --------------------------------------------------------------- calls
    def call(self, node: Call) -> R:
        n = node.name
        if n in EXTRACTORS:
            return self._extract(node)
        if n in QUERY_FUNCTIONS:
            return self._query(node)
        if n == "IF":
            cond = self.eval(node.args[0])
            if not isinstance(cond.value, BoolV):
                raise EvalError("IF needs a true/false condition")
            chosen = self.eval(node.args[1] if cond.value.value else node.args[2])
            return self.result(node, chosen.value, [cond, chosen])
        if n == "COUNT":
            return self._count(node)
        if n == "DISTINCT":
            return self._distinct(node)
        if n in ("SUM", "AVG", "MIN", "MAX"):
            return self._aggregate(node)
        parts = [self.eval(a) for a in node.args]
        vals = [self._plain(p.value) if n in ("EQ", "NE", "GT", "GTE", "LT", "LTE") else p.value for p in parts]
        places = self.kw_int(node, "places")
        if n == "ROUND":
            v = ops.round_(self._num(vals[0], n), places)
        elif n == "PCT":
            v = ops.pct(self._num(vals[0], n), self._num(vals[1], n), places)
        elif n == "RATIO":
            v = ops.ratio(self._num(vals[0], n), self._num(vals[1], n), places)
        elif n == "DIFF":
            v = ops.binop("-", vals[0], vals[1])
        elif n == "ABS":
            v = ops.absolute(vals[0])
        elif n == "DAYS_BETWEEN":
            v = ops.days_between(vals[0], vals[1])
        elif n in ("BEFORE", "AFTER"):
            v = ops.before_after(n, vals[0], vals[1])
        elif n in ("AND", "OR"):
            bools = [self._bool(x, n) for x in vals]
            v = BoolV(all(bools) if n == "AND" else any(bools))
        elif n == "NOT":
            v = BoolV(not self._bool(vals[0], n))
        elif n in ("EQ", "NE", "GT", "GTE", "LT", "LTE"):
            v = ops.compare({"EQ": "==", "NE": "!=", "GT": ">", "GTE": ">=", "LT": "<", "LTE": "<="}[n], vals[0], vals[1])
        elif n == "CONTAINS":
            v = self._contains(vals[0], vals[1])
        else:
            raise EvalError(f"function {n} is not implemented")
        return self.result(node, v, parts)

    @staticmethod
    def _num(v: Value, fn: str) -> Number:
        if not isinstance(v, Number):
            raise EvalError(f"{fn} needs a number, got {v.type}")
        return v

    @staticmethod
    def _bool(v: Value, fn: str) -> bool:
        if not isinstance(v, BoolV):
            raise EvalError(f"{fn} needs true/false values, got {v.type}")
        return v.value

    def _contains(self, hay: Value, needle: Value) -> BoolV:
        if not isinstance(needle, Text):
            raise EvalError("CONTAINS needs a text value to look for")
        if isinstance(hay, Text):
            return BoolV(needle.value in hay.value)
        if isinstance(hay, SpanV):
            return BoolV(needle.value in self.store.span_text(hay.span))
        if isinstance(hay, SpanSetV):
            return BoolV(any(needle.value in self.store.span_text(i.span) for i in self._full_items(hay)))
        raise EvalError(f"CONTAINS cannot search a {hay.type}")

    # ------------------------------------------------------------ extraction
    def _extract(self, node: Call) -> R:
        arg = self.eval(node.args[0])
        extra_inputs: tuple[dict, ...] = ()
        v = arg.value
        if isinstance(v, SpanV):
            span = v.span
        elif isinstance(v, SpanSetV):
            span = self._one(v, node.name)
            extra_inputs = (self.span_input(span),)
        else:
            raise EvalError(f"{node.name} needs a source span, got {v.type}")
        text = self.store.span_text(span)
        try:
            p = extract(EXTRACTORS[node.name], text, locale=self.kw_str(node, "locale"), fmt=self.kw_str(node, "fmt"),
                        year=self.kw_int(node, "year"))
        except ExtractError as e:
            raise ExtractError(f"{e.message} (in {span.id})", hint=e.hint, pos=node.pos) from e
        c0, c1 = p.consumed
        consumed = text[c0:c1]
        use = {"op": node.name, "consumed": consumed, "range": [len(text[:c0].encode()), len(text[:c1].encode())],
               "parsed": p.info}
        inp = dict(self.span_input(span), uses=[use], consumed=consumed, consumed_range=use["range"], parsed=p.info)
        parts = [R(arg.value, arg.steps, merge_inputs([arg.inputs, extra_inputs]))]
        r = self.result(node, p.value, parts, **{"in": _clip(consumed, 80)})
        return R(r.value, r.steps, merge_inputs([r.inputs, (inp,)]))

    # ----------------------------------------------------------------- query
    def _query_input(self, spec: ScanSpec, res: ScanResult) -> dict:
        return {"kind": "query", "op": spec.op, "query": spec.describe(), "snapshots": res.snapshots,
                "count": res.total, "matched_sources": res.matched_sources, "blocks_searched": res.blocks_searched,
                "sample": res.sample, "truncated": res.total > len(res.sample)}

    def _scan(self, node: Call, limit: int | None) -> tuple[ScanSpec, ScanResult]:
        spec = compile_scan(node, self.src)
        res = run_scan(self.store, spec, pins=self.pins, limit=limit, timeout=self.timeout)
        self.snapshots_used.update(res.snapshots)
        return spec, res

    def _query(self, node: Call) -> R:
        spec, res = self._scan(node, DEFAULT_ITEMS)
        inp = self._query_input(spec, res)
        value = SpanSetV(tuple(res.items), res.total, inp, spec)
        step = {"op": node.name, "expr": self.text_of(node), "out": f"{res.total} match{'' if res.total == 1 else 'es'}"}
        return R(value, (step,), (inp,))

    def _count(self, node: Call) -> R:
        a = node.args[0]
        if isinstance(a, Call) and a.name in QUERY_FUNCTIONS:  # COUNT(FIND(...)) is one SQL statement
            spec, res = self._scan(a, 0)
            inp = self._query_input(spec, res)
            return self.result(node, Number(Decimal(res.total)), [R(Number(Decimal(res.total)), (), (inp,))])
        arg = self.eval(a)
        return self.result(node, Number(Decimal(arg.value.total)), [arg])

    def _full_items(self, sv: SpanSetV) -> tuple[Item, ...]:
        if sv.complete:
            return sv.items
        if sv.total > MAX_MATERIALIZE:
            raise EvalError(f"{sv.total} matches is too many to aggregate (limit {MAX_MATERIALIZE})",
                            hint="narrow the query with src:, day:, or a filter")
        spec = sv.spec
        assert isinstance(spec, ScanSpec)
        res = run_scan(self.store, spec, pins=self.pins, limit=None, timeout=self.timeout)
        return tuple(res.items)

    def _distinct(self, node: Call) -> R:
        arg = self.eval(node.args[0])
        sv = arg.value
        if not isinstance(sv, SpanSetV):
            raise EvalError("DISTINCT needs a FIND/COLUMN/SYMBOLS result")
        seen: dict[str, Item] = {}
        for it in self._full_items(sv):
            seen.setdefault(it.label or self.store.span_text(it.span).strip(), it)
        items = tuple(seen.values())
        return self.result(node, SpanSetV(items, len(items), sv.scan, None), [arg])

    def _aggregate(self, node: Call) -> R:
        name = node.name
        parts = [self.eval(a) for a in node.args]
        extra: list[dict] = []
        if len(parts) == 1 and isinstance(parts[0].value, SpanSetV):
            sv = parts[0].value
            items = self._full_items(sv)
            if not items:
                raise EmptySupportError(f"{name} over an empty result: the query matched nothing",
                                        hint="check src:, filters and spelling; zero rows cannot be summed or averaged")
            as_kind = (self.kw_str(node, "as") or "num").lower()
            if as_kind not in AS_TYPES:
                raise EvalError(f"`as:` must be one of {', '.join(AS_TYPES)}")
            values: list[Value] = []
            for i, it in enumerate(items):
                text = self.store.span_text(it.span)
                try:
                    p = extract(as_kind, text, locale=self.kw_str(node, "locale"))
                except ExtractError as e:
                    where = f"{it.source}" + (f" line {it.line}" if it.line else "")
                    raise ExtractError(f"{name}: value {i + 1} of {len(items)} ({where}) is not a clean {as_kind}: "
                                       f"{e.message}", hint="clean the source column or filter such rows out "
                                       "with FIND/COLUMN conditions", pos=node.pos) from e
                values.append(p.value)
                if i < MAX_TRACE_SPANS:
                    use = {"op": as_kind.upper(), "consumed": text[p.consumed[0]:p.consumed[1]],
                           "range": [len(text[:p.consumed[0]].encode()), len(text[:p.consumed[1]].encode())],
                           "parsed": p.info}
                    extra.append(dict(self.span_input(it.span), uses=[use], consumed=use["consumed"],
                                      consumed_range=use["range"], parsed=p.info))
        else:
            values = [p.value for p in parts]
        v = self._fold(name, values)
        r = self.result(node, v, parts, **({"n": len(values)} if len(values) > 1 else {}))
        return R(r.value, r.steps, merge_inputs([r.inputs, extra]))

    def _fold(self, name: str, values: list[Value]) -> Value:
        first = values[0]
        if isinstance(first, Number):
            if any(not isinstance(x, Number) or x.unit != first.unit for x in values):
                units = sorted({(x.unit or "no unit") for x in values if isinstance(x, Number)})
                raise EvalError(f"{name} needs values with the same unit, got {', '.join(units)}")
            nums = [x.value for x in values]  # type: ignore[union-attr]
            if name in ("SUM", "AVG"):
                total = Decimal(0)
                for x in nums:
                    total = ops.CTX.add(total, x)
                return Number(total if name == "SUM" else ops.CTX.divide(total, Decimal(len(nums))), first.unit)
            return Number(min(nums) if name == "MIN" else max(nums), first.unit)
        if isinstance(first, DurationV):
            tds = [x.value for x in values]  # type: ignore[union-attr]
            if name == "SUM":
                return DurationV(sum(tds, type(tds[0])()))
            if name == "AVG":
                return DurationV(sum(tds, type(tds[0])()) / len(tds))
            return DurationV(min(tds) if name == "MIN" else max(tds))
        if isinstance(first, (DateV, DateTimeV)) and name in ("MIN", "MAX"):
            vals = [x.value for x in values]  # type: ignore[union-attr]
            if isinstance(first, DateTimeV) and len({v.tzinfo is None for v in vals}) > 1:
                raise EvalError("cannot compare timestamps with and without a time zone")
            return type(first)(min(vals) if name == "MIN" else max(vals))
        raise EvalError(f"{name} cannot combine {first.type} values")
