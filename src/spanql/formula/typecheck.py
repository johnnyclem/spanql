"""Bottom-up type checking. Collects every problem it can find so a single retry
can fix them all."""

from __future__ import annotations

import difflib
import re
from typing import Callable

from ..errors import FormulaTypeError
from .ast import (Ambiguous, Answer, BinOp, BoolLit, Call, CellRef, Compare, Formula, Neg, Node, NotFound, Num,
                  Ref, Str)
from .functions import (ANY, AS_TYPES, BOOL, DATE, DATETIME, DURATION, FIELD, FUNCTIONS, NUMBER, ORDERED, SPAN,
                        SPANSET, TEXT, FuncSpec, coercible)
from .predicate import to_predicate

CellTypeLookup = Callable[[int | str], "str | None"]
_PLACEHOLDER = re.compile(r"\{(\d+)\}")


def template_problems(template: str, n_args: int) -> list[str]:
    problems = []
    for m in _PLACEHOLDER.finditer(template):
        if int(m.group(1)) >= n_args:
            problems.append(f"template placeholder {{{m.group(1)}}} refers to a value that does not exist "
                            f"(ANSWER has {n_args})")
    rest = _PLACEHOLDER.sub("", template)
    if "{" in rest or "}" in rest:
        problems.append("template may only contain placeholders like {0}, {1}; remove other braces")
    return problems


def _norm(t: str) -> str:
    return TEXT if t in (SPAN, SPANSET) else t


class _Checker:
    def __init__(self, cell_type: CellTypeLookup | None):
        self.problems: list[str] = []
        self.first_pos: int | None = None
        self.cell_type = cell_type
        self.arg_types: list[str] = []
        self.cur_index = 0

    def err(self, msg: str, node: Node | None = None) -> str:
        if node is not None:
            msg = f"{msg} (at character {node.pos})"
            if self.first_pos is None:
                self.first_pos = node.pos
        self.problems.append(msg)
        return ANY

    # ----------------------------------------------------------------- nodes
    def check(self, node: Node, in_query: bool = False) -> str:
        t = self._check(node, in_query)
        node.typ = t
        return t

    def _check(self, node: Node, in_query: bool) -> str:
        if isinstance(node, Num):
            return NUMBER
        if isinstance(node, Str):
            return TEXT
        if isinstance(node, BoolLit):
            return BOOL
        if isinstance(node, Ref):
            return SPAN
        if isinstance(node, CellRef):
            return self._cellref(node)
        if isinstance(node, Neg):
            t = self.check(node.operand, in_query)
            if t not in (NUMBER, DURATION, ANY):
                return self.err(f"unary minus needs a Number or Duration, got {t}", node)
            return t
        if isinstance(node, BinOp):
            return self._binop(node, in_query)
        if isinstance(node, Compare):
            return self._compare(node, in_query)
        if isinstance(node, Call):
            return self._call(node, in_query)
        return self.err("unsupported expression", node)

    def _cellref(self, node: CellRef) -> str:
        if isinstance(node.key, int):
            if node.key >= self.cur_index:
                return self.err(f"[{node.key}] must refer to an EARLIER ANSWER argument (this is argument "
                                f"{self.cur_index}); arguments are numbered from 0", node)
            return self.arg_types[node.key]
        t = self.cell_type(node.key) if self.cell_type else None
        if t is None:
            return self.err(f"unknown cell [{node.key}]", node)
        return t

    def _binop(self, node: BinOp, in_query: bool) -> str:
        lt, rt = self.check(node.left, in_query), self.check(node.right, in_query)
        if ANY in (lt, rt):
            return ANY
        op = node.op
        table = {
            ("+", NUMBER, NUMBER): NUMBER, ("-", NUMBER, NUMBER): NUMBER,
            ("*", NUMBER, NUMBER): NUMBER, ("/", NUMBER, NUMBER): NUMBER,
            ("+", DATE, DURATION): DATE, ("-", DATE, DURATION): DATE, ("-", DATE, DATE): DURATION,
            ("+", DATETIME, DURATION): DATETIME, ("-", DATETIME, DURATION): DATETIME,
            ("-", DATETIME, DATETIME): DURATION,
            ("+", DURATION, DURATION): DURATION, ("-", DURATION, DURATION): DURATION,
            ("*", DURATION, NUMBER): DURATION, ("*", NUMBER, DURATION): DURATION,
            ("/", DURATION, NUMBER): DURATION, ("/", DURATION, DURATION): NUMBER,
            ("+", DURATION, DATE): DATE, ("+", DURATION, DATETIME): DATETIME,
        }
        res = table.get((op, lt, rt))
        if res is None:
            hint = ""
            if SPAN in (lt, rt) or SPANSET in (lt, rt):
                hint = "; a span is text, wrap it in NUM(...), MONEY(...), DATE(...) etc. first"
            return self.err(f"cannot compute {lt} {op} {rt}{hint}", node)
        return res

    def _compare(self, node: Compare, in_query: bool) -> str:
        lt, rt = self.check(node.left, in_query), self.check(node.right, in_query)
        self._compare_types(node.op, lt, rt, node, in_query)
        return BOOL

    def _compare_types(self, op: str, lt: str, rt: str, node: Node, in_query: bool) -> None:
        if FIELD in (lt, rt):
            if not in_query:
                self.err('FIELD("...") only works inside FIND(...) / COLUMN(...) filters', node)
            return
        if ANY in (lt, rt):
            return
        l, r = _norm(lt), _norm(rt)
        if l != r:
            self.err(f"cannot compare {lt} with {rt}", node)
        elif op not in ("==", "!=") and l not in ORDERED and l != TEXT:
            self.err(f"{lt} values cannot be ordered with {op}", node)

    # ----------------------------------------------------------------- calls
    def _call(self, node: Call, in_query: bool) -> str:
        spec = FUNCTIONS.get(node.name)
        if spec is None:
            close = difflib.get_close_matches(node.name.upper(), list(FUNCTIONS), n=1)
            for a in node.args:
                self.check(a, in_query)
            for _, v in node.kwargs:
                self.check(v, in_query)
            return self.err(f"unknown function {node.name}" + (f"; did you mean {close[0]}?" if close else ""), node)

        if node.name == "FIELD":
            if not in_query:
                return self.err('FIELD("...") only works inside FIND(...) / COLUMN(...) filters', node)
            if len(node.args) != 1 or not isinstance(node.args[0], Str) or node.kwargs:
                return self.err('FIELD takes one quoted field name, e.g. FIELD("status")', node)
            node.args[0].typ = TEXT
            return FIELD

        child_in_query = in_query or spec.query
        arg_types = [self.check(a, child_in_query) for a in node.args]
        self._arity(spec, node, len(arg_types))
        for i, (a, t) in enumerate(zip(node.args, arg_types)):
            param = spec.params[i] if i < len(spec.params) else spec.variadic
            if param is None:
                continue
            if spec.query and i >= len(spec.params):  # a row predicate
                if t not in (BOOL, ANY):
                    self.err(f"{spec.name} filters must be true/false conditions, got {t}", a)
                else:
                    try:
                        to_predicate(a)
                    except FormulaTypeError as e:
                        self.err(e.message, a)
                continue
            if not coercible(t, param.types):
                self.err(f"{spec.name} argument {i + 1} must be {_or(param.types)}, got {t}"
                         + self._hint(spec, t), a)
        kw_lits: dict[str, str | None] = {}
        for name, v in node.kwargs:
            kt = self.check(v, child_in_query)
            p = spec.kwargs.get(name)
            kw_lits[name] = v.value if isinstance(v, Str) else None
            if p is None:
                valid = ", ".join(f"{k}:" for k in spec.kwargs) or "no keywords"
                self.err(f"{spec.name} has no keyword `{name}:` (it accepts {valid})", v)
            elif not coercible(kt, p.types):
                self.err(f"keyword `{name}:` of {spec.name} must be {_or(p.types)}, got {kt}", v)
            elif name == "as" and (not isinstance(v, Str) or v.value.lower() not in AS_TYPES):
                self.err(f"`as:` must be one of {', '.join(repr(k) for k in AS_TYPES)}", v)
        self._cross_checks(spec, node, arg_types, in_query)
        res = spec.returns
        return res(arg_types, kw_lits) if callable(res) else res

    def _arity(self, spec: FuncSpec, node: Call, n: int) -> None:
        lo = spec.required
        hi = None if spec.variadic else len(spec.params)
        if n < lo or (hi is not None and n > hi):
            want = f"{lo}" if hi == lo else (f"at least {lo}" if hi is None else f"{lo} to {hi}")
            self.err(f"{spec.name} takes {want} argument(s), got {n}", node)

    def _hint(self, spec: FuncSpec, t: str) -> str:
        if t == TEXT and spec.group == "extraction":
            return '; extractors read a source span such as $1 or $1."quote", not a text literal'
        if t in (SPAN, SPANSET) and spec.group in ("math", "dates", "aggregation"):
            return "; a span is text, wrap it in NUM(...), MONEY(...), DATE(...) etc. first"
        return ""

    def _cross_checks(self, spec: FuncSpec, node: Call, arg_types: list[str], in_query: bool) -> None:
        n = spec.name
        known = [t for t in arg_types if t != ANY]
        if n in ("SUM", "AVG", "MIN", "MAX") and known:
            if SPANSET in known and len(known) > 1:
                self.err(f"{n} takes either one FIND/COLUMN result or several values, not both", node)
            elif len({t for t in known}) > 1:
                self.err(f"{n} needs all values to have the same type, got {', '.join(known)}", node)
            elif n in ("SUM", "AVG") and known[0] in (DATE, DATETIME):
                self.err(f"{n} cannot add dates", node)
        if n == "DIFF" and len(known) == 2 and _norm(known[0]) != _norm(known[1]):
            self.err(f"DIFF needs two values of the same type, got {known[0]} and {known[1]}", node)
        if n == "IF" and len(arg_types) == 3 and ANY not in arg_types[1:] and _norm(arg_types[1]) != _norm(arg_types[2]):
            self.err(f"IF branches must have the same type, got {arg_types[1]} and {arg_types[2]}", node)
        if n in ("EQ", "NE", "GT", "GTE", "LT", "LTE") and len(arg_types) == 2:
            self._compare_types({"EQ": "==", "NE": "!="}.get(n, ">"), arg_types[0], arg_types[1], node, in_query)
        if n == "LINES" and "src" not in {k for k, _ in node.kwargs}:
            self.err("LINES needs src:\"...\"", node)
        if n == "COLUMN" and node.args and not isinstance(node.args[0], Str):
            self.err("COLUMN's first argument must be the quoted column name, e.g. COLUMN(\"amount\", src:\"data/*.csv\")", node)


def _or(types: frozenset[str]) -> str:
    names = sorted(t for t in types if t != FIELD)
    return " or ".join(names) if len(names) <= 3 else "a value"


def typecheck(formula: Formula, *, cell_type: CellTypeLookup | None = None) -> list[str]:
    """Annotate nodes with types; return the type of each ANSWER argument (empty for refusals).

    Raises :class:`FormulaTypeError` listing every problem found.
    """
    c = _Checker(cell_type)
    types: list[str] = []
    if isinstance(formula, Answer):
        for i, a in enumerate(formula.args):
            c.cur_index = i
            t = c.check(a)
            if t in (FIELD,):
                t = c.err('FIELD("...") only works inside FIND(...) / COLUMN(...) filters', a)
            c.arg_types.append(t)
        types = c.arg_types
        if formula.template is not None:
            c.problems.extend(template_problems(formula.template, len(formula.args)))
    elif isinstance(formula, (NotFound, Ambiguous)):
        pass
    if c.problems:
        raise FormulaTypeError(c.problems[0], pos=c.first_pos, problems=c.problems)
    return types
