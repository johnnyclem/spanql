"""A tiny GBNF subset parser + random sampler, used to check that the generated
grammar only derives strings our parser accepts. Not a full GBNF implementation."""

from __future__ import annotations

import random
import re

PRINTABLE = [chr(c) for c in range(32, 127)]


class Grammar:
    def __init__(self, text: str):
        self.rules: dict[str, list] = {}
        for line in text.strip().splitlines():
            name, _, body = line.partition("::=")
            toks = self.tokenize(body.strip())
            node, pos = self.parse_alt(toks, 0)
            assert pos == len(toks), f"trailing tokens in rule {name!r}: {toks[pos:]}"
            self.rules[name.strip()] = node

    @staticmethod
    def tokenize(s: str) -> list[tuple[str, str]]:
        toks, i = [], 0
        while i < len(s):
            c = s[i]
            if c.isspace():
                i += 1
            elif c == '"':
                j = i + 1
                while s[j] != '"':
                    j += 2 if s[j] == "\\" else 1
                toks.append(("str", s[i + 1 : j]))
                i = j + 1
            elif c == "[":
                j = i + 1
                if s[j] == "^":
                    j += 1
                while s[j] != "]":
                    j += 2 if s[j] == "\\" else 1
                toks.append(("cls", s[i + 1 : j]))
                i = j + 1
            elif c == "{":
                j = s.index("}", i)
                toks.append(("rep", s[i + 1 : j]))
                i = j + 1
            elif c in "()|*+?":
                toks.append((c, c))
                i += 1
            else:
                m = re.match(r"[a-z][a-z0-9\-]*", s[i:])
                assert m, f"bad grammar text at {s[i:i+20]!r}"
                toks.append(("ref", m.group()))
                i += m.end()
        return toks

    def parse_alt(self, toks, pos):
        alts = []
        seq, pos = self.parse_seq(toks, pos)
        alts.append(seq)
        while pos < len(toks) and toks[pos][0] == "|":
            seq, pos = self.parse_seq(toks, pos + 1)
            alts.append(seq)
        return ("alt", alts), pos

    def parse_seq(self, toks, pos):
        items = []
        while pos < len(toks) and toks[pos][0] not in ("|", ")"):
            atom, pos = self.parse_atom(toks, pos)
            while pos < len(toks) and toks[pos][0] in ("*", "+", "?", "rep"):
                kind, val = toks[pos]
                atom = ("rep", atom, kind if kind != "rep" else val)
                pos += 1
            items.append(atom)
        return ("seq", items), pos

    def parse_atom(self, toks, pos):
        kind, val = toks[pos]
        if kind == "(":
            node, pos = self.parse_alt(toks, pos + 1)
            assert toks[pos][0] == ")"
            return node, pos + 1
        return (kind, val), pos + 1

    # ---------------------------------------------------------------- sampling
    def sample(self, rng: random.Random, root: str = "root", max_depth: int = 45) -> str:
        for _ in range(200):
            try:
                return self._gen(("ref", root), rng, 0, max_depth)
            except RecursionError:
                continue
        raise AssertionError("could not sample within depth limit")

    def _gen(self, node, rng, depth, max_depth) -> str:
        # depth counts rule expansions only, so a budget of ~45 allows realistic nesting
        if depth > max_depth:
            raise RecursionError
        kind = node[0]
        if kind == "str":
            return re.sub(r"\\(.)", lambda m: {"n": "\n", "t": "\t"}.get(m.group(1), m.group(1)), node[1])
        if kind == "cls":
            return self._cls(node[1], rng)
        if kind == "ref":
            return self._gen(self.rules[node[1]], rng, depth + 1, max_depth)
        if kind == "alt":
            alts = node[1]
            if len(alts) == 3 and depth <= 2:  # root form: answer | notfound | ambiguous
                alts = [alts[0]] * 6 + alts[1:]
            return self._gen(rng.choice(alts), rng, depth, max_depth)
        if kind == "seq":
            return "".join(self._gen(i, rng, depth, max_depth) for i in node[1])
        if kind == "rep":
            q = node[2]
            if q == "?":
                n = rng.randint(0, 1)
            elif q == "*":
                n = rng.choice([0, 0, 1, 2])
            elif q == "+":
                n = rng.choice([1, 1, 2])
            else:
                lo, _, hi = q.partition(",")
                n = rng.randint(int(lo), int(hi or lo))
            return "".join(self._gen(node[1], rng, depth, max_depth) for _ in range(n))
        raise AssertionError(node)

    @staticmethod
    def _cls(body: str, rng: random.Random) -> str:
        neg = body.startswith("^")
        body = body[1:] if neg else body
        chars: list[str] = []
        i = 0
        while i < len(body):
            if body[i] == "\\":
                esc = body[i + 1]
                if esc == "x":
                    chars.append(chr(int(body[i + 2 : i + 4], 16)))
                    i += 4
                else:
                    chars.append({"n": "\n", "t": "\t"}.get(esc, esc))
                    i += 2
            elif i + 2 < len(body) and body[i + 1] == "-":
                lo, hi = body[i], body[i + 2]
                if lo == "\\":  # \x00-\x1f style handled above; fallthrough safety
                    chars.append(lo)
                    i += 1
                else:
                    chars.extend(chr(c) for c in range(ord(lo), ord(hi) + 1))
                    i += 3
            else:
                chars.append(body[i])
                i += 1
        if neg:
            allowed = [c for c in PRINTABLE if c not in chars]
            return rng.choice(allowed)
        return rng.choice(chars)
