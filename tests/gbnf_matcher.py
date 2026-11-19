"""A tiny GBNF interpreter for tests: parses the subset spanql emits and checks whether a
string is in the language (backtracking over all match ends). Not a general implementation."""

from __future__ import annotations

import re
from collections.abc import Iterator
from dataclasses import dataclass
from functools import cache


@dataclass(frozen=True)
class Lit:
    s: str


@dataclass(frozen=True)
class Cls:
    rx: str


@dataclass(frozen=True)
class Ref:
    name: str


@dataclass(frozen=True)
class Seq:
    items: tuple


@dataclass(frozen=True)
class Alt:
    opts: tuple


@dataclass(frozen=True)
class Rep:
    item: object
    lo: int
    hi: int | None


_ESC = {"n": "\n", "t": "\t", "r": "\r", '"': '"', "\\": "\\"}


class _P:
    def __init__(self, s: str):
        self.s, self.i = s, 0

    def ws(self) -> None:
        while self.i < len(self.s) and self.s[self.i] in " \t":
            self.i += 1

    def alt(self):
        opts = [self.seq()]
        self.ws()
        while self.i < len(self.s) and self.s[self.i] == "|":
            self.i += 1
            opts.append(self.seq())
            self.ws()
        return opts[0] if len(opts) == 1 else Alt(tuple(opts))

    def seq(self):
        items = []
        while True:
            self.ws()
            if self.i >= len(self.s) or self.s[self.i] in "|)":
                break
            items.append(self.postfix(self.atom()))
        return items[0] if len(items) == 1 else Seq(tuple(items))

    def postfix(self, a):
        if self.i < len(self.s) and self.s[self.i] in "?*+":
            c = self.s[self.i]
            self.i += 1
            return Rep(a, 0 if c != "+" else 1, 1 if c == "?" else None)
        return a

    def atom(self):
        c = self.s[self.i]
        if c == '"':
            self.i += 1
            out = []
            while self.s[self.i] != '"':
                if self.s[self.i] == "\\":
                    self.i += 1
                    out.append(_ESC.get(self.s[self.i], self.s[self.i]))
                else:
                    out.append(self.s[self.i])
                self.i += 1
            self.i += 1
            return Lit("".join(out))
        if c == "[":
            j = self.i + 1
            while self.s[j] != "]" or (self.s[j - 1] == "\\" and self.s[j - 2] != "\\"):
                j += 1
            body = self.s[self.i + 1 : j]
            self.i = j + 1
            body = re.sub(r"\\x([0-9A-Fa-f]{2})", lambda m: "\\x" + m.group(1), body)
            return Cls("[" + body + "]")
        if c == "(":
            self.i += 1
            a = self.alt()
            self.ws()
            assert self.s[self.i] == ")", self.s[self.i :]
            self.i += 1
            return a
        m = re.compile(r"[a-zA-Z_][\w-]*").match(self.s, self.i)
        assert m, self.s[self.i :]
        self.i = m.end()
        return Ref(m.group(0))


def parse_grammar(text: str) -> dict[str, object]:
    rules: dict[str, object] = {}
    for line in text.splitlines():
        if not line.strip():
            continue
        name, _, body = line.partition("::=")
        rules[name.strip()] = _P(body.strip()).alt()
    return rules


class Grammar:
    def __init__(self, text: str):
        self.rules = parse_grammar(text)

    def accepts(self, s: str, start: str = "root") -> bool:
        @cache
        def ends(node, i: int) -> frozenset[int]:
            return frozenset(self._m(node, i, s, ends))

        return len(s) in ends(Ref(start), 0)

    def _m(self, node, i: int, s: str, ends) -> Iterator[int]:
        if isinstance(node, Lit):
            if s.startswith(node.s, i):
                yield i + len(node.s)
        elif isinstance(node, Cls):
            if i < len(s) and re.fullmatch(node.rx, s[i]):
                yield i + 1
        elif isinstance(node, Ref):
            yield from ends(self.rules[node.name], i)
        elif isinstance(node, Seq):
            cur = {i}
            for it in node.items:
                cur = {e for c in cur for e in ends(it, c)}
                if not cur:
                    return
            yield from cur
        elif isinstance(node, Alt):
            for o in node.opts:
                yield from ends(o, i)
        elif isinstance(node, Rep):
            seen, frontier, n = {i} if node.lo == 0 else set(), {i}, 0
            while frontier and (node.hi is None or n < node.hi):
                n += 1
                nxt = {e for c in frontier for e in ends(node.item, c)} - {*frontier}
                if n >= node.lo:
                    seen |= nxt
                frontier = nxt
            yield from seen
