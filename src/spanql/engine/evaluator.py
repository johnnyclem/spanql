"""Deterministic evaluation of formula expressions into typed values with provenance."""

from __future__ import annotations

import difflib
from collections.abc import Callable
from dataclasses import dataclass, field
from datetime import date, datetime
from decimal import Decimal, DivisionByZero, InvalidOperation
from typing import Any

from spanql.engine import extract as ex
from spanql.engine import query as q
from spanql.engine.errors import EngineError, citation, extraction, support, syntax
from spanql.engine.handles import HandleTable
from spanql.engine.values import Value, display, from_json, merge_inputs, merge_steps, quantize
from spanql.formula.ast import (
    BinOp,
    BoolLit,
    Call,
    CellRef,
    Compare,
    HandleRef,
    Neg,
    Node,
    NumberLit,
    StringLit,
    unparse,
)
from spanql.store.db import Span, Store

TEXTLIKE = ("Text", "Span")


@dataclass
class EvalContext:
    store: Store
    handles: HandleTable
    count_question: bool = True  # V5: COUNT over zero matches is an answer only for counts
    cell_lookup: Callable[[str], dict[str, Any] | None] | None = None
    queries: list[dict[str, Any]] = field(default_factory=list)


def _span_input(
    ctx: EvalContext, span: Span, *, handle: int | None = None, quote: str | None = None
) -> dict[str, Any]:
    rs = ctx.store.resolve(span)
    return {
        "kind": "span",
        "id": rs.id,
        "text": rs.text,
        "source": rs.source_uri,
        "line": rs.line_start,
        "line_end": rs.line_end,
        "page": rs.page,
        "snapshot": rs.span.snapshot_id,
        "text_sha256": rs.text_sha256,
        "current": rs.is_current,
        "handle": f"${handle}" if handle else None,
        "quote": quote,
    }


def _closest(text: str, quote: str) -> str | None:
    """Closest substring of ``text`` to ``quote`` (for retry hints)."""
    n = len(quote)
    if not text or n == 0:
        return None
    words = text.split()
    best, best_r = None, 0.0
    qw = max(1, len(quote.split()))
    for size in {qw - 1, qw, qw + 1}:
        if size < 1:
            continue
        for i in range(0, max(1, len(words) - size + 1)):
            cand = " ".join(words[i : i + size]).strip(",.;:!?()[]\"'")
            r = difflib.SequenceMatcher(None, quote, cand, autojunk=False).ratio()
            if r > best_r:
                best, best_r = cand, r
    return best if best_r >= 0.5 else None


class Evaluator:
    def __init__(self, ctx: EvalContext):
        self.ctx = ctx

    # ------------------------------------------------------------------ entry
    def eval(self, node: Node) -> Value:
        try:
            return self._eval(node)
        except EngineError as err:
            if err.problem.pos is None:
                err.problem.pos = node.pos
            raise
        except (DivisionByZero, InvalidOperation, ZeroDivisionError) as exc:
            raise extraction(f"arithmetic error: {exc.__class__.__name__}", pos=node.pos) from exc

    def _eval(self, node: Node) -> Value:
        match node:
            case NumberLit(value=v):
                return Value("Number", v, meta={"literal": True})
            case StringLit(value=v):
                return Value("Text", v, meta={"literal": True})
            case BoolLit(value=v):
                return Value("Bool", v, meta={"literal": True})
            case HandleRef():
                return self._handle(node)
            case CellRef(cell_id=cid):
                return self._cell(cid, node.pos)
            case Neg(operand=o):
                val = self._number(self.eval(o), "-")
                return self._derive("NEG", Value("Number", -val.v, val.unit), val)
            case BinOp(op=op, left=left, right=right):
                return self._arith(op, self.eval(left), self.eval(right))
            case Compare(op=op, left=left, right=right):
                return self._compare(op, self.eval(left), self.eval(right))
            case Call():
                return self._call(node)
        raise syntax(f"unsupported expression {unparse(node)}", pos=node.pos)

    # --------------------------------------------------------------- helpers
    def _derive(self, op: str, out: Value, *srcs: Value, **step: Any) -> Value:
        out.inputs = merge_inputs(*srcs)
        out.steps = [*merge_steps(*srcs), {"op": op, "out": display(out), **step}]
        return out

    def _number(self, val: Value, op: str) -> Value:
        if val.type == "Number":
            return val
        if val.type in TEXTLIKE:
            return self._extract("NUM", val)
        raise syntax(
            f"{op} expects a number, got {val.type}",
            hint="wrap quotes in NUM(...), MONEY(...) or PCT_OF(...)",
        )

    def _handle(self, node: HandleRef) -> Value:
        h = self.ctx.handles.get(node.handle)
        if h is None:
            valid = ", ".join(f"${k}" for k in sorted(self.ctx.handles)) or "none"
            raise citation(
                f"handle ${node.handle} is not in the candidate list",
                hint=f"valid handles: {valid}",
                pos=node.pos,
            )
        rs = self.ctx.store.resolve(h.span, expected_sha256=h.text_sha256)
        if node.quote is None:
            inp = _span_input(self.ctx, rs.span, handle=h.n)
            return Value(
                "Span",
                {"id": rs.id, "text": rs.text},
                inputs=[inp],
                steps=[{"op": f"${h.n}", "out": rs.id}],
            )
        data = rs.text.encode("utf-8")
        needle = node.quote.encode("utf-8")
        hits: list[int] = []
        i = data.find(needle)
        while i != -1:
            hits.append(i)
            i = data.find(needle, i + 1)
        if not hits:
            near = _closest(rs.text, node.quote)
            hint = (
                f"closest text in ${h.n}: {near!r}" if near else "quote text exactly as it appears"
            )
            raise citation(f"quote {node.quote!r} not found in ${h.n}", hint=hint, pos=node.pos)
        occ = node.occurrence
        if occ is None and len(hits) > 1:
            raise citation(
                f"quote {node.quote!r} occurs {len(hits)} times in ${h.n}",
                hint=f'use a longer quote or ${h.n}."…"#2 for the 2nd occurrence',
                pos=node.pos,
            )
        idx = (occ or 1) - 1
        if idx >= len(hits):
            raise citation(
                f"quote {node.quote!r} occurs only {len(hits)} time(s) in ${h.n}", pos=node.pos
            )
        start = rs.span.start + hits[idx]
        sub = Span(rs.span.snapshot_id, start, start + len(needle))
        inp = _span_input(self.ctx, sub, handle=h.n, quote=node.quote)
        return Value(
            "Span",
            {"id": sub.id, "text": node.quote},
            inputs=[inp],
            steps=[{"op": "QUOTE", "out": sub.id}],
        )

    def _cell(self, cid: str, pos: int) -> Value:
        cell = self.ctx.cell_lookup(cid) if self.ctx.cell_lookup else self.ctx.store.get_cell(cid)
        if cell is None:
            raise citation(f"cell [{cid}] does not exist", pos=pos)
        if cell.get("status") != "grounded":
            raise citation(f"cell [{cid}] is not grounded", pos=pos)
        val = from_json(cell["value"])
        val.inputs = [{"kind": "cell", "id": cid, "formula": cell["formula"]}]
        val.steps = [{"op": f"[{cid}]", "out": display(val)}]
        return val

    # ------------------------------------------------------------ extraction
    def _extract(self, fn: str, val: Value, call: Call | None = None) -> Value:
        if val.type == "List":
            items = [self._extract(fn, x, call) for x in val.v]
            out = Value("List", items)
            return self._derive(fn, out, val, count=len(items))
        if val.type == "Number" and fn in ("NUM", "MONEY", "PCT_OF"):
            return val
        if val.type not in TEXTLIKE:
            raise syntax(f"{fn} expects a quote or text, got {val.type}")
        text = val.text()
        kw = (lambda k: call.kw(k)) if call else (lambda k: None)
        unit: str | None = None
        if fn == "NUM":
            r = ex.parse_quantity(text)
            out = Value("Number", r.value, r.unit)
        elif fn == "MONEY":
            cur = kw("currency")
            r = ex.parse_quantity(text, require=None if isinstance(cur, StringLit) else "money")
            unit = r.unit
            if isinstance(cur, StringLit):
                if r.unit and r.unit != cur.value:
                    raise extraction(f"currency in text is {r.unit}, not {cur.value}")
                unit = cur.value
            out = Value("Number", r.value, unit)
        elif fn == "PCT_OF":
            r = ex.parse_quantity(text, require="%")
            out = Value("Number", r.value, "%")
        elif fn == "DATE":
            f = kw("format")
            r = ex.parse_date(text, f.value if isinstance(f, StringLit) else None)
            out = Value("Date", r.value)
        elif fn == "DATETIME":
            r = ex.parse_datetime(text)
            out = Value("DateTime", r.value)
        elif fn == "DURATION":
            r = ex.parse_duration(text)
            out = Value("Number", r.value, "s")
        elif fn == "BOOL":
            r = ex.parse_bool(text)
            out = Value("Bool", r.value)
        else:  # TEXT
            return self._derive("TEXT", Value("Text", text), val)
        out = self._derive(fn, out, val)
        if val.type == "Span" and val.inputs:
            # record which bytes were consumed, as a precise sub-span
            src = val.inputs[-1]
            prefix = text[: r.start].encode("utf-8")
            consumed = text[r.start : r.end]
            base_start, _ = _span_bounds(src["id"])
            start = base_start + len(prefix)
            sp = Span(src["snapshot"], start, start + len(consumed.encode("utf-8")))
            inp = dict(src)
            inp.update({"consumed": consumed, "consumed_span": sp.id, "parsed": r.parsed})
            out.inputs = [x for x in out.inputs if x is not src] + [inp]
        return out

    # ------------------------------------------------------------ arithmetic
    def _same_unit(self, a: Value, b: Value, op: str) -> str | None:
        if a.unit != b.unit:
            raise syntax(
                f"{op}: unit mismatch ({a.unit or 'none'} vs {b.unit or 'none'})",
                hint="compare like with like; spanql never converts units implicitly",
            )
        return a.unit

    def _arith(self, op: str, a: Value, b: Value) -> Value:
        if a.type in ("Date", "DateTime") or b.type in ("Date", "DateTime"):
            raise syntax(f"'{op}' does not apply to dates", hint="use DAYS_BETWEEN(a, b)")
        a, b = self._number(a, op), self._number(b, op)
        if op in "+-":
            unit = self._same_unit(a, b, op)
            v = a.v + b.v if op == "+" else a.v - b.v
        elif op == "*":
            if a.unit and b.unit:
                raise syntax("'*': can't multiply two values that both have units")
            unit = a.unit or b.unit
            v = a.v * b.v
        else:
            if b.v == 0:
                raise extraction("division by zero")
            if a.unit and b.unit:
                unit = None if a.unit == b.unit else self._same_unit(a, b, "/")
            elif b.unit:
                raise syntax("'/': can't divide a plain number by a value with a unit")
            else:
                unit = a.unit
            v = a.v / b.v
        return self._derive(op, Value("Number", v, unit), a, b)

    def _compare(self, op: str, a: Value, b: Value) -> Value:
        if a.type in TEXTLIKE and b.type in TEXTLIKE:
            if op not in ("==", "!="):
                raise syntax(f"'{op}' does not apply to text")
            res = (a.text() == b.text()) == (op == "==")
            return self._derive(op, Value("Bool", res), a, b)
        if a.type in ("Date", "DateTime") or b.type in ("Date", "DateTime"):
            if a.type != b.type:
                raise syntax(f"'{op}': can't compare {a.type} with {b.type}")
            x, y = a.v, b.v
        elif a.type == "Bool" and b.type == "Bool":
            if op not in ("==", "!="):
                raise syntax(f"'{op}' does not apply to booleans")
            x, y = a.v, b.v
        else:
            a, b = self._number(a, op), self._number(b, op)
            self._same_unit(a, b, op)
            x, y = a.v, b.v
        res = {"==": x == y, "!=": x != y, ">": x > y, "<": x < y, ">=": x >= y, "<=": x <= y}[op]
        return self._derive(op, Value("Bool", res), a, b)

    # ----------------------------------------------------------------- calls
    def _call(self, c: Call) -> Value:
        fn = c.name
        if fn in ("FIND", "SYMBOLS", "LINES", "COLUMN"):
            return self._query(c)
        if fn == "FIELD":
            return self._field(c)
        if fn == "IF":
            self._arity(c, 3)
            cond = self.eval(c.args[0])
            if cond.type != "Bool":
                raise syntax("IF condition must be a boolean")
            branch = self.eval(c.args[1] if cond.v else c.args[2])
            out = Value(branch.type, branch.v, branch.unit, places=branch.places)
            return self._derive("IF", out, cond, branch)
        args = [self.eval(a) for a in c.args]
        if fn in ex_fns:
            self._arity(c, 1)
            return self._extract(fn, args[0], c)
        if fn in ("SUM", "AVG", "MIN", "MAX"):
            return self._aggregate(c, args)
        if fn == "COUNT":
            self._arity(c, 1)
            a = args[0]
            if a.type not in ("SpanSet", "List"):
                raise syntax(f"COUNT expects FIND(...)/SYMBOLS(...)/a list, got {a.type}")
            n = len(a.v)
            if n == 0 and not self.ctx.count_question:
                raise support(
                    "COUNT matched nothing, and the question is not a count",
                    hint="the sources may not contain this; consider NOT_FOUND",
                )
            return self._derive("COUNT", Value("Number", Decimal(n)), a)
        if fn == "DISTINCT":
            self._arity(c, 1)
            return self._distinct(args[0])
        if fn == "ROUND":
            places = self._places(c, positional=1)
            if places is None:
                raise syntax("ROUND needs places, e.g. ROUND(x, places:1)")
            a = self._number(args[0], "ROUND")
            return self._derive(
                "ROUND", Value("Number", quantize(a.v, places), a.unit, places=places), a
            )
        if fn in ("PCT", "RATIO"):
            self._arity(c, 2)
            a, b = self._number(args[0], fn), self._number(args[1], fn)
            self._same_unit(a, b, fn)
            if b.v == 0:
                raise extraction(f"{fn}: division by zero")
            v = a.v / b.v * (100 if fn == "PCT" else 1)
            places = self._places(c)
            if places is not None:
                v = quantize(v, places)
            return self._derive(
                fn, Value("Number", v, "%" if fn == "PCT" else None, places=places), a, b
            )
        if fn == "DIFF":
            self._arity(c, 2)
            a, b = args
            if a.type == b.type == "DateTime":
                secs = Decimal(str((a.v - b.v).total_seconds()))
                return self._derive("DIFF", Value("Number", secs, "s"), a, b)
            return self._arith("-", a, b)
        if fn == "ABS":
            self._arity(c, 1)
            a = self._number(args[0], "ABS")
            return self._derive("ABS", Value("Number", abs(a.v), a.unit), a)
        if fn == "DAYS_BETWEEN":
            self._arity(c, 2)
            a, b = (self._dateish(x, fn) for x in args)
            if isinstance(a.v, datetime) != isinstance(b.v, datetime):
                raise syntax("DAYS_BETWEEN: both arguments must be dates, or both date-times")
            delta = b.v - a.v
            days = (
                Decimal(delta.days)
                if not isinstance(a.v, datetime)
                else (Decimal(str(delta.total_seconds())) / Decimal(86400))
            )
            return self._derive(fn, Value("Number", days, "days"), a, b)
        if fn in ("BEFORE", "AFTER"):
            self._arity(c, 2)
            a, b = (self._dateish(x, fn) for x in args)
            if type(a.v) is not type(b.v):
                raise syntax(f"{fn}: can't compare {a.type} with {b.type}")
            res = a.v < b.v if fn == "BEFORE" else a.v > b.v
            return self._derive(fn, Value("Bool", res), a, b)
        if fn in ("AND", "OR"):
            if not args:
                raise syntax(f"{fn} needs arguments")
            for a in args:
                if a.type != "Bool":
                    raise syntax(f"{fn} expects booleans, got {a.type}")
            res = all(a.v for a in args) if fn == "AND" else any(a.v for a in args)
            return self._derive(fn, Value("Bool", res), *args)
        if fn == "NOT":
            self._arity(c, 1)
            if args[0].type != "Bool":
                raise syntax("NOT expects a boolean")
            return self._derive("NOT", Value("Bool", not args[0].v), args[0])
        if fn in ("EQ", "GT", "LT"):
            self._arity(c, 2)
            op = {"EQ": "==", "GT": ">", "LT": "<"}[fn]
            return self._compare(op, args[0], args[1])
        if fn == "CONTAINS":
            self._arity(c, 2)
            a, b = args
            if a.type not in TEXTLIKE or b.type not in TEXTLIKE:
                raise syntax("CONTAINS expects (text, text)")
            return self._derive("CONTAINS", Value("Bool", b.text() in a.text()), a, b)
        raise syntax(f"unknown function {fn}", pos=c.pos)

    def _arity(self, c: Call, n: int) -> None:
        if len(c.args) != n:
            raise syntax(f"{c.name} takes {n} argument(s), got {len(c.args)}", pos=c.pos)

    def _places(self, c: Call, positional: int | None = None) -> int | None:
        node = c.kw("places")
        if node is None and positional is not None and len(c.args) > positional:
            node = c.args[positional]
        if node is None:
            return None
        if (
            not isinstance(node, NumberLit)
            or node.value != node.value.to_integral_value()
            or not (0 <= node.value <= 10)
        ):
            raise syntax("places must be a whole number from 0 to 10", pos=node.pos)
        return int(node.value)

    def _dateish(self, v: Value, fn: str) -> Value:
        if v.type in ("Date", "DateTime"):
            return v
        if v.type in TEXTLIKE:
            return self._extract("DATE", v)
        raise syntax(f"{fn} expects dates, got {v.type}")

    def _elements(self, args: list[Value], fn: str) -> tuple[list[Value], Value | None]:
        if len(args) == 1 and args[0].type == "List":
            return list(args[0].v), args[0]
        if len(args) == 1 and args[0].type == "SpanSet":
            raise syntax(
                f"{fn} needs values, not matches",
                hint=f'use {fn}(FIELD("name", FIND(...))) or {fn}(COLUMN(...))',
            )
        return args, None

    def _aggregate(self, c: Call, args: list[Value]) -> Value:
        fn = c.name
        elems, container = self._elements(args, fn)
        if not elems:
            raise support(
                f"{fn} over an empty set",
                hint="nothing in the sources supports this; consider NOT_FOUND",
            )
        if fn in ("MIN", "MAX") and all(e.type in ("Date", "DateTime") for e in elems):
            pick = min(elems, key=lambda e: e.v) if fn == "MIN" else max(elems, key=lambda e: e.v)
            out = Value(pick.type, pick.v)
            return self._derive(fn, out, *(([container] if container else []) + [pick]))
        nums = [self._number(e, fn) for e in elems]
        units = {n.unit for n in nums}
        if len(units) > 1:
            raise syntax(f"{fn}: mixed units {sorted(u or 'none' for u in units)}")
        unit = units.pop()
        vals = [n.v for n in nums]
        if fn == "SUM":
            v = sum(vals, Decimal(0))
            out = Value("Number", v, unit)
        elif fn == "AVG":
            v = sum(vals, Decimal(0)) / Decimal(len(vals))
            places = self._places(c)
            out = Value(
                "Number", quantize(v, places) if places is not None else v, unit, places=places
            )
        else:
            pick = min(nums, key=lambda n: n.v) if fn == "MIN" else max(nums, key=lambda n: n.v)
            return self._derive(
                fn, Value("Number", pick.v, unit), *(([container] if container else []) + [pick])
            )
        srcs = [container] if container else nums
        return self._derive(fn, out, *srcs, n=len(vals))

    def _distinct(self, a: Value) -> Value:
        items: list[Value] = []
        seen: set[str] = set()
        if a.type == "SpanSet":
            names = a.meta.get("names")
            for i, sp in enumerate(a.v):
                label = names[i] if names else self.ctx.store.resolve(sp).text.strip()
                if label in seen:
                    continue
                seen.add(label)
                items.append(Value("Text", label, inputs=[_span_input(self.ctx, sp)]))
        elif a.type == "List":
            for x in a.v:
                key = f"{x.type}:{display(x)}"
                if key not in seen:
                    seen.add(key)
                    items.append(x)
        else:
            raise syntax(f"DISTINCT expects matches or a list, got {a.type}")
        if not items and not self.ctx.count_question:
            raise support(
                "DISTINCT matched nothing, so there is nothing to list",
                "loosen the query (src:, kind:, name:) or write NOT_FOUND(...)",
            )
        return self._derive("DISTINCT", Value("List", items), a, count=len(items))

    # ---------------------------------------------------------------- queries
    def _query(self, c: Call) -> Value:
        runner = {
            "FIND": q.run_find,
            "SYMBOLS": q.run_symbols,
            "LINES": q.run_lines,
            "COLUMN": q.run_column,
        }[c.name]
        res = runner(self.ctx.store, c)
        rec = res.record()
        self.ctx.queries.append(rec)
        step = {
            "op": c.name,
            "out": f"{len(res.spans)} matches",
            "sql": res.sql,
            "params": res.params,
        }
        if c.name == "COLUMN":
            items = []
            for sp in res.spans:
                rs = self.ctx.store.resolve(sp)
                items.append(
                    Value(
                        "Span", {"id": sp.id, "text": rs.text}, inputs=[_span_input(self.ctx, sp)]
                    )
                )
            return Value("List", items, inputs=[rec], steps=[step])
        val = Value("SpanSet", res.spans, inputs=[rec], steps=[step])
        val.meta["result"] = res
        if "names" in res.extra:
            val.meta["names"] = res.extra["names"]
        return val

    def _field(self, c: Call) -> Value:
        if len(c.args) != 2:
            raise syntax(
                'FIELD(name) is only valid inside FIND(...); use FIELD("name", FIND(...))'
                " to read values",
                pos=c.pos,
            )
        name_node = c.args[0]
        if not isinstance(name_node, StringLit):
            raise syntax("FIELD name must be a string literal", pos=name_node.pos)
        setv = self.eval(c.args[1])
        if setv.type != "SpanSet" or "result" not in setv.meta:
            raise syntax("FIELD's second argument must be FIND(...)", pos=c.pos)
        pairs = q.field_values(self.ctx.store, name_node.value, setv.meta["result"])
        items: list[Value] = []
        for raw, sp in pairs:
            inp = _span_input(self.ctx, sp)
            d = q.as_decimal(raw)
            if d is not None:
                items.append(Value("Number", d, inputs=[inp]))
            else:
                items.append(Value("Text", str(raw), inputs=[inp]))
        out = Value("List", items)
        return self._derive(
            "FIELD", out, setv, field=name_node.value, missing=len(setv.v) - len(items)
        )


ex_fns = frozenset(("NUM", "MONEY", "PCT_OF", "DATE", "DATETIME", "DURATION", "TEXT", "BOOL"))


def _span_bounds(span_id: str) -> tuple[int, int]:
    _, s, e = Span.parse(span_id)
    return s, e


def is_date(v: Any) -> bool:
    return isinstance(v, date)
