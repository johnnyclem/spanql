"""AST for the spanql formula language.

Nodes use identity equality (``eq=False``) so passes can key side tables by node.
``typ`` is filled in by the type checker.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from decimal import Decimal


@dataclass(eq=False, slots=True)
class Node:
    pos: int
    end: int
    typ: str | None = field(default=None, kw_only=True)


@dataclass(eq=False, slots=True)
class Num(Node):
    value: Decimal
    text: str


@dataclass(eq=False, slots=True)
class Str(Node):
    value: str


@dataclass(eq=False, slots=True)
class BoolLit(Node):
    value: bool


@dataclass(eq=False, slots=True)
class Ref(Node):
    """``$1`` (whole candidate span) or ``$1."quote"`` / ``$1."quote"#2`` (exact substring)."""

    handle: int
    quote: str | None = None
    occurrence: int | None = None


@dataclass(eq=False, slots=True)
class CellRef(Node):
    """``[2]``: an earlier argument of the same ANSWER, or ``[c_7Hq2]``: a stored cell."""

    key: int | str


@dataclass(eq=False, slots=True)
class Neg(Node):
    operand: Node


@dataclass(eq=False, slots=True)
class BinOp(Node):
    op: str  # + - * /
    left: Node
    right: Node


@dataclass(eq=False, slots=True)
class Compare(Node):
    op: str  # == != >= <= > <
    left: Node
    right: Node


@dataclass(eq=False, slots=True)
class Call(Node):
    name: str
    args: list[Node]
    kwargs: list[tuple[str, Node]]


@dataclass(eq=False, slots=True)
class Answer(Node):
    args: list[Node]
    template: str | None = None
    implicit: bool = False  # a bare expression wrapped in ANSWER(...) by the parser


@dataclass(eq=False, slots=True)
class NotFound(Node):
    message: str
    searched: list[int]


@dataclass(eq=False, slots=True)
class Ambiguous(Node):
    message: str
    handles: list[int]


Formula = Answer | NotFound | Ambiguous
Expr = Num | Str | BoolLit | Ref | CellRef | Neg | BinOp | Compare | Call


def children(node: Node) -> list[Node]:
    """Direct sub-expressions, in source order."""
    if isinstance(node, (Neg,)):
        return [node.operand]
    if isinstance(node, (BinOp, Compare)):
        return [node.left, node.right]
    if isinstance(node, Call):
        return list(node.args) + [v for _, v in node.kwargs]
    if isinstance(node, Answer):
        return list(node.args)
    return []


def walk(node: Node):
    yield node
    for c in children(node):
        yield from walk(c)
