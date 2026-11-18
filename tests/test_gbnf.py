from __future__ import annotations

import random
import re

import pytest

from gbnf_sampler import Grammar
from spanql.errors import ParseError
from spanql.formula.ast import Ambiguous, Answer, NotFound, Ref, walk
from spanql.formula.functions import FUNCTIONS
from spanql.formula.gbnf import build_gbnf
from spanql.formula.parser import parse_formula


def parse_or_known_limit(s: str):
    """Parse a sampled string. The one tolerated failure is a repeated keyword, which a CFG cannot rule out."""
    try:
        return parse_formula(s)
    except ParseError as e:
        if "given twice" in e.message:
            return None
        raise


def referenced_rules(text: str) -> tuple[set[str], set[str]]:
    defined, used = set(), set()
    for line in text.strip().splitlines():
        name, _, body = line.partition("::=")
        defined.add(name.strip())
        body = re.sub(r'"(?:[^"\\]|\\.)*"', "", body)  # drop string literals
        body = re.sub(r"\[(?:[^\]\\]|\\.)*\]", "", body)  # drop character classes
        used |= set(re.findall(r"[a-z][a-z0-9\-]*", body))
    return defined, used


@pytest.mark.parametrize("k", [0, 1, 9, 12, 40])
def test_grammar_is_closed_and_has_root(k):
    text = build_gbnf(k)
    defined, used = referenced_rules(text)
    assert "root" in defined and used <= defined, used - defined


def test_grammar_only_offers_the_requested_handles():
    text = build_gbnf(3)
    handle = next(l for l in text.splitlines() if l.startswith("handle ::="))
    assert '"1" | "2" | "3"' in handle and '"4"' not in handle
    no_handles = build_gbnf(0)
    assert "answer" not in no_handles and "handle" not in no_handles
    assert all(f in text for f in FUNCTIONS)


@pytest.mark.parametrize("k", [0, 3, 12])
def test_every_sampled_derivation_parses(k):
    g = Grammar(build_gbnf(k))
    rng = random.Random(1234 + k)
    kinds = set()
    for _ in range(400):
        s = g.sample(rng)
        f = parse_or_known_limit(s)  # any other ParseError is a grammar/parser mismatch
        if f is None:
            continue
        kinds.add(type(f).__name__)
        if isinstance(f, Answer):
            for a in f.args:
                for n in walk(a):
                    if isinstance(n, Ref):
                        assert 1 <= n.handle <= k  # only offered handles are derivable
        if isinstance(f, (NotFound, Ambiguous)):
            assert all(1 <= h <= k for h in (f.searched if isinstance(f, NotFound) else f.handles))
    assert kinds == ({"NotFound"} if k == 0 else {"Answer", "NotFound", "Ambiguous"})


def test_sampler_actually_exercises_the_grammar():
    """Guard against a vacuous pass: samples must include nested calls, refs with quotes, kwargs and operators."""
    g = Grammar(build_gbnf(6))
    rng = random.Random(99)
    samples = [g.sample(rng) for _ in range(600)]
    answers = [s for s in samples if s.lstrip().startswith("ANSWER")]
    assert len(answers) > 250
    joined = "\n".join(answers)
    assert len(joined) / len(answers) > 80  # not just trivial one-token answers
    assert re.search(r'\$[1-6]\.\"', joined)  # quoted sub-span
    assert re.search(r"\b[a-z]+\s*:", joined)  # keyword argument
    assert re.search(r"[+*/]|==|>=|<=", joined)  # operators
    assert max(len(re.findall(r"[A-Z_]+\s*\(", s)) for s in answers) >= 3  # nested calls
    parsed = [parse_or_known_limit(s) for s in answers]
    assert sum(p is not None for p in parsed) > 0.8 * len(answers)  # the limit is rare, not the norm


def test_invalid_handle_counts_are_rejected():
    for bad in (-1, 10_000):
        with pytest.raises(ValueError):
            build_gbnf(bad)
