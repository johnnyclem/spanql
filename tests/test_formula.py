from __future__ import annotations

from decimal import Decimal

import pytest

from spanql.errors import FormulaTypeError, ParseError
from spanql.formula.ast import Ambiguous, Answer, BinOp, Call, CellRef, Compare, Neg, NotFound, Num, Ref, Str
from spanql.formula.parser import lex, parse_formula
from spanql.formula.typecheck import typecheck


def types(src, **kw):
    f = parse_formula(src)
    return typecheck(f, **kw)


# ------------------------------------------------------------------- parsing


def test_spec_examples_parse():
    f = parse_formula(
        """ANSWER(
          DIFF(MONEY($1."$4.82 million"), MONEY($2."$4.31M")),
          PCT(DIFF(MONEY($1."$4.82 million"), MONEY($2."$4.31M")), MONEY($2."$4.31M"), places:1),
          text:"Revenue grew by {0} ({1}) from Q2 to Q3."
        )"""
    )
    assert isinstance(f, Answer) and len(f.args) == 2 and f.template.startswith("Revenue")
    pct = f.args[1]
    assert isinstance(pct, Call) and pct.name == "PCT" and pct.kwargs[0][0] == "places"
    f = parse_formula('ANSWER(COUNT(FIND(src:"logs/api/*.log", kind:"log", day:"2026-11-16", FIELD("status") >= 500)), text:"x {0}")')
    find = f.args[0].args[0]
    assert find.name == "FIND" and isinstance(find.args[0], Compare) and find.args[0].op == ">="


def test_refusal_forms():
    f = parse_formula('NOT_FOUND("No churn figure.", searched:[$1,$2,$3])')
    assert isinstance(f, NotFound) and f.searched == [1, 2, 3]
    f = parse_formula('AMBIGUOUS("Two timeouts.", [$4, $7])')
    assert isinstance(f, Ambiguous) and f.handles == [4, 7]
    assert parse_formula('NOT_FOUND("none")').searched == []


def test_refs_quotes_occurrence_and_cellrefs():
    f = parse_formula('ANSWER(NUM($3."a \\"b\\" c"#2), [0], [c_7Hq2])')
    ref = f.args[0].args[0]
    assert isinstance(ref, Ref) and (ref.handle, ref.quote, ref.occurrence) == (3, 'a "b" c', 2)
    assert isinstance(f.args[1], CellRef) and f.args[1].key == 0 and f.args[2].key == "c_7Hq2"


def test_precedence_and_unary():
    f = parse_formula("1 + 2 * 3 - -4")
    e = f.args[0]
    assert isinstance(e, BinOp) and e.op == "-" and isinstance(e.right, Neg)
    assert isinstance(e.left, BinOp) and e.left.op == "+" and e.left.right.op == "*"
    assert parse_formula("(1+2)*3").args[0].op == "*"


def test_bare_expression_is_implicit_answer():
    f = parse_formula('COUNT(FIND(src:"a"))')
    assert isinstance(f, Answer) and f.implicit


@pytest.mark.parametrize(
    "src, fragment",
    [
        ("", "empty"),
        ("ANSWER()", "at least one"),
        ('ANSWER(NUM($1)', "expected `)`"),
        ('ANSWER(NUM($1), text:"x", NUM($2))', "`text:` must come last"),
        ('ANSWER(NUM($1), foo:"x")', "only accepts the keyword `text:`"),
        ("NUM($1.5)", "quoted substring"),
        ('NUM($1."x"#0)', ">= 1"),
        ('FIND(src:"a", src:"b")', "given twice"),
        ("1 < 2 < 3", "chained comparison"),
        ('"unterminated', "unterminated"),
        ('"bad \\q escape"', "bad escape"),
        ('"raw\nnewline"', "raw newline"),
        ("foo", "not a function call"),
        ("NUM(@)", "unexpected character"),
        ("[x.y]", "expected `]`"),
        ("1 2", "end of formula"),
    ],
)
def test_parse_errors_are_specific(src, fragment):
    with pytest.raises(ParseError) as e:
        parse_formula(src)
    assert fragment in e.value.message


def test_deep_nesting_and_huge_input_are_rejected_cleanly():
    with pytest.raises(ParseError):
        parse_formula("(" * 200 + "1" + ")" * 200)
    with pytest.raises(ParseError):
        parse_formula("1+" * 20000 + "1")


def test_lexer_unicode_escape():
    assert lex('"\\u00e9"')[0].value == "é"


# ---------------------------------------------------------------- typechecking


def test_types_of_spec_examples():
    assert types('DIFF(MONEY($1."$4.82 million"), MONEY($2."$4.31M"))') == ["Number"]
    assert types('COUNT(FIND(src:"x", re:"E"))') == ["Number"]
    assert types('DIFF(DATE($1), DATE($2))') == ["Duration"]
    assert types('DISTINCT(SYMBOLS(src:"r/**/*.py", kind:"function", calls:"f"))') == ["SpanSet"]
    assert types('MAX(COLUMN("d", src:"x.csv"), as:"date")') == ["Date"]
    assert types('SUM(COLUMN("amount", src:"x.csv"))') == ["Number"]
    assert types('IF(AFTER(DATE($1), DATE($2)), NUM($3), NUM($4))') == ["Number"]
    assert types("NUM($1)", ) == ["Number"] and types("$1") == ["Span"]
    assert types('GT(NUM($1), 5) ') == ["Bool"]
    assert types('DATE($1) + DURATION($2)') == ["Date"]


def test_cellrefs_must_point_backwards():
    assert types("ANSWER(COUNT(FIND(src:\"a\")), COUNT(FIND(src:\"b\")), PCT([0], [1], places:1))") == ["Number"] * 3
    with pytest.raises(FormulaTypeError) as e:
        types("ANSWER(PCT([1], [0]), NUM($1))")
    assert "EARLIER" in e.value.message
    with pytest.raises(FormulaTypeError):
        types("ANSWER([c_nope])")
    assert types("ANSWER([c_ok])", cell_type=lambda k: "Number" if k == "c_ok" else None) == ["Number"]


@pytest.mark.parametrize(
    "src, fragment",
    [
        ('NUM("5")', "extractors read a source span"),
        ('SUM($1, $2)', "wrap it in NUM"),
        ('DIFF(NUM($1), DATE($2))', "same type"),
        ('BOGUS($1)', "unknown function BOGUS"),
        ('COUNT_(FIND())', "unknown function"),
        ('count($1)', "did you mean COUNT"),
        ('NUM($1, 3)', "takes 1 argument"),
        ('NUM($1, bogus:"x")', "no keyword `bogus:`"),
        ('SUM(COLUMN("a", src:"x"), as:"weird")', "`as:` must be one of"),
        ('FIELD("status")', "only works inside"),
        ('FIND(src:"a", FIELD("x") >= NUM($1))', "compare FIELD"),
        ('FIND(src:"a", NUM($1))', "true/false conditions"),
        ('NUM($1) + DATE($2)', "cannot compute Number + Date"),
        ('IF(NUM($1), 1, 2)', "must be Bool"),
        ('IF(GT(NUM($1),1), NUM($2), DATE($3))', "same type"),
        ('GT(NUM($1), DATE($2))', "cannot compare"),
        ('SUM(NUM($1), DATE($2))', "same type"),
        ('SUM(DATE($1), DATE($2))', "cannot add dates"),
        ('LINES(from:1, to:2)', "needs src"),
        ('COLUMN(src:"x")', "at least 1"),
        ('ANSWER(NUM($1), text:"{3}")', "does not exist"),
        ('ANSWER(NUM($1), text:"a {b} c")', "only contain placeholders"),
        ('$1 == 5', "cannot compare"),
        ('-DATE($1)', "unary minus"),
    ],
)
def test_type_errors_are_specific(src, fragment):
    with pytest.raises(FormulaTypeError) as e:
        types(src)
    assert fragment in " | ".join(e.value.problems), e.value.problems


def test_all_problems_reported_at_once():
    with pytest.raises(FormulaTypeError) as e:
        types('ANSWER(NUM("x"), BOGUS($1), DIFF(NUM($1), DATE($2)))')
    assert len(e.value.problems) == 3
    assert "[V1]" in e.value.feedback()
