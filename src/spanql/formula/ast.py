"""AST for spanql formulas, plus a canonical unparser."""

from __future__ import annotations

import json
from collections.abc import Iterator
from dataclasses import dataclass, field, replace
from decimal import Decimal


@dataclass(frozen=True)
class Node:
    pos: int = field(default=0, compare=False, kw_only=True)


@dataclass(frozen=True)
class NumberLit(Node):
    value: Decimal
    text: str


@dataclass(frozen=True)
class StringLit(Node):
    value: str


@dataclass(frozen=True)
class BoolLit(Node):
    value: bool


@dataclass(frozen=True)
class HandleRef(Node):
    handle: int
    quote: str | None = None
    occurrence: int | None = None


@dataclass(frozen=True)
class CellRef(Node):
    cell_id: str


@dataclass(frozen=True)
class ArgRef(Node):
    """``[n]``: the value of the n-th (0-based) earlier expression of the same ``ANSWER``,
    so ``PCT([0], [1])`` doesn't repeat two long ``COUNT(FIND(...))`` expressions."""

    index: int


@dataclass(frozen=True)
class Call(Node):
    name: str
    args: tuple[Node, ...] = ()
    kwargs: tuple[tuple[str, Node], ...] = ()

    def kw(self, name: str) -> Node | None:
        for k, v in self.kwargs:
            if k == name:
                return v
        return None


@dataclass(frozen=True)
class BinOp(Node):
    op: str
    left: Node
    right: Node


@dataclass(frozen=True)
class Neg(Node):
    operand: Node


@dataclass(frozen=True)
class Compare(Node):
    op: str
    left: Node
    right: Node


@dataclass(frozen=True)
class Answer(Node):
    exprs: tuple[Node, ...]
    template: str | None = None


@dataclass(frozen=True)
class NotFound(Node):
    reason: str
    searched: tuple[int, ...] = ()


@dataclass(frozen=True)
class Ambiguous(Node):
    reason: str
    handles: tuple[int, ...] = ()


Formula = Answer | NotFound | Ambiguous

_PREC = {"+": 1, "-": 1, "*": 2, "/": 2}


def _q(s: str) -> str:
    return json.dumps(s, ensure_ascii=False)


def unparse(node: Node) -> str:
    """Canonical text for a node; ``parse(unparse(n)) == n`` (ignoring positions)."""
    match node:
        case NumberLit(text=t):
            return t
        case StringLit(value=v):
            return _q(v)
        case BoolLit(value=v):
            return "TRUE" if v else "FALSE"
        case HandleRef(handle=h, quote=q, occurrence=o):
            s = f"${h}"
            if q is not None:
                s += "." + _q(q)
                if o is not None:
                    s += f"#{o}"
            return s
        case CellRef(cell_id=c):
            return f"[{c}]"
        case ArgRef(index=i):
            return f"[{i}]"
        case Call(name=n, args=a, kwargs=k):
            parts = [unparse(x) for x in a] + [f"{kn}:{unparse(kv)}" for kn, kv in k]
            return f"{n}({', '.join(parts)})"
        case Neg(operand=o):
            inner = unparse(o)
            return f"-({inner})" if isinstance(o, BinOp | Neg | Compare) else f"-{inner}"
        case BinOp(op=op, left=left, right=right):
            ls, rs = unparse(left), unparse(right)
            if (isinstance(left, BinOp) and _PREC[left.op] < _PREC[op]) or isinstance(
                left, Compare
            ):
                ls = f"({ls})"
            if (isinstance(right, BinOp) and _PREC[right.op] <= _PREC[op]) or isinstance(
                right, Compare
            ):
                rs = f"({rs})"
            return f"{ls} {op} {rs}"
        case Compare(op=op, left=left, right=right):
            ls, rs = unparse(left), unparse(right)
            if isinstance(left, Compare):
                ls = f"({ls})"
            if isinstance(right, Compare):
                rs = f"({rs})"
            return f"{ls} {op} {rs}"
        case Answer(exprs=e, template=t):
            parts = [unparse(x) for x in e]
            if t is not None:
                parts.append(f"text:{_q(t)}")
            return f"ANSWER({', '.join(parts)})"
        case NotFound(reason=r, searched=s):
            tail = f", searched:[{', '.join(f'${h}' for h in s)}]" if s else ""
            return f"NOT_FOUND({_q(r)}{tail})"
        case Ambiguous(reason=r, handles=h):
            return f"AMBIGUOUS({_q(r)}, [{', '.join(f'${x}' for x in h)}])"
    raise TypeError(f"cannot unparse {node!r}")


def walk(node: Node) -> Iterator[Node]:
    """Yield every node in the tree (pre-order)."""
    yield node
    match node:
        case Call(args=a, kwargs=k):
            for x in a:
                yield from walk(x)
            for _, v in k:
                yield from walk(v)
        case BinOp(left=left, right=right) | Compare(left=left, right=right):
            yield from walk(left)
            yield from walk(right)
        case Neg(operand=o):
            yield from walk(o)
        case Answer(exprs=e):
            for x in e:
                yield from walk(x)


class ArgRefError(ValueError):
    def __init__(self, message: str, pos: int):
        super().__init__(message)
        self.message = message
        self.pos = pos


def _subst(node: Node, done: list[Node], current: int) -> Node:
    match node:
        case ArgRef(index=i):
            if i >= current:
                raise ArgRefError(
                    f"[{i}] in value {current} must refer to an earlier value "
                    f"(use [0]..[{current - 1}])"
                    if current
                    else "[n] can't be used in the first value of ANSWER",
                    node.pos,
                )
            return done[i]
        case Call(args=a, kwargs=k):
            return replace(
                node,
                args=tuple(_subst(x, done, current) for x in a),
                kwargs=tuple((kn, _subst(kv, done, current)) for kn, kv in k),
            )
        case BinOp(left=left, right=right):
            return replace(
                node, left=_subst(left, done, current), right=_subst(right, done, current)
            )
        case Compare(left=left, right=right):
            return replace(
                node, left=_subst(left, done, current), right=_subst(right, done, current)
            )
        case Neg(operand=o):
            return replace(node, operand=_subst(o, done, current))
    return node


def expand_arg_refs(answer: Answer) -> Answer:
    """Replace every ``[n]`` with the expression it names, so each value (and each stored cell)
    is a self-contained formula. Raises :class:`ArgRefError` for forward or self references."""
    if not any(isinstance(n, ArgRef) for n in walk(answer)):
        return answer
    done: list[Node] = []
    for i, e in enumerate(answer.exprs):
        done.append(_subst(e, done, i))
    return replace(answer, exprs=tuple(done))
