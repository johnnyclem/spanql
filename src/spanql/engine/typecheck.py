"""Static type checking (verifier check V1), run before any evaluation."""

from __future__ import annotations

from spanql.engine.errors import Problem
from spanql.formula.ast import (
    Ambiguous,
    Answer,
    BinOp,
    BoolLit,
    Call,
    CellRef,
    Compare,
    Formula,
    HandleRef,
    Neg,
    Node,
    NumberLit,
    StringLit,
)
from spanql.formula.spec import KWARGS

ANY = "Any"
EXTRACT_OUT = {
    "NUM": "Number",
    "MONEY": "Number",
    "PCT_OF": "Number",
    "DURATION": "Number",
    "DATE": "Date",
    "DATETIME": "DateTime",
    "TEXT": "Text",
    "BOOL": "Bool",
}
SCALAR_NUMERIC = {"Number", "Text", ANY}


class TypeChecker:
    def __init__(self) -> None:
        self.problems: list[Problem] = []

    def err(self, node: Node, msg: str, hint: str | None = None) -> str:
        self.problems.append(Problem("V1", msg, hint, node.pos))
        return ANY

    def check_formula(self, f: Formula) -> list[Problem]:
        if isinstance(f, Answer):
            for e in f.exprs:
                if self.t(e) == "SpanSet":
                    self.err(
                        e,
                        "an answer can't be a bare set of matches",
                        hint="wrap it: COUNT(FIND(...)) or DISTINCT(SYMBOLS(...))",
                    )
        elif isinstance(f, Ambiguous) and len(f.handles) < 2:
            self.err(f, "AMBIGUOUS needs at least two conflicting handles")
        return self.problems

    def numeric(self, node: Node, fn: str) -> None:
        t = self.t(node)
        if t not in SCALAR_NUMERIC:
            self.err(node, f"{fn} expects a number, got {t}")

    def t(self, node: Node) -> str:
        match node:
            case NumberLit():
                return "Number"
            case StringLit() | HandleRef():
                return "Text"
            case BoolLit():
                return "Bool"
            case CellRef():
                return ANY
            case Neg(operand=o):
                self.numeric(o, "'-'")
                return "Number"
            case BinOp(op=op, left=left, right=right):
                self.numeric(left, f"'{op}'")
                self.numeric(right, f"'{op}'")
                return "Number"
            case Compare(left=left, right=right):
                for side in (left, right):
                    if isinstance(side, Call) and side.name == "FIELD" and len(side.args) == 1:
                        return self.err(
                            side,
                            'FIELD("name") comparisons are only allowed as '
                            "FIND(...) / COLUMN(...) filters",
                        )
                    st = self.t(side)
                    if st in ("SpanSet", "List"):
                        self.err(side, f"can't compare a {st}")
                return "Bool"
            case Call():
                return self.call(node)
        return self.err(node, "unsupported expression")

    def call(self, c: Call) -> str:
        allowed = KWARGS.get(c.name, frozenset())
        for k, v in c.kwargs:
            if k not in allowed:
                self.err(
                    v,
                    f"{c.name} has no keyword '{k}'",
                    hint=f"allowed: {', '.join(sorted(allowed)) or 'none'}",
                )
        n = len(c.args)
        fn = c.name

        def arity(lo: int, hi: int | None = None) -> bool:
            hi = lo if hi is None else hi
            if not (lo <= n <= hi):
                want = str(lo) if lo == hi else f"{lo}-{hi}"
                self.err(c, f"{fn} takes {want} argument(s), got {n}")
                return False
            return True

        if fn in ("FIND", "SYMBOLS", "LINES"):
            if fn != "FIND" and n:
                self.err(c, f"{fn} takes only keyword arguments")
            if fn == "LINES" and c.kw("re") is None:
                self.err(c, 'LINES needs re:"…"')
            for k, v in c.kwargs:
                if not isinstance(v, StringLit):
                    self.err(v, f"{fn}: {k} must be a string literal")
            for p in c.args:
                self.predicate(p, fn)
            return "SpanSet"
        if fn == "COLUMN":
            if n < 1 or not isinstance(c.args[0], StringLit):
                self.err(c, 'COLUMN needs a header string, e.g. COLUMN(src:"x.csv", "amount")')
            for p in c.args[1:]:
                self.predicate(p, fn)
            return "List"
        if fn == "FIELD":
            if n == 1:
                return self.err(
                    c,
                    'FIELD("name") alone is only valid as a FIND filter',
                    hint='read values with FIELD("name", FIND(...))',
                )
            if arity(2):
                if not isinstance(c.args[0], StringLit):
                    self.err(c.args[0], "FIELD name must be a string literal")
                inner = c.args[1]
                if not (isinstance(inner, Call) and inner.name == "FIND"):
                    self.err(inner, "FIELD's second argument must be FIND(...)")
                else:
                    self.t(inner)
            return "List"
        types = [self.t(a) for a in c.args]
        if fn in EXTRACT_OUT:
            if arity(1) and types[0] not in ("Text", "List", ANY, "Number"):
                self.err(c.args[0], f"{fn} expects a quote or text, got {types[0]}")
            if types and types[0] == "List":
                return "List"
            return EXTRACT_OUT[fn]
        if fn in ("SUM", "AVG", "MIN", "MAX"):
            if n == 0:
                return self.err(c, f"{fn} needs arguments")
            for a, t in zip(c.args, types, strict=True):
                if t == "SpanSet":
                    self.err(
                        a,
                        f"{fn} needs values, not matches",
                        hint=f'use {fn}(FIELD("name", FIND(...))) or {fn}(COLUMN(...))',
                    )
                elif t not in ("Number", "Text", "List", ANY, "Date", "DateTime"):
                    self.err(a, f"{fn} can't aggregate {t}")
            return ANY if fn in ("MIN", "MAX") else "Number"
        if fn == "COUNT":
            if arity(1) and types[0] not in ("SpanSet", "List", ANY):
                self.err(c.args[0], f"COUNT expects FIND(...)/SYMBOLS(...)/a list, got {types[0]}")
            return "Number"
        if fn == "DISTINCT":
            if arity(1) and types[0] not in ("SpanSet", "List", ANY):
                self.err(c.args[0], f"DISTINCT expects matches or a list, got {types[0]}")
            return "List"
        if fn == "ROUND":
            if arity(1, 2):
                self.numeric(c.args[0], fn)
                if n == 1 and c.kw("places") is None:
                    self.err(c, "ROUND needs places, e.g. ROUND(x, places:1)")
            return "Number"
        if fn in ("PCT", "DIFF", "RATIO"):
            if arity(2):
                for a, t in zip(c.args, types, strict=True):
                    if t not in SCALAR_NUMERIC and not (fn == "DIFF" and t == "DateTime"):
                        self.err(a, f"{fn} expects numbers, got {t}")
            return "Number"
        if fn == "ABS":
            if arity(1):
                self.numeric(c.args[0], fn)
            return "Number"
        if fn in ("DAYS_BETWEEN", "BEFORE", "AFTER"):
            if arity(2):
                for a, t in zip(c.args, types, strict=True):
                    if t not in ("Date", "DateTime", "Text", ANY):
                        self.err(a, f"{fn} expects dates, got {t}")
            return "Number" if fn == "DAYS_BETWEEN" else "Bool"
        if fn == "IF":
            if arity(3):
                if types[0] not in ("Bool", ANY):
                    self.err(c.args[0], f"IF condition must be a boolean, got {types[0]}")
                return types[1]
            return ANY
        if fn in ("AND", "OR"):
            if n == 0:
                self.err(c, f"{fn} needs arguments")
            for a, t in zip(c.args, types, strict=True):
                if t not in ("Bool", ANY):
                    self.err(a, f"{fn} expects booleans, got {t}")
            return "Bool"
        if fn == "NOT":
            if arity(1) and types[0] not in ("Bool", ANY):
                self.err(c.args[0], f"NOT expects a boolean, got {types[0]}")
            return "Bool"
        if fn in ("EQ", "GT", "LT", "CONTAINS"):
            arity(2)
            return "Bool"
        return self.err(c, f"unknown function {fn}")

    def predicate(self, p: Node, fn: str) -> None:
        def is_field(x: Node) -> bool:
            return (
                isinstance(x, Call)
                and x.name == "FIELD"
                and len(x.args) == 1
                and isinstance(x.args[0], StringLit)
            )

        def is_lit(x: Node) -> bool:
            return isinstance(x, NumberLit | StringLit | BoolLit)

        if isinstance(p, Compare):
            if not (
                (is_field(p.left) and is_lit(p.right)) or (is_field(p.right) and is_lit(p.left))
            ):
                self.err(p, f'{fn} filters compare FIELD("name") with a literal')
            return
        if isinstance(p, Call):
            if p.name in ("EQ", "GT", "LT", "CONTAINS") and len(p.args) == 2:
                a, b = p.args
                if (is_field(a) and is_lit(b)) or (
                    is_field(b) and is_lit(a) and p.name != "CONTAINS"
                ):
                    return
            if p.name in ("AND", "OR", "NOT") and p.args:
                for a in p.args:
                    self.predicate(a, fn)
                return
        hint = ""
        if isinstance(p, Call) and p.name == "CONTAINS":
            hint = '; to filter by text use the keyword match:"words" (or re:"regex")'
        elif isinstance(p, Call) and p.name == "FIELD" and fn == "COLUMN":
            hint = (
                '; the column to read is the string after src:, e.g. COLUMN(src:"x.csv", "amount")'
            )
        self.err(p, f'{fn}: positional arguments must be filters like FIELD("status") >= 500{hint}')


def typecheck(f: Formula) -> list[Problem]:
    return TypeChecker().check_formula(f)
