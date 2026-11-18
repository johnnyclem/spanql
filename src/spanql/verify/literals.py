"""Verifier check V4: data comes only from references.

The model may not smuggle an ungrounded value into an answer, either as a numeric
literal in a computation or as digits in the prose template. A literal is allowed
when it is configuration (``places:1``), part of a query constraint (a FIND filter
such as ``FIELD("status") >= 500``), or *appears in the user's question* (the user
supplied it, the model did not invent it). Every answer value must also trace to
at least one source reference.
"""

from __future__ import annotations

import re
from decimal import Decimal, InvalidOperation

from ..errors import LiteralError
from ..formula.ast import (Ambiguous, Answer, BinOp, BoolLit, Call, CellRef, Compare, Formula, Neg, Node, NotFound,
                           Num, Ref, Str, walk)
from ..formula.functions import FUNCTIONS, QUERY_FUNCTIONS

_QUESTION_NUM = re.compile(r"(?<![\w.])\d[\d,]*(?:\.\d+)?")
# digits not glued to a letter: "Q3", "FY2026", "p95" are labels; "2026", "41", "4.82" are values
_PROSE_NUM = re.compile(r"(?<![A-Za-z_\d])\d[\d,]*(?:\.\d+)?")


def question_numbers(question: str | None) -> tuple[set[Decimal], set[str]]:
    """Numbers (as Decimals) and raw digit strings that appear in the question."""
    nums: set[Decimal] = set()
    raw: set[str] = set()
    for m in _QUESTION_NUM.finditer(question or ""):
        s = m.group(0).rstrip(",")
        raw.add(s.replace(",", ""))
        try:
            nums.add(Decimal(s.replace(",", "")))
        except InvalidOperation:
            continue
    return nums, raw


def check_prose(text: str, question: str | None, what: str) -> list[str]:
    """Digits in model-written prose must be labels (``Q3``) or echo the question."""
    _, raw = question_numbers(question)
    problems = []
    text = re.sub(r"\{\d+\}", " ", text)  # {0}, {1} are placeholders, not digits
    for m in _PROSE_NUM.finditer(text):
        token = m.group(0).rstrip(",.").replace(",", "")
        if token and token not in raw:
            problems.append(f"{what} contains the number {m.group(0)!r}; numbers may only appear through "
                            "{0}, {1} placeholders (or if the question itself contains them)")
    return problems


def _is_source(node: Node) -> bool:
    return isinstance(node, (Ref, CellRef)) or (isinstance(node, Call) and node.name in QUERY_FUNCTIONS)


class _Visitor:
    def __init__(self, src: str, question: str | None):
        self.src = src
        self.nums, _ = question_numbers(question)
        self.problems: list[str] = []

    def num_ok(self, n: Num) -> bool:
        return n.value in self.nums

    def visit(self, node: Node, role: str) -> None:
        if isinstance(node, Num):
            if role in ("data", "compare") and not self.num_ok(node):
                self.problems.append(
                    f"the number {node.text} at character {node.pos} is not from a source and is not in the "
                    "question; take values from $n references (or FIND/COLUMN) instead of typing them")
        elif isinstance(node, Str):
            if role == "data":
                self.problems.append(
                    f"the text {node.value!r} at character {node.pos} is used as a value; values must come from "
                    "source spans (use TEXT($n.\"...\")), and prose belongs in text:\"...\" with placeholders")
        elif isinstance(node, (BoolLit, Ref, CellRef)):
            return
        elif isinstance(node, Neg):
            self.visit(node.operand, role)
        elif isinstance(node, BinOp):
            self.visit(node.left, "data")
            self.visit(node.right, "data")
        elif isinstance(node, Compare):
            r = "query" if role == "query" else "compare"
            self.visit(node.left, r)
            self.visit(node.right, r)
        elif isinstance(node, Call):
            spec = FUNCTIONS.get(node.name)
            if spec is None:
                return
            in_query = role == "query" or spec.query
            for i, a in enumerate(node.args):
                p = spec.params[i] if i < len(spec.params) else spec.variadic
                self.visit(a, "query" if in_query else (p.role if p else "data"))
            for k, v in node.kwargs:
                p = spec.kwargs.get(k)
                self.visit(v, "query" if in_query else (p.role if p else "data"))


def check_literals(formula: Formula, src: str, question: str | None) -> None:
    """Raise :class:`LiteralError` listing every V4 violation."""
    problems: list[str] = []
    if isinstance(formula, Answer):
        v = _Visitor(src, question)
        for i, arg in enumerate(formula.args):
            v.visit(arg, "data")
            if not any(_is_source(n) for n in walk(arg)):
                v.problems.append(f"value {i} ({src[arg.pos:arg.end]}) is not derived from any source; every ANSWER "
                                  "value must come from a $n reference, a FIND/COLUMN/SYMBOLS query, or a cell")
        problems += v.problems
        if formula.template:
            problems += check_prose(formula.template, question, "the text: template")
    elif isinstance(formula, (NotFound, Ambiguous)):
        problems += check_prose(formula.message, question, "the explanation")
    if problems:
        raise LiteralError(
            problems[0], problems=problems,
            hint="values come only from references; only formatting (places:), query filters, and numbers that "
                 "appear in the question may be typed")
