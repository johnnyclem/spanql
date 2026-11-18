"""Lexer and recursive-descent parser for the formula language (ARCHITECTURE.md 2.3).

Precedence, lowest to highest: comparison (non-associative) < ``+ -`` < ``* /`` < unary minus.
Hand-written rather than Lark so parse errors can say exactly what the model got wrong.
"""

from __future__ import annotations

from dataclasses import dataclass
from decimal import Decimal
import re

from ..errors import ParseError
from .ast import (Ambiguous, Answer, BinOp, BoolLit, Call, CellRef, Compare, Formula, Neg, Node, NotFound, Num,
                  Ref, Str)

MAX_FORMULA_CHARS = 20_000
MAX_DEPTH = 60

_TOKEN = re.compile(
    r"""
    (?P<ws>\s+)
  | (?P<number>\d+(?:\.\d+)?)
  | (?P<handle>\$\d+)
  | (?P<ident>[A-Za-z_][A-Za-z0-9_]*)
  | (?P<op>==|!=|>=|<=|[+\-*/<>(),:.\[\]#])
    """,
    re.VERBOSE,
)
_ESCAPES = {'"': '"', "\\": "\\", "n": "\n", "t": "\t", "r": "\r", "/": "/"}
CMP_OPS = ("==", "!=", ">=", "<=", ">", "<")


@dataclass(slots=True)
class Tok:
    kind: str  # number handle ident string op eof
    value: str
    pos: int
    end: int


def _snippet(src: str, pos: int) -> str:
    line = src.count("\n", 0, pos) + 1
    col = pos - (src.rfind("\n", 0, pos) + 1) + 1
    near = src[pos : pos + 24].replace("\n", " ")
    return f"line {line}, column {col}, near `{near}`"


def lex(src: str) -> list[Tok]:
    if len(src) > MAX_FORMULA_CHARS:
        raise ParseError(f"formula is too long ({len(src)} characters; limit {MAX_FORMULA_CHARS})")
    toks: list[Tok] = []
    i, n = 0, len(src)
    while i < n:
        c = src[i]
        if c == '"':
            j, buf = i + 1, []
            while True:
                if j >= n:
                    raise ParseError(f"unterminated string starting at {_snippet(src, i)}", pos=i)
                ch = src[j]
                if ch == '"':
                    break
                if ch == "\n":
                    raise ParseError(f"raw newline in string at {_snippet(src, i)}; write \\n instead", pos=i)
                if ch == "\\":
                    nxt = src[j + 1 : j + 2]
                    if nxt in _ESCAPES:
                        buf.append(_ESCAPES[nxt])
                        j += 2
                        continue
                    if nxt == "u" and re.fullmatch(r"[0-9a-fA-F]{4}", src[j + 2 : j + 6]):
                        buf.append(chr(int(src[j + 2 : j + 6], 16)))
                        j += 6
                        continue
                    raise ParseError(f"bad escape `\\{nxt}` in string at {_snippet(src, j)}", pos=j)
                buf.append(ch)
                j += 1
            toks.append(Tok("string", "".join(buf), i, j + 1))
            i = j + 1
            continue
        m = _TOKEN.match(src, i)
        if not m:
            raise ParseError(f"unexpected character `{c}` at {_snippet(src, i)}", pos=i)
        i = m.end()
        kind = m.lastgroup
        if kind == "ws":
            continue
        toks.append(Tok(kind, m.group(), m.start(), m.end()))
    toks.append(Tok("eof", "", n, n))
    return toks


class _Parser:
    def __init__(self, src: str):
        self.src = src
        self.toks = lex(src)
        self.i = 0
        self.depth = 0

    # ----------------------------------------------------------- token helpers
    @property
    def tok(self) -> Tok:
        return self.toks[self.i]

    def peek(self, k: int = 1) -> Tok:
        return self.toks[min(self.i + k, len(self.toks) - 1)]

    def prev_end(self) -> int:
        return self.toks[self.i - 1].end if self.i else 0

    def fail(self, expected: str) -> ParseError:
        t = self.tok
        got = "end of formula" if t.kind == "eof" else f"`{t.value}`"
        return ParseError(f"expected {expected} but found {got} at {_snippet(self.src, t.pos)}", pos=t.pos)

    def is_op(self, v: str, k: int = 0) -> bool:
        t = self.peek(k) if k else self.tok
        return t.kind == "op" and t.value == v

    def expect_op(self, v: str) -> Tok:
        if not self.is_op(v):
            raise self.fail(f"`{v}`")
        t = self.tok
        self.i += 1
        return t

    def expect(self, kind: str, what: str) -> Tok:
        if self.tok.kind != kind:
            raise self.fail(what)
        t = self.tok
        self.i += 1
        return t

    # ------------------------------------------------------------- top level
    def parse(self) -> Formula:
        t = self.tok
        if t.kind == "ident" and self.is_op("(", 1) and t.value in ("ANSWER", "NOT_FOUND", "AMBIGUOUS"):
            f = {"ANSWER": self.answer, "NOT_FOUND": self.not_found, "AMBIGUOUS": self.ambiguous}[t.value]()
        else:  # a bare expression is an implicit one-cell answer
            e = self.expr()
            f = Answer(e.pos, e.end, [e], None, implicit=True)
        if self.tok.kind != "eof":
            raise self.fail("end of formula")
        return f

    def answer(self) -> Answer:
        start = self.tok.pos
        self.i += 2  # ANSWER (
        args: list[Node] = []
        template: str | None = None
        if self.is_op(")"):
            raise ParseError("ANSWER needs at least one expression", pos=self.tok.pos)
        while True:
            if self.tok.kind == "ident" and self.is_op(":", 1):
                name = self.tok.value
                if name != "text":
                    raise ParseError(f"ANSWER only accepts the keyword `text:`, not `{name}:`", pos=self.tok.pos)
                if template is not None:
                    raise ParseError("ANSWER has more than one `text:` template", pos=self.tok.pos)
                self.i += 2
                template = self.expect("string", "a quoted template string after `text:`").value
            else:
                if template is not None:
                    raise ParseError("`text:` must come last in ANSWER", pos=self.tok.pos)
                args.append(self.expr())
            if self.is_op(","):
                self.i += 1
                continue
            break
        self.expect_op(")")
        return Answer(start, self.prev_end(), args, template)

    def handle_list(self) -> list[int]:
        self.expect_op("[")
        out = []
        while True:
            t = self.expect("handle", "a handle like $1")
            out.append(int(t.value[1:]))
            if self.is_op(","):
                self.i += 1
                continue
            break
        self.expect_op("]")
        return out

    def not_found(self) -> NotFound:
        start = self.tok.pos
        self.i += 2
        msg = self.expect("string", "a quoted explanation").value
        searched: list[int] = []
        if self.is_op(","):
            self.i += 1
            if not (self.tok.kind == "ident" and self.tok.value == "searched" and self.is_op(":", 1)):
                raise self.fail("`searched:` followed by a handle list")
            self.i += 2
            searched = self.handle_list()
        self.expect_op(")")
        return NotFound(start, self.prev_end(), msg, searched)

    def ambiguous(self) -> Ambiguous:
        start = self.tok.pos
        self.i += 2
        msg = self.expect("string", "a quoted explanation").value
        self.expect_op(",")
        if self.tok.kind == "ident" and self.tok.value == "handles" and self.is_op(":", 1):
            self.i += 2
        handles = self.handle_list()
        self.expect_op(")")
        return Ambiguous(start, self.prev_end(), msg, handles)

    # ------------------------------------------------------------ expressions
    def expr(self) -> Node:
        self.depth += 1
        if self.depth > MAX_DEPTH:
            raise ParseError("formula is nested too deeply", pos=self.tok.pos)
        try:
            left = self.additive()
            if self.tok.kind == "op" and self.tok.value in CMP_OPS:
                op = self.tok.value
                self.i += 1
                right = self.additive()
                if self.tok.kind == "op" and self.tok.value in CMP_OPS:
                    raise ParseError(
                        f"chained comparison at {_snippet(self.src, self.tok.pos)}; combine with AND(...)",
                        pos=self.tok.pos,
                    )
                return Compare(left.pos, right.end, op, left, right)
            return left
        finally:
            self.depth -= 1

    def additive(self) -> Node:
        left = self.term()
        while self.tok.kind == "op" and self.tok.value in ("+", "-"):
            op = self.tok.value
            self.i += 1
            right = self.term()
            left = BinOp(left.pos, right.end, op, left, right)
        return left

    def term(self) -> Node:
        left = self.factor()
        while self.tok.kind == "op" and self.tok.value in ("*", "/"):
            op = self.tok.value
            self.i += 1
            right = self.factor()
            left = BinOp(left.pos, right.end, op, left, right)
        return left

    def factor(self) -> Node:
        if self.is_op("-"):
            start = self.tok.pos
            self.i += 1
            operand = self.factor()
            return Neg(start, operand.end, operand)
        return self.primary()

    def primary(self) -> Node:
        t = self.tok
        if t.kind == "number":
            self.i += 1
            return Num(t.pos, t.end, Decimal(t.value), t.value)
        if t.kind == "string":
            self.i += 1
            return Str(t.pos, t.end, t.value)
        if t.kind == "handle":
            self.i += 1
            n = int(t.value[1:])
            if self.is_op("."):
                self.i += 1
                q = self.expect("string", 'a quoted substring after `$n.` (e.g. $1."4.82 million")')
                occ = None
                if self.is_op("#"):
                    self.i += 1
                    occ_tok = self.expect("number", "an occurrence number after `#`")
                    if "." in occ_tok.value or int(occ_tok.value) < 1:
                        raise ParseError("occurrence after `#` must be a whole number >= 1", pos=occ_tok.pos)
                    occ = int(occ_tok.value)
                return Ref(t.pos, self.prev_end(), n, q.value, occ)
            return Ref(t.pos, t.end, n)
        if t.kind == "ident":
            if self.is_op("(", 1):
                return self.call()
            if t.value in ("true", "false"):
                self.i += 1
                return BoolLit(t.pos, t.end, t.value == "true")
            raise ParseError(
                f"`{t.value}` is not a function call or value at {_snippet(self.src, t.pos)}; "
                "function names are followed by `(`; text values must be quoted", pos=t.pos)
        if self.is_op("("):
            self.i += 1
            e = self.expr()
            self.expect_op(")")
            return e
        if self.is_op("["):
            start = t.pos
            self.i += 1
            if self.tok.kind == "number" and "." not in self.tok.value:
                key: int | str = int(self.tok.value)
            elif self.tok.kind == "ident":
                key = self.tok.value
            else:
                raise self.fail("a cell reference like [0] or [c_7Hq2]")
            self.i += 1
            self.expect_op("]")
            return CellRef(start, self.prev_end(), key)
        raise self.fail("a value, function call, `$n` reference or `(`")

    def call(self) -> Call:
        name_tok = self.tok
        self.i += 2
        args: list[Node] = []
        kwargs: list[tuple[str, Node]] = []
        if not self.is_op(")"):
            while True:
                if self.tok.kind == "ident" and self.is_op(":", 1):
                    kw = self.tok.value
                    if any(k == kw for k, _ in kwargs):
                        raise ParseError(f"keyword `{kw}:` given twice in {name_tok.value}(...)", pos=self.tok.pos)
                    self.i += 2
                    kwargs.append((kw, self.expr()))
                else:
                    args.append(self.expr())
                if self.is_op(","):
                    self.i += 1
                    continue
                break
        self.expect_op(")")
        return Call(name_tok.pos, self.prev_end(), name_tok.value, args, kwargs)


def parse_formula(src: str) -> Formula:
    """Parse a full formula: ANSWER(...), NOT_FOUND(...), AMBIGUOUS(...) or a bare expression."""
    if not src or not src.strip():
        raise ParseError("empty formula")
    return _Parser(src).parse()
