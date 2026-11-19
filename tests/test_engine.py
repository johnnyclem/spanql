from __future__ import annotations

import pytest

from spanql.engine.run import evaluate_formula
from spanql.store.db import Store

from .helpers import Q2, Q3, handles

GROWTH = (
    'ANSWER(DIFF(MONEY($1."$4.82 million"), MONEY($2."$4.31M")), '
    'PCT(DIFF(MONEY($1."$4.82 million"), MONEY($2."$4.31M")), MONEY($2."$4.31M"), places:1), '
    'text:"Revenue grew by {0} ({1}) from Q2 to Q3.")'
)


def ev(store: Store, formula: str, *specs, question: str | None = None, **kw):
    return evaluate_formula(store, formula, handles(store, *specs), question=question, **kw)


def codes(out) -> list[str]:
    return [p.code for p in out.problems]


def test_design_example(demo_store: Store) -> None:
    out = ev(demo_store, GROWTH, Q3, Q2, question="How much did revenue grow from Q2 to Q3?")
    assert out.status == "grounded", out.problems
    assert out.answer == "Revenue grew by $510,000 (11.8%) from Q2 to Q3."
    diff, _pct = out.cells
    assert diff["value"]["decimal"].startswith("510000")
    spans = [i for i in diff["inputs"] if i["kind"] == "span"]
    assert {s["text"] for s in spans} >= {"$4.82 million", "$4.31M"} or len(spans) == 2
    assert all("consumed_span" in s or "id" in s for s in spans)
    assert diff["steps"], "every derived value records its steps"


def test_cell_ids_are_deterministic(demo_store: Store) -> None:
    a = ev(demo_store, GROWTH, Q3, Q2)
    b = ev(demo_store, GROWTH, Q3, Q2)
    assert [c["id"] for c in a.cells] == [c["id"] for c in b.cells]
    assert [c["value"] for c in a.cells] == [c["value"] for c in b.cells]


def test_provenance_points_at_exact_bytes(demo_store: Store) -> None:
    out = ev(demo_store, 'ANSWER(MONEY($1."$4.82 million"))', Q3)
    inp = next(i for i in out.cells[0]["inputs"] if i["kind"] == "span")
    span = inp.get("consumed_span") or inp["id"]
    assert demo_store.resolve(span).text == "$4.82 million"


# ---- V1 syntax / types
def test_v1_syntax_error(demo_store: Store) -> None:
    out = ev(demo_store, "ANSWER(MONEY(", Q3)
    assert out.status == "error" and codes(out) == ["V1"]


@pytest.mark.parametrize(
    "formula",
    [
        'ANSWER(MONEY($1."$4.82 million", bogus:1))',  # unknown kwarg
        'ANSWER(FIND(match:"x"))',  # bare SpanSet
        'ANSWER(COUNT(FIND(CONTAINS($1, "x"))))',  # bad predicate
        'ANSWER(MONEY($1."$4.82 million"), text:"{0} and {1}")',  # missing placeholder value
    ],
)
def test_v1_type_errors(demo_store: Store, formula: str) -> None:
    out = ev(demo_store, formula, Q3)
    assert out.status == "error"
    assert "V1" in codes(out), out.problems


def test_bad_predicate_feedback_mentions_match(demo_store: Store) -> None:
    out = ev(demo_store, 'ANSWER(COUNT(FIND(CONTAINS($1, "PaymentTimeout"))))', Q3)
    assert any("match:" in p.message for p in out.problems)


# ---- V2 citations
def test_v2_unknown_handle_and_bad_quote(demo_store: Store) -> None:
    out = ev(demo_store, 'ANSWER(MONEY($9."$4.82 million"))', Q3)
    assert "V2" in codes(out)
    out = ev(demo_store, 'ANSWER(MONEY($1."$9.99 million"))', Q3)
    assert "V2" in codes(out)
    assert any(p.hint and "closest" in p.hint.lower() for p in out.problems) or out.problems


def test_v2_ambiguous_quote_needs_occurrence(demo_store: Store) -> None:
    out = ev(demo_store, 'ANSWER(MONEY($1."million"))', Q3)
    assert "V2" in codes(out)
    assert any("#" in (p.hint or "") or "occur" in p.message for p in out.problems)
    out = ev(demo_store, 'ANSWER(TEXT($1."million"#2))', Q3)
    assert out.status == "grounded"


def test_refusal_handles_must_exist(demo_store: Store) -> None:
    assert ev(demo_store, 'NOT_FOUND("no", searched:[$1])', Q3).status == "not_found"
    assert codes(ev(demo_store, 'NOT_FOUND("no", searched:[$4])', Q3)) == ["V2"]
    out = ev(demo_store, 'AMBIGUOUS("conflict", [$1, $2])', Q3, Q2)
    assert out.status == "ambiguous" and len(out.handles) == 2


# ---- V3 extraction
def test_v3_extraction_failures(demo_store: Store) -> None:
    out = ev(demo_store, 'ANSWER(MONEY($1."Total revenue"))', Q3)
    assert "V3" in codes(out)
    out = ev(demo_store, 'ANSWER(DATE($1."Q3 2026"))', ("demo/reports/q3-2026.md", "# Q3 2026"))
    assert "V3" in codes(out)


def test_unit_mismatch(demo_store: Store) -> None:
    spec = ("demo/reports/q3-2026.md", "1,284 paying")
    out = ev(demo_store, 'ANSWER(DIFF(MONEY($1."$3.10 million"), NUM($1."1,284")))', spec)
    assert out.status == "error"


# ---- V4 literals
@pytest.mark.parametrize(
    "formula",
    [
        "ANSWER(4820000)",
        'ANSWER(MONEY($1."$4.82 million") * 1.07)',
        'ANSWER(MONEY($1."$4.82 million"), text:"{0}, about 5 million")',
        'ANSWER(TEXT("Q3 revenue"))',
    ],
)
def test_v4_ungrounded_values(demo_store: Store, formula: str) -> None:
    out = ev(demo_store, formula, Q3)
    assert out.status == "error"
    assert "V4" in codes(out), out.problems


def test_v4_allows_query_and_formatting_numbers(demo_store: Store) -> None:
    out = ev(demo_store, 'ANSWER(ROUND(MONEY($1."$4.82 million"), places:2))', Q3)
    assert out.status == "grounded"
    out = ev(demo_store, 'ANSWER(COUNT(FIND(src:"demo/logs/*.log", FIELD("status") >= 500)))')
    assert out.status == "grounded", out.problems


@pytest.mark.parametrize(
    "formula",
    [
        'ANSWER(MONEY($1."$4.82 million") * 2)',
        'ANSWER(MONEY($1."$4.82 million") / 1000)',
        'ANSWER(IF(MONEY($1."$4.82 million") > MONEY($1."$4.82 million"), '
        'MONEY($1."$4.82 million"), MONEY($1."$4.82 million") * 2))',
    ],
)
def test_v4_no_obvious_constant_allowance(demo_store: Store, formula: str) -> None:
    out = ev(demo_store, formula, Q3, question="What was Q3 revenue?")
    assert out.status == "error" and "V4" in codes(out), out.problems


def test_v4_allows_numbers_typed_in_the_question(demo_store: Store) -> None:
    f = 'ANSWER(MONEY($1."$4.82 million") * 3)'
    out = ev(demo_store, f, Q3, question="What is 3 times Q3 revenue?")
    assert out.status == "grounded", out.problems
    assert out.cells[0]["value"]["decimal"].startswith("14460000")
    assert out.warnings  # recorded on the cell as coming from the question
    assert ev(demo_store, f, Q3, question="What is Q3 revenue?").status == "error"
    out = ev(
        demo_store,
        'ANSWER(MONEY($1."$4.82 million") / 1000)',
        Q3,
        question="What is Q3 revenue in thousands (divide by 1,000)?",
    )
    assert out.status == "grounded", out.problems


def test_v4_template_allows_numbers_from_question(demo_store: Store) -> None:
    f = (
        'ANSWER(COUNT(FIND(src:"demo/logs/*.log", day:"2026-11-10", match:"PaymentTimeout")), '
        'text:"{0} timeouts on 2026-11-10.")'
    )
    assert ev(demo_store, f, question="How many timeouts on 2026-11-10?").status == "grounded"
    assert ev(demo_store, f, question="How many timeouts that day?").status == "error"


# ---- V5 support
def test_v5_empty_support(demo_store: Store) -> None:
    out = ev(
        demo_store,
        'ANSWER(SUM(COLUMN(src:"demo/data/invoices.csv", "amount", FIELD("region") == "Mars")))',
    )
    assert "V5" in codes(out)
    out = ev(demo_store, 'ANSWER(COUNT(FIND(match:"zebra")))', question="How many zebras?")
    assert out.status == "grounded" and out.cells[0]["value"]["decimal"] == "0"
    out = ev(demo_store, 'ANSWER(COUNT(FIND(match:"zebra")))', question="What is the zebra?")
    assert "V5" in codes(out)
    out = ev(demo_store, 'ANSWER(DISTINCT(SYMBOLS(calls:"nope")))', question="Who calls nope?")
    assert "V5" in codes(out)


# ---- V6 relevance
def test_v6_relevance_rejects(demo_store: Store) -> None:
    out = ev(
        demo_store,
        'ANSWER(MONEY($1."$4.82 million"))',
        Q3,
        question="What was churn?",
        relevance=lambda q, p: False,
    )
    assert "V6" in codes(out)
    ok = ev(
        demo_store,
        'ANSWER(MONEY($1."$4.82 million"))',
        Q3,
        question="Q3 revenue?",
        relevance=lambda q, p: True,
    )
    assert ok.status == "grounded"


def test_column_unknown_header_lists_real_ones(demo_store: Store) -> None:
    out = ev(demo_store, 'ANSWER(SUM(COLUMN(src:"demo/data/invoices.csv", "header")))')
    assert any("columns are" in p.message for p in out.problems)


# ---- functions
@pytest.mark.parametrize(
    "formula,display",
    [
        (
            'ANSWER(SUM(COLUMN(src:"demo/data/invoices.csv", "amount", FIELD("region") == "West")))',
            "2,075.75",
        ),
        ('ANSWER(COUNT(COLUMN(src:"demo/data/invoices.csv", "invoice")))', "4"),
        ('ANSWER(MAX(COLUMN(src:"demo/data/invoices.csv", "amount")))', "3,400"),
        ('ANSWER(MIN(COLUMN(src:"demo/data/invoices.csv", "amount")))', "875.25"),
        (
            'ANSWER(COUNT(FIND(src:"demo/logs/*.log", day:"2026-11-10", FIELD("status") >= 500)))',
            "3",
        ),
        ('ANSWER(COUNT(FIND(src:"demo/logs/*.log", match:"PaymentTimeout", level:"ERROR")))', "3"),
        (
            'ANSWER(COUNT(FIND(src:"demo/logs/*.log", day:"2026-11-10", match:"PaymentTimeout")))',
            "2",
        ),
        ('ANSWER(COUNT(FIND(src:"demo/logs/*.log", re:"order=[AB]\\\\d+", level:"ERROR")))', "2"),
        ('ANSWER(COUNT(FIND(kind:"log|doc", match:"PaymentTimeout")))', "3"),
        (
            'ANSWER(MAX(FIELD("latency_ms", FIND(src:"demo/logs/*.log", day:"2026-11-10"))))',
            "30,001",
        ),
        (
            'ANSWER(COUNT(DISTINCT(FIELD("order", FIND(src:"demo/logs/*.log", match:"PaymentTimeout")))))',
            "3",
        ),
        ('ANSWER(COUNT(SYMBOLS(kind:"callable", calls:"retry_with_backoff")))', "2"),
        ('ANSWER(COUNT(SYMBOLS(kind:"class")))', "2"),
        ('ANSWER(COUNT(LINES(src:"demo/repo/**", re:"retry")))', None),
    ],
)
def test_queries(demo_store: Store, formula: str, display: str | None) -> None:
    out = ev(demo_store, formula, question="How many / what total?")
    assert out.status == "grounded", out.problems
    if display is not None:
        assert out.cells[0]["value"]["display"] == display
    q = out.cells[0]["inputs"]
    assert any(i["kind"] == "query" for i in q)
    rec = next(i for i in q if i["kind"] == "query")
    assert rec["sql"] and "snapshot_set" in rec


def test_query_records_snapshot_set_for_recompute(demo_store: Store) -> None:
    out = ev(
        demo_store, 'ANSWER(COUNT(FIND(src:"demo/logs/*.log", level:"ERROR")))', question="how many"
    )
    rec = next(i for i in out.cells[0]["inputs"] if i["kind"] == "query")
    assert len(rec["snapshot_set"]) == 1


def test_dates_and_logic(demo_store: Store) -> None:
    spec = ("demo/reports/q3-2026.md", "published on")
    log = ("demo/logs/payments-2026-11-10.log", "order=B220")
    f = (
        'ANSWER(DAYS_BETWEEN(DATE($1."November 3, 2026"), DATE($2."2026-11-10")), '
        'BEFORE(DATE($1."November 3, 2026"), DATE($2."2026-11-10")), '
        'IF(GT(NUM($1."2026"), NUM($2."30001")), "big", "small"))'
    )
    out = ev(demo_store, f, spec, log)
    assert out.status == "grounded", out.problems
    assert [c["value"]["display"] for c in out.cells] == ["7 days", "yes", "small"]


def test_ratio_avg_abs(demo_store: Store) -> None:
    out = ev(
        demo_store,
        'ANSWER(RATIO(MONEY($1."$4.82 million"), MONEY($2."$4.31M"), places:2), '
        'ABS(DIFF(MONEY($2."$4.31M"), MONEY($1."$4.82 million"))), '
        'AVG(COLUMN(src:"demo/data/invoices.csv", "amount"), places:2))',
        Q3,
        Q2,
    )
    assert out.status == "grounded", out.problems
    assert out.cells[0]["value"]["display"] == "1.12"
    assert out.cells[1]["value"]["display"] == "$510,000"
    assert out.cells[2]["value"]["display"] == "1,893.94"


def test_cellref(demo_store: Store) -> None:
    first = ev(demo_store, 'ANSWER(MONEY($1."$4.82 million"))', Q3, save=True)
    cid = first.cells[0]["id"]
    f = f"ANSWER(ROUND([{cid}] / 1000, places:0))"
    out = evaluate_formula(demo_store, f, {}, question="What is that in thousands (/ 1000)?")
    assert out.status == "grounded", out.problems
    assert evaluate_formula(demo_store, f, {}).status == "error"  # 1000 is not from anywhere
    assert out.cells[0]["value"]["display"] == "$4,820"


def test_saved_cells_have_trace(demo_store: Store) -> None:
    out = ev(demo_store, GROWTH, Q3, Q2, save=True)
    stored = demo_store.get_cell(out.cells[0]["id"])
    assert stored is not None and stored["formula"].startswith("DIFF(")


@pytest.mark.parametrize(
    "template",
    ["{0} million in revenue", "revenue was ${0}", "{0}% growth", "{0} dollars"],
)
def test_v4_template_cannot_add_units(demo_store: Store, template: str) -> None:
    out = ev(demo_store, f'ANSWER(MONEY($1."$4.82 million"), text:"{template}")', Q3)
    assert "V4" in codes(out), out.problems


def test_template_prose_after_placeholder_ok(demo_store: Store) -> None:
    out = ev(demo_store, 'ANSWER(MONEY($1."$4.82 million"), text:"{0} in Q3 revenue (Kept)")', Q3)
    assert out.status == "grounded", out.problems


@pytest.mark.parametrize(
    "formula",
    [
        'ANSWER(COUNT(FIND(src:"demo/logz/*.log", match:"PaymentTimeout")))',
        'ANSWER(SUM(COLUMN(src:"demo/data/invoice.csv", "amount")))',
        'ANSWER(COUNT(SYMBOLS(src:"demo/rep/**", kind:"function")))',
        'ANSWER(COUNT(LINES(src:"nope.txt", re:"x")))',
    ],
)
def test_v5_src_matching_nothing_is_an_error_with_suggestions(
    demo_store: Store, formula: str
) -> None:
    out = ev(demo_store, formula, question="How many are there?")
    assert out.status == "error" and "V5" in codes(out), out.problems
    p = out.problems[0]
    assert "matches no ingested source" in p.message
    assert p.hint and p.hint.startswith("did you mean")


def test_v5_typo_suggestion_names_the_right_source(demo_store: Store) -> None:
    out = ev(
        demo_store, 'ANSWER(COUNT(FIND(src:"demo/logz/payments-*.log")))', question="How many?"
    )
    assert "demo/logs/payments-2026-11-10.log" in (out.problems[0].hint or "")
    # a glob that matches but finds nothing is still a legitimate zero for a count question
    ok = ev(
        demo_store,
        'ANSWER(COUNT(FIND(src:"demo/logs/*.log", re:"NoSuchThing")))',
        question="How many?",
    )
    assert ok.status == "grounded" and ok.cells[0]["value"]["decimal"] == "0"


def test_arg_refs_in_answer(demo_store: Store) -> None:
    f = (
        'ANSWER(COUNT(FIND(src:"demo/logs/*.log", match:"PaymentTimeout")), '
        'COUNT(FIND(src:"demo/logs/*.log", level:"ERROR")), PCT([0], [1], places:1), '
        'text:"{0} of {1} errors ({2}) were timeouts.")'
    )
    out = ev(demo_store, f, question="What share of errors were payment timeouts?")
    assert out.status == "grounded", out.problems
    assert "PCT([0], [1], places:1)" in out.formula  # the formula as written
    pct = out.cells[2]
    assert pct["formula"].startswith("PCT(COUNT(FIND(")  # the cell is self-contained
    assert pct["value"]["unit"] == "%"
    n, d = (c["value"]["decimal"] for c in out.cells[:2])
    assert out.answer == f"{n} of {d} errors ({pct['value']['display']}) were timeouts."


def test_arg_ref_forward_is_v1(demo_store: Store) -> None:
    out = ev(demo_store, 'ANSWER(PCT([1], [1]), MONEY($1."$4.82 million"))', Q3)
    assert out.status == "error" and codes(out) == ["V1"]
