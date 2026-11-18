"""Row predicates inside FIND/COLUMN: ``FIELD("status") >= 500``, ``AND(...)``, ``CONTAINS(...)``.

Predicates are deliberately restricted so they compile to SQL: a ``FIELD`` compared
with a literal, combined with AND / OR / NOT.
"""

from __future__ import annotations

import re
from dataclasses import dataclass
from decimal import Decimal

from ..errors import FormulaTypeError
from .ast import BoolLit, Call, Compare, Neg, Node, Num, Str

FIELD_NAME = re.compile(r"^[\w.\-@]{1,64}$")
_FLIP = {">": "<", "<": ">", ">=": "<=", "<=": ">=", "==": "==", "!=": "!="}
_FUNC_OPS = {"EQ": "==", "NE": "!=", "GT": ">", "GTE": ">=", "LT": "<", "LTE": "<="}


@dataclass(frozen=True)
class PCmp:
    field: str
    op: str  # == != > >= < <=
    value: Decimal | str | bool


@dataclass(frozen=True)
class PContains:
    field: str
    text: str


@dataclass(frozen=True)
class PAnd:
    items: tuple


@dataclass(frozen=True)
class POr:
    items: tuple


@dataclass(frozen=True)
class PNot:
    item: object


@dataclass(frozen=True)
class PTrue:
    value: bool


Pred = PCmp | PContains | PAnd | POr | PNot | PTrue


def _field(node: Node) -> str | None:
    if isinstance(node, Call) and node.name == "FIELD" and len(node.args) == 1 and isinstance(node.args[0], Str):
        name = node.args[0].value
        if not FIELD_NAME.match(name):
            raise FormulaTypeError(f"field name {name!r} may only contain letters, digits, `_ . - @`", pos=node.pos)
        return name
    return None


def _literal(node: Node):
    if isinstance(node, Num):
        return node.value
    if isinstance(node, Neg) and isinstance(node.operand, Num):
        return -node.operand.value
    if isinstance(node, Str):
        return node.value
    if isinstance(node, BoolLit):
        return node.value
    return None


def _cmp(op: str, left: Node, right: Node, pos: int) -> PCmp:
    lf, rf = _field(left), _field(right)
    if lf is not None and _literal(right) is not None:
        return PCmp(lf, op, _literal(right))
    if rf is not None and _literal(left) is not None:
        return PCmp(rf, _FLIP[op], _literal(left))
    raise FormulaTypeError(
        'a FIND/COLUMN filter must compare FIELD("name") with a literal, e.g. FIELD("status") >= 500',
        pos=pos,
    )


def to_predicate(node: Node) -> Pred:
    """Convert a predicate AST to a :class:`Pred`; raises FormulaTypeError if it cannot be a SQL filter."""
    if isinstance(node, BoolLit):
        return PTrue(node.value)
    if isinstance(node, Compare):
        return _cmp(node.op, node.left, node.right, node.pos)
    if isinstance(node, Call):
        if node.name in _FUNC_OPS and len(node.args) == 2:
            return _cmp(_FUNC_OPS[node.name], node.args[0], node.args[1], node.pos)
        if node.name in ("AND", "OR") and len(node.args) >= 2:
            items = tuple(to_predicate(a) for a in node.args)
            return PAnd(items) if node.name == "AND" else POr(items)
        if node.name == "NOT" and len(node.args) == 1:
            return PNot(to_predicate(node.args[0]))
        if node.name == "CONTAINS" and len(node.args) == 2:
            f, t = _field(node.args[0]), node.args[1]
            if f is not None and isinstance(t, Str):
                return PContains(f, t.value)
    raise FormulaTypeError(
        'a FIND/COLUMN filter must be built from FIELD("name") compared with literals '
        "(==, !=, >, >=, <, <=, CONTAINS) combined with AND/OR/NOT",
        pos=node.pos,
    )
