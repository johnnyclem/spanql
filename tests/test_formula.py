from __future__ import annotations

from decimal import Decimal

import pytest
from hypothesis import given, settings
from hypothesis import strategies as st

from spanql.formula import ast as A
from spanql.formula.gbnf import build_gbnf
from spanql.formula.parser import FormulaSyntaxError, parse, parse_expr

from .gbnf_matcher import Grammar

GOOD = [
    'ANSWER(MONEY($1."$4.82 million"), text:"Q3 revenue was {0}.")',
    'ANSWER(DIFF(MONEY($3."$4.82 million"), MONEY($1."$4.31M")), PCT(DIFF(MONEY($3."$4.82 million"), MONEY($1."$4.31M")), MONEY($1."$4.31M"), places:1), text:"Revenue grew by {0} ({1}).")',
    'ANSWER(COUNT(FIND(src:"logs/api.log", day:"2026-11-16", FIELD("status") >= 500)), text:"{0} 5xx.")',
    'ANSWER(SUM(COLUMN(src:"d/x.csv", "amount", FIELD("region") == "West")))',
    'ANSWER(DISTINCT(SYMBOLS(kind:"callable", calls:"flush")), text:"Callers: {0}.")',
    'ANSWER(COUNT(FIND(re:"timeout\\\\s+\\\\d+", level:"ERROR")))',
    'ANSWER(IF(GT(NUM($2."12"), NUM($1."10")), "up", "down"))',
    'ANSWER(NUM($1."5"#2) + -NUM($2."3") * (NUM($1."2") - NUM($1."1")) / NUM($2."4"))',
    'ANSWER([c_abc123] , ROUND(RATIO(NUM($1."7"), NUM($2."9")), places:2))',
    'NOT_FOUND("No churn figure.", searched:[$1, $2])',
    'NOT_FOUND("nothing")',
    'AMBIGUOUS("two different dates", [$1, $2])',
    "ANSWER(AND(TRUE, NOT(FALSE)))",
]


@pytest.mark.parametrize("src", GOOD)
def test_parse_unparse_roundtrip(src: str) -> None:
    f = parse(src)
    again = parse(A.unparse(f))
    assert again == f
    assert A.unparse(again) == A.unparse(f)


def test_parse_details() -> None:
    f = parse('ANSWER(MONEY($3."a \\"b\\""#2), text:"x {0}")')
    assert isinstance(f, A.Answer) and f.template == "x {0}"
    call = f.exprs[0]
    assert isinstance(call, A.Call) and call.name == "MONEY"
    ref = call.args[0]
    assert ref == A.HandleRef(3, 'a "b"', 2)


def test_precedence() -> None:
    e = parse_expr("1 + 2 * 3 - 4")
    assert A.unparse(e) in ("1 + 2 * 3 - 4", "(1 + (2 * 3)) - 4")
    assert isinstance(e, A.BinOp) and e.op == "-"


def test_code_fence_tolerated() -> None:
    f = parse('```\nNOT_FOUND("x")\n```')
    assert isinstance(f, A.NotFound)


@pytest.mark.parametrize(
    "bad",
    [
        "",
        "ANSWER(",
        "SUM(1)",  # not a top-level form
        'ANSWER(MONEY($1."x")) trailing',
        "ANSWER(FOO($1))",
        'ANSWER($1."unterminated)',
        'NOT_FOUND("x", searched:[1])',
    ],
)
def test_syntax_errors(bad: str) -> None:
    with pytest.raises(FormulaSyntaxError):
        parse(bad)


def test_walk_visits_all() -> None:
    f = parse(GOOD[1])
    names = [n.name for n in A.walk(f) if isinstance(n, A.Call)]
    assert names.count("MONEY") == 5


# ---------------------------------------------------------------- property tests
idents = st.sampled_from(["NUM", "MONEY", "SUM", "DIFF", "PCT", "COUNT", "TEXT"])
strings = st.text(alphabet=st.characters(min_codepoint=32, max_codepoint=126), max_size=8)
leaf = st.one_of(
    st.decimals(min_value=0, max_value=10**6, places=2, allow_nan=False).map(
        lambda d: A.NumberLit(Decimal(str(d)), str(d))
    ),
    strings.map(A.StringLit),
    st.booleans().map(A.BoolLit),
    st.builds(
        A.HandleRef, st.integers(1, 30), st.one_of(st.none(), strings.filter(bool)), st.none()
    ),
)


def _expr(children):
    return st.one_of(
        st.builds(lambda n, a: A.Call(n, tuple(a)), idents, st.lists(children, max_size=3)),
        st.builds(A.BinOp, st.sampled_from(["+", "-", "*", "/"]), children, children),
    )


exprs = st.recursive(leaf, _expr, max_leaves=10)


@settings(max_examples=200, deadline=None)
@given(st.lists(exprs, min_size=1, max_size=3), st.one_of(st.none(), strings))
def test_roundtrip_property(es: list[A.Node], tpl: str | None) -> None:
    f = A.Answer(tuple(es), tpl)
    text = A.unparse(f)
    assert A.unparse(parse(text)) == text


# ---------------------------------------------------------------- grammar
@pytest.fixture(scope="module")
def grammar5() -> Grammar:
    return Grammar(build_gbnf(range(1, 6)))


@pytest.mark.parametrize("src", [g for g in GOOD if "$9" not in g])
def test_grammar_accepts_valid_formulas(grammar5: Grammar, src: str) -> None:
    assert grammar5.accepts(src), src


@pytest.mark.parametrize(
    "src",
    [
        'ANSWER(MONEY($7."x"))',  # handle not retrieved
        'ANSWER(MONEY($0."x"))',
        "Revenue was $4.82 million.",  # prose
        'ANSWER(BOGUS($1."x"))',  # unknown function
        'ANSWER(FIND(nope:"x"))',  # unknown kwarg
        'NOT_FOUND("x", searched:[$6])',
    ],
)
def test_grammar_rejects(grammar5: Grammar, src: str) -> None:
    assert not grammar5.accepts(src)


def test_grammar_without_candidates() -> None:
    g = Grammar(build_gbnf([]))
    assert g.accepts('NOT_FOUND("nothing matched")')
    assert not g.accepts('ANSWER(MONEY($1."x"))')
    assert g.accepts('ANSWER(COUNT(FIND(match:"x")))')


@settings(max_examples=100, deadline=None)
@given(st.lists(exprs, min_size=1, max_size=2))
def test_grammar_accepts_unparsed_asts(es: list[A.Node]) -> None:
    es = [e for e in es]
    text = A.unparse(A.Answer(tuple(es), None))
    g = Grammar(build_gbnf(range(1, 31)))
    assert g.accepts(text), text
