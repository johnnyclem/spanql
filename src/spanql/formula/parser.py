"""Hand-written recursive-descent parser for spanql formulas.

Grammar (EBNF; see docs/ARCHITECTURE.md §2.3, plus comparisons which FIND predicates need)::

    formula  = answer | not_found | ambiguous ;
    answer   = "ANSWER" "(" cmp { "," cmp } [ "," "text" ":" string ] ")" ;
    not_found= "NOT_FOUND" "(" string [ "," "searched" ":" hlist ] ")" ;
    ambiguous= "AMBIGUOUS" "(" string "," hlist ")" ;
    cmp      = expr [ ("=="|"!="|">="|"<="|">"|"<") expr ] ;
    expr     = term { ("+"|"-") term } ;
    term     = factor { ("*"|"/") factor } ;
    factor   = [ "-" ] primary ;
    primary  = call | ref | number | string | bool | cellref | "(" cmp ")" ;
    ref      = "$" int [ "." string [ "#" int ] ] ;
    call     = FNAME "(" [ arg { "," arg } ] ")" ;
    arg      = ident ":" cmp | cmp ;
    hlist    = "[" "$" int { "," "$" int } "]" ;
    cellref  = "[" ident "]" | "[" int "]" ;   (* stored cell | earlier ANSWER value *)
"""

from __future__ import annotations

from dataclasses import dataclass
from decimal import Decimal

from spanql.formula.ast import (
    Ambiguous,
    Answer,
    ArgRef,
    BinOp,
    BoolLit,
    Call,
    CellRef,
    Compare,
    Formula,
    HandleRef,
    Neg,
    Node,
    NotFound,
    NumberLit,
    StringLit,
)
from spanql.formula.spec import CMP_OPS, FUNCTIONS

MAX_FORMULA_CHARS = 8000
MAX_DEPTH = 64


class FormulaSyntaxError(ValueError):
    def __init__(self, message: str, pos: int, source: str = ""):
        self.message = message
        self.pos = pos
        self.source = source
        super().__init__(f"{message} at position {pos}" + (f": {self.context()}" if source else ""))

    def context(self) -> str:
        a = max(0, self.pos - 20)
        return f"…{self.source[a : self.pos]}⟨here⟩{self.source[self.pos : self.pos + 20]}…"


@dataclass
class Tok:
    kind: str  # num str ident handle op eof
    text: str
    pos: int
    value: object = None


_ESC = {'"': '"', "\\": "\\", "/": "/", "n": "\n", "t": "\t", "r": "\r", "b": "\b", "f": "\f"}
_PUNCT2 = ("==", "!=", ">=", "<=")
_PUNCT1 = "(),.#:[]+-*/<>"


def tokenize(src: str) -> list[Tok]:
    if len(src) > MAX_FORMULA_CHARS:
        raise FormulaSyntaxError(f"formula longer than {MAX_FORMULA_CHARS} characters", 0)
    toks: list[Tok] = []
    i, n = 0, len(src)
    while i < n:
        c = src[i]
        if c.isspace():
            i += 1
            continue
        if c == '"':
            j = i + 1
            buf: list[str] = []
            while True:
                if j >= n:
                    raise FormulaSyntaxError("unterminated string", i, src)
                ch = src[j]
                if ch == '"':
                    break
                if ch == "\\":
                    if j + 1 >= n:
                        raise FormulaSyntaxError("unterminated escape", j, src)
                    nx = src[j + 1]
                    if nx == "u" and j + 5 < n:
                        try:
                            buf.append(chr(int(src[j + 2 : j + 6], 16)))
                            j += 6
                            continue
                        except ValueError:
                            pass
                    if nx in _ESC:
                        # "\b" stays a regex word boundary: keep it literal for regexes
                        buf.append("\\b" if nx == "b" else _ESC[nx])
                    else:
                        buf.append("\\" + nx)  # lenient: "\d" means a regex escape
                    j += 2
                    continue
                buf.append(ch)
                j += 1
            toks.append(Tok("str", src[i : j + 1], i, "".join(buf)))
            i = j + 1
            continue
        if c.isdigit():
            j = i
            while j < n and src[j].isdigit():
                j += 1
            if j + 1 < n and src[j] == "." and src[j + 1].isdigit():
                j += 1
                while j < n and src[j].isdigit():
                    j += 1
            toks.append(Tok("num", src[i:j], i, Decimal(src[i:j])))
            i = j
            continue
        if c == "$":
            j = i + 1
            while j < n and src[j].isdigit():
                j += 1
            if j == i + 1:
                raise FormulaSyntaxError("expected a handle number after '$'", i, src)
            toks.append(Tok("handle", src[i:j], i, int(src[i + 1 : j])))
            i = j
            continue
        if c.isalpha() or c == "_":
            j = i
            while j < n and (src[j].isalnum() or src[j] == "_"):
                j += 1
            toks.append(Tok("ident", src[i:j], i))
            i = j
            continue
        two = src[i : i + 2]
        if two in _PUNCT2:
            toks.append(Tok("op", two, i))
            i += 2
            continue
        if c in _PUNCT1:
            toks.append(Tok("op", c, i))
            i += 1
            continue
        if c == "=":
            raise FormulaSyntaxError("use '==' for equality", i, src)
        raise FormulaSyntaxError(f"unexpected character {c!r}", i, src)
    toks.append(Tok("eof", "", n))
    return toks


class Parser:
    def __init__(self, src: str):
        self.src = src
        self.toks = tokenize(src)
        self.i = 0
        self.depth = 0

    # helpers
    @property
    def cur(self) -> Tok:
        return self.toks[self.i]

    def peek(self, k: int = 1) -> Tok:
        return self.toks[min(self.i + k, len(self.toks) - 1)]

    def err(self, msg: str, tok: Tok | None = None) -> FormulaSyntaxError:
        t = tok or self.cur
        return FormulaSyntaxError(msg, t.pos, self.src)

    def eat(self, kind: str, text: str | None = None) -> Tok:
        t = self.cur
        if t.kind != kind or (text is not None and t.text != text):
            want = repr(text) if text else kind
            got = repr(t.text) if t.kind != "eof" else "end of formula"
            raise self.err(f"expected {want}, got {got}")
        self.i += 1
        return t

    def at(self, kind: str, text: str | None = None) -> bool:
        t = self.cur
        return t.kind == kind and (text is None or t.text == text)

    # grammar
    def formula(self) -> Formula:
        t = self.cur
        if not self.at("ident") or t.text not in ("ANSWER", "NOT_FOUND", "AMBIGUOUS"):
            raise self.err("a formula must start with ANSWER(, NOT_FOUND( or AMBIGUOUS(")
        self.i += 1
        self.eat("op", "(")
        result: Formula
        if t.text == "ANSWER":
            exprs: list[Node] = []
            template: str | None = None
            while True:
                if self.at("ident", "text") and self.peek().text == ":":
                    self.i += 2
                    template = str(self.eat("str").value)
                    break
                exprs.append(self.cmp())
                if not self.at("op", ","):
                    break
                self.i += 1
            if not exprs:
                raise self.err("ANSWER needs at least one expression")
            result = Answer(tuple(exprs), template, pos=t.pos)
        elif t.text == "NOT_FOUND":
            reason = str(self.eat("str").value)
            searched: tuple[int, ...] = ()
            if self.at("op", ","):
                self.i += 1
                kw = self.eat("ident")
                if kw.text != "searched":
                    raise self.err("expected 'searched:'", kw)
                self.eat("op", ":")
                searched = self.hlist()
            result = NotFound(reason, searched, pos=t.pos)
        else:
            reason = str(self.eat("str").value)
            self.eat("op", ",")
            result = Ambiguous(reason, self.hlist(), pos=t.pos)
        self.eat("op", ")")
        if not self.at("eof"):
            raise self.err("unexpected text after the formula")
        return result

    def hlist(self) -> tuple[int, ...]:
        self.eat("op", "[")
        hs = [int(self.eat("handle").value)]  # type: ignore[call-overload]
        while self.at("op", ","):
            self.i += 1
            hs.append(int(self.eat("handle").value))  # type: ignore[call-overload]
        self.eat("op", "]")
        return tuple(hs)

    def cmp(self) -> Node:
        left = self.expr()
        if self.cur.kind == "op" and self.cur.text in CMP_OPS:
            op = self.cur
            self.i += 1
            right = self.expr()
            return Compare(op.text, left, right, pos=op.pos)
        return left

    def expr(self) -> Node:
        node = self.term()
        while self.cur.kind == "op" and self.cur.text in "+-" and self.cur.text:
            op = self.cur
            self.i += 1
            node = BinOp(op.text, node, self.term(), pos=op.pos)
        return node

    def term(self) -> Node:
        node = self.factor()
        while self.cur.kind == "op" and self.cur.text in ("*", "/"):
            op = self.cur
            self.i += 1
            node = BinOp(op.text, node, self.factor(), pos=op.pos)
        return node

    def factor(self) -> Node:
        if self.at("op", "-"):
            t = self.cur
            self.i += 1
            inner = self.primary()
            if isinstance(inner, NumberLit):
                return NumberLit(-inner.value, "-" + inner.text, pos=t.pos)
            return Neg(inner, pos=t.pos)
        return self.primary()

    def primary(self) -> Node:
        self.depth += 1
        if self.depth > MAX_DEPTH:
            raise self.err("formula nested too deeply")
        try:
            return self._primary()
        finally:
            self.depth -= 1

    def _primary(self) -> Node:
        t = self.cur
        if t.kind == "num":
            self.i += 1
            return NumberLit(t.value, t.text, pos=t.pos)  # type: ignore[arg-type]
        if t.kind == "str":
            self.i += 1
            return StringLit(str(t.value), pos=t.pos)
        if t.kind == "handle":
            self.i += 1
            quote = occ = None
            if self.at("op", "."):
                self.i += 1
                quote = str(self.eat("str").value)
                if quote == "":
                    raise self.err("empty quote")
                if self.at("op", "#"):
                    self.i += 1
                    occ = int(str(self.eat("num").text))
                    if occ < 1:
                        raise self.err("occurrence must be 1 or more")
            return HandleRef(int(t.value), quote, occ, pos=t.pos)  # type: ignore[call-overload]
        if t.kind == "op" and t.text == "(":
            self.i += 1
            node = self.cmp()
            self.eat("op", ")")
            return node
        if t.kind == "op" and t.text == "[":
            self.i += 1
            if self.cur.kind == "num":
                num = self.eat("num")
                if not str(num.text).isdigit():
                    raise self.err("[n] needs a whole number, e.g. [0]")
                self.eat("op", "]")
                return ArgRef(int(str(num.text)), pos=t.pos)
            ident = self.eat("ident")
            self.eat("op", "]")
            return CellRef(ident.text, pos=t.pos)
        if t.kind == "ident":
            if t.text in ("TRUE", "FALSE"):
                self.i += 1
                return BoolLit(t.text == "TRUE", pos=t.pos)
            if self.peek().text == "(":
                if t.text not in FUNCTIONS:
                    raise self.err(f"unknown function {t.text}")
                return self.call()
            raise self.err(f"unexpected name {t.text!r}")
        got = repr(t.text) if t.kind != "eof" else "end of formula"
        raise self.err(f"expected a value, got {got}")

    def call(self) -> Call:
        name = self.eat("ident")
        self.eat("op", "(")
        args: list[Node] = []
        kwargs: list[tuple[str, Node]] = []
        seen: set[str] = set()
        if not self.at("op", ")"):
            while True:
                if self.at("ident") and self.peek().text == ":":
                    k = self.cur
                    self.i += 2
                    if k.text in seen:
                        raise self.err(f"duplicate keyword {k.text}", k)
                    seen.add(k.text)
                    kwargs.append((k.text, self.cmp()))
                else:
                    if kwargs and name.text not in ("FIND", "COLUMN", "SYMBOLS", "LINES"):
                        raise self.err("positional argument after keyword argument")
                    args.append(self.cmp())
                if not self.at("op", ","):
                    break
                self.i += 1
        self.eat("op", ")")
        return Call(name.text, tuple(args), tuple(kwargs), pos=name.pos)


def parse(src: str) -> Formula:
    """Parse a formula. Leading/trailing whitespace and a ```code fence``` are tolerated."""
    s = src.strip()
    if s.startswith("```"):
        s = s.strip("`").strip()
        if s.lower().startswith(("spanql", "text")):
            s = s.split("\n", 1)[1] if "\n" in s else ""
    return Parser(s).formula()


def parse_expr(src: str) -> Node:
    p = Parser(src)
    node = p.cmp()
    if not p.at("eof"):
        raise p.err("unexpected text after expression")
    return node
