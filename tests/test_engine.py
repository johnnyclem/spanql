from __future__ import annotations

import json
from decimal import Decimal

import pytest

from conftest import block_span
from spanql.engine import Engine, HandleTable
from spanql.engine.values import Number
from spanql.errors import (EmptySupportError, EvalError, ExtractError, FormulaTypeError, ParseError, ResolveError)
from spanql.ingest import ingest_path
from spanql.store import Store


@pytest.fixture
def eng(ingested):
    return Engine(ingested)


@pytest.fixture
def h(ingested):
    return HandleTable({
        1: block_span(ingested, "reports/q3-2026.md", "Total revenue"),
        2: block_span(ingested, "reports/q2-2026.md", "Q2 revenue"),
        3: block_span(ingested, "reports/q2-2026.md", "The invoice was issued"),
        4: block_span(ingested, "reports/q3-2026.md", "Headcount"),
    })


def one(ev):
    assert ev.kind == "answer" and len(ev.cells) == 1
    return ev.cells[0]


# ------------------------------------------------------- spec examples


def test_revenue_growth_example_from_the_spec(eng, h):
    ev = eng.evaluate(
        """ANSWER(
          DIFF(MONEY($1."$4.82 million"), MONEY($2."$4.31M")),
          PCT(DIFF(MONEY($1."$4.82 million"), MONEY($2."$4.31M")), MONEY($2."$4.31M"), places:1),
          text:"Revenue grew by {0} ({1}) from Q2 to Q3.")""", h)
    assert ev.render() == "Revenue grew by $510,000 (11.8%) from Q2 to Q3."
    c0, c1 = ev.cells
    assert (c0.value.value, c0.value.unit) == (Decimal(510000), "USD")
    assert [s["op"] for s in c1.steps] == ["MONEY", "MONEY", "DIFF", "PCT"]  # CSE: no duplicated MONEY steps
    spans = {i["id"]: i for i in c1.inputs}
    assert len(spans) == 2
    q3 = next(i for i in c1.inputs if i["source"] == "reports/q3-2026.md")
    assert q3["consumed"] == "$4.82 million" and q3["parsed"] == {"amount": "4820000", "currency": "USD"}
    assert q3["line"] == 5 and q3["path"] == "Q3 2026 Report > Revenue"


def test_count_and_percent_over_logs(eng, ingested):
    f = """ANSWER(
      COUNT(FIND(src:"logs/payments-*.log", re:"ERROR .*PaymentTimeout", day:"2026-11-10")),
      COUNT(FIND(src:"logs/payments-*.log", re:"\\\\bERROR\\\\b", day:"2026-11-10")),
      PCT([0], [1], places:1),
      text:"{0} timeouts out of {1} errors, which is {2}.")"""
    ev = eng.evaluate(f, HandleTable({}))
    assert ev.render() == "2 timeouts out of 3 errors, which is 66.7%."
    pct = ev.cells[2]
    assert [i["kind"] for i in pct.inputs] == ["cell", "cell"]
    q = ev.cells[0].inputs[0]
    assert q["kind"] == "query" and q["count"] == 2 and len(q["sample"]) == 2
    assert q["snapshots"] == {"logs/payments-2026-11-10.log": ingested.get_source("logs/payments-2026-11-10.log").current_snapshot}


def test_5xx_with_infix_predicate_from_the_spec(eng):
    ev = eng.evaluate('COUNT(FIND(src:"logs/api/*.log", kind:"log", day:"2026-11-16", FIELD("status") >= 500))',
                      HandleTable({}))
    assert one(ev).value == Number(Decimal(2))  # 500 and 503; 502 is on the 17th
    ev = eng.evaluate('COUNT(FIND(src:"logs/api/*.log", AND(GTE(FIELD("status"), 500), LT(FIELD("status"), 505))))',
                      HandleTable({}))
    assert one(ev).value.value == 3
    ev = eng.evaluate('COUNT(FIND(src:"logs/api/*.log", FIELD("method") == "POST"))', HandleTable({}))
    assert one(ev).value.value == 1
    ev = eng.evaluate('COUNT(FIND(src:"logs/api/*.log", NOT(FIELD("status") == 200), CONTAINS(FIELD("path"), "items")))',
                      HandleTable({}))
    assert one(ev).value.value == 4


def test_symbols_distinct_from_the_spec(eng):
    ev = eng.evaluate('ANSWER(COUNT(DISTINCT(SYMBOLS(src:"repo/**/*.py", kind:"function", calls:"retry_with_backoff"))),'
                      ' DISTINCT(SYMBOLS(src:"repo/**/*.py", kind:"function", calls:"retry_with_backoff")),'
                      ' text:"{0} functions call it: {1}.")', HandleTable({}))
    assert ev.render() == "2 functions call it: payments.retry.Client.get, payments.retry.standalone."
    assert eng.evaluate('COUNT(SYMBOLS(src:"repo/**/*.py", kind:"class"))', HandleTable({})).cells[0].value.value == 1
    assert eng.evaluate('COUNT(SYMBOLS(src:"repo/**/*.py", name:"get"))', HandleTable({})).cells[0].value.value == 1


def test_not_found_and_ambiguous_forms(eng, h):
    ev = eng.evaluate('NOT_FOUND("No churn figure.", searched:[$1,$2])', h)
    assert ev.kind == "not_found" and len(ev.spans) == 2 and ev.render() == "No churn figure."
    ev = eng.evaluate('AMBIGUOUS("Two revenue figures.", [$1, $2])', h)
    assert ev.kind == "ambiguous" and len(ev.spans) == 2
    # a refusal is always safe: unknown handles in searched: are dropped rather than blocking it
    ev = eng.evaluate('NOT_FOUND("x", searched:[$1, $9])', h)
    assert ev.kind == "not_found" and len(ev.spans) == 1
    with pytest.raises(ResolveError):  # but AMBIGUOUS shows its spans to the user, so they must exist
        eng.evaluate('AMBIGUOUS("x", [$1, $9])', h)


# ------------------------------------------------------- tables and files


def test_csv_column_aggregates_and_lookups(eng):
    hp = HandleTable({})
    assert one(eng.evaluate('SUM(COLUMN("units", src:"data/sales.csv"))', hp)).value.value == 26
    assert one(eng.evaluate('AVG(COLUMN("units", src:"data/sales.csv"))', hp)).value.value == Decimal("6.5")
    assert one(eng.evaluate('MAX(COLUMN("units", src:"data/sales.csv"))', hp)).value.value == 10
    ev = eng.evaluate('SUM(COLUMN("amount", src:"data/sales.csv", FIELD("region") == "EMEA"))', hp)
    assert one(ev).value.value == Decimal("1500.5")
    ev = eng.evaluate('NUM(COLUMN("amount", src:"data/sales.csv", FIELD("region") == "APAC", FIELD("product") == "widget"))', hp)
    assert one(ev).value.value == 950
    c = one(ev)
    assert any(i["kind"] == "span" and i["consumed"] == "950" for i in c.inputs)
    assert eng.evaluate('COUNT(FIND(src:"data/sales.csv", FIELD("units") > 4))', hp).cells[0].value.value == 3


def test_csv_dirty_cell_is_reported_with_its_location(eng):
    with pytest.raises(ExtractError) as e:
        eng.evaluate('SUM(COLUMN("amount", src:"data/sales.csv"))', HandleTable({}))
    assert "data/sales.csv" in e.value.message and "not a clean num" in e.value.message


def test_json_table_and_jsonl_fields(eng):
    hp = HandleTable({})
    assert one(eng.evaluate('SUM(COLUMN("price", src:"data/items.json"))', hp)).value.value == Decimal("21.5")
    assert one(eng.evaluate('COUNT(FIND(src:"logs/jobs.jsonl", level:"error"))', hp)).value.value == 2
    assert one(eng.evaluate('COUNT(FIND(src:"logs/jobs.jsonl", FIELD("attempts") > 3))', hp)).value.value == 1
    assert one(eng.evaluate('COUNT(FIND(src:"logs/jobs.jsonl", FIELD("level") == "error"))', hp)).value.value == 2
    ev = eng.evaluate('COUNT(FIND(src:"logs/jobs.jsonl", since:"2026-11-12T10:00:06Z"))', hp)
    assert one(ev).value.value == 1
    ev = eng.evaluate('COUNT(FIND(src:"logs/*", kind:"log", contains:"job failed"))', hp)
    assert one(ev).value.value == 2


def test_dates_and_logic(eng, h):
    ev = eng.evaluate('DAYS_BETWEEN(DATE($3."2026-03-01"), DATE($3."March 15, 2026"))', h)
    assert one(ev).value == Number(Decimal(14), "days")
    assert eng.evaluate('BEFORE(DATE($3."2026-03-01"), DATE($3."March 15, 2026"))', h).cells[0].display == "yes"
    ev = eng.evaluate('DIFF(DATE($3."March 15, 2026"), DATE($3."2026-03-01"))', h)
    assert one(ev).display == "14d"
    ev = eng.evaluate('IF(GT(MONEY($1."$4.82 million"), MONEY($2."$4.31M")), MONEY($1."$4.82 million"), MONEY($2."$4.31M"))', h)
    assert one(ev).display == "$4,820,000"
    ev = eng.evaluate('AND(GT(NUM($4."120"), 100), LT(NUM($4."120"), 200))', h)
    assert one(ev).display == "yes"
    ev = eng.evaluate('CONTAINS($1, "revenue")', h)
    assert one(ev).display == "yes"
    ev = eng.evaluate('EQ(TEXT($4."120"), "120")', h)
    assert one(ev).display == "yes"


def test_arithmetic_and_units(eng, h):
    assert one(eng.evaluate('ROUND(RATIO(NUM($4."120"), NUM($4."120") * 3), places:2)', h)).display == "0.33"
    assert one(eng.evaluate('NUM($4."120") * 2 + 1', h)).display == "241"
    assert one(eng.evaluate('MONEY($1."$4.82 million") / 2', h)).display == "$2,410,000"
    assert one(eng.evaluate('ABS(MONEY($2."$4.31M") - MONEY($1."$4.82 million"))', h)).display == "$510,000"
    with pytest.raises(EvalError) as e:
        eng.evaluate('MONEY($1."$4.82 million") + NUM($4."120")', h)
    assert "units differ" in e.value.message
    with pytest.raises(EvalError):
        eng.evaluate('MONEY($1."$4.82 million") * MONEY($2."$4.31M")', h)
    with pytest.raises(EvalError) as e:
        eng.evaluate('RATIO(NUM($4."120"), NUM($4."120") - NUM($4."120"))', h)
    assert "zero" in e.value.message
    with pytest.raises(EvalError):
        eng.evaluate('GT(MONEY($1."$4.82 million"), NUM($4."120"))', h)


# ------------------------------------------------------------ V2 / V3 / V5


def test_quote_errors_carry_retry_feedback(eng, h):
    with pytest.raises(ResolveError) as e:
        eng.evaluate('MONEY($1."$4.8 million")', h)
    assert "not found in $1" in e.value.message and "closest text" in e.value.feedback() and "$4.82 million" in e.value.feedback()
    with pytest.raises(ResolveError) as e:  # "2026" appears twice in $3
        eng.evaluate('TEXT($3."2026")', h)
    assert "occurs 2 times" in e.value.message and "#1..#2" in e.value.feedback()
    with pytest.raises(ResolveError) as e:
        eng.evaluate('TEXT($7."x")', h)
    assert "$7 does not exist" in e.value.message and "$1..$4" in e.value.message


def test_all_resolve_problems_reported_together(eng, h):
    with pytest.raises(ResolveError) as e:
        eng.evaluate('ANSWER(MONEY($1."nope"), MONEY($2."also nope"), MONEY($9))', h)
    assert len(e.value.problems) == 3


def test_occurrence_selects_among_duplicates(eng, h):
    c = one(eng.evaluate('TEXT($3."2026"#2)', h))
    assert c.value.value == "2026"
    first = one(eng.evaluate('TEXT($3."2026"#1)', h)).inputs[0]["id"]
    second = c.inputs[0]["id"]
    assert first != second
    with pytest.raises(ResolveError):
        eng.evaluate('TEXT($3."2026"#3)', h)


def test_extraction_ambiguity_is_refused(eng, h):
    assert one(eng.evaluate('NUM($4)', h)).display == "120"  # exactly one number in the span: fine
    with pytest.raises(ExtractError) as e:
        eng.evaluate('NUM($1)', h)  # two amounts: refuse to pick one
    assert "found 2 numbers" in e.value.message
    with pytest.raises(ExtractError) as e:
        eng.evaluate('MONEY($1)', h)
    assert "found 2 amounts" in e.value.message and "$4.82 million" in e.value.message


def test_empty_support_rules(eng):
    hp = HandleTable({})
    assert one(eng.evaluate('COUNT(FIND(src:"logs/payments-*.log", re:"NoSuchThing"))', hp)).value.value == 0  # a count of 0 is an answer
    for f in ('SUM(COLUMN("units", src:"data/sales.csv", FIELD("region") == "MARS"))',
              'AVG(COLUMN("units", src:"data/sales.csv", FIELD("region") == "MARS"))',
              'FIND(src:"logs/payments-*.log", re:"NoSuchThing")',
              'NUM(FIND(src:"logs/payments-*.log", re:"NoSuchThing"))'):
        with pytest.raises(EmptySupportError):
            eng.evaluate(f, hp)
    with pytest.raises(EmptySupportError) as e:  # a typo'd glob must not silently count 0
        eng.evaluate('COUNT(FIND(src:"logz/*.log"))', hp)
    assert "matches no ingested source" in e.value.message and "ingested sources include" in (e.value.hint or "")
    with pytest.raises(EmptySupportError) as e:
        eng.evaluate('SUM(COLUMN("nope", src:"data/sales.csv"))', hp)
    assert "columns available" in e.value.hint and "units" in e.value.hint
    with pytest.raises(ExtractError) as e:
        eng.evaluate('NUM(FIND(src:"logs/payments-*.log", re:"ERROR"))', hp)
    assert "matched" in e.value.message


def test_query_argument_validation(eng):
    hp = HandleTable({})
    for f, frag in (('COUNT(FIND(src:"a", re:"("))', "not a valid regular expression"),
                    ('COUNT(FIND(src:"logs/*", day:"yesterday"))', "must look like"),
                    ('COUNT(FIND(src:"logs/*", since:"soon"))', "not a date"),
                    ('COUNT(SYMBOLS(src:"repo/*", kind:"widget"))', "must be one of"),
                    ('COUNT(LINES(src:"logs/*", from:1, to:2))', "exactly one source")):
        with pytest.raises((EvalError, EmptySupportError, FormulaTypeError)) as e:
            eng.evaluate(f, hp)
        assert frag in e.value.message, (f, e.value.message)


def test_lines_query(eng, ingested):
    ev = eng.evaluate('TEXT(LINES(src:"data/sales.csv", from:1, to:2))', HandleTable({}))
    assert one(ev).value.value == "region,product,amount,units\nEMEA,widget,\"1,200.50\",10"
    with pytest.raises(Exception):
        eng.evaluate('TEXT(LINES(src:"data/sales.csv", from:50, to:60))', HandleTable({}))


# ------------------------------------------------- security and determinism


def test_formulas_cannot_inject_sql(eng, ingested):
    hp = HandleTable({})
    evil = "x'; DROP TABLE block; --"
    for f in (f'COUNT(FIND(src:"logs/*", contains:"{evil}"))', f'COUNT(FIND(src:"logs/*", re:"{evil}"))',
              f'COUNT(FIND(src:"logs/*", day:"2026-11-10", logger:"{evil}"))',
              f'COUNT(FIND(src:"logs/*", FIELD("level") == "{evil}"))',
              f'COUNT(FIND(src:"logs/*", CONTAINS(FIELD("user"), "{evil}")))',
              f'COUNT(SYMBOLS(src:"repo/*", calls:"{evil}"))'):
        try:
            eng.evaluate(f, hp)
        except (EvalError, EmptySupportError):
            pass
    with pytest.raises(FormulaTypeError):
        eng.evaluate('COUNT(FIND(src:"logs/*", FIELD("a\') OR 1=1 --") == 1))', hp)
    assert ingested.status()["blocks"] > 0  # tables still intact


def test_evaluation_is_deterministic_and_pinned(eng, ingested, corpus):
    hp = HandleTable({})
    f = 'COUNT(FIND(src:"logs/payments-*.log", re:"PaymentTimeout"))'
    first = eng.evaluate(f, hp)
    pins = first.snapshots
    assert one(first).value.value == 3
    # the log grows; unpinned evaluation sees it, pinned evaluation reproduces the old answer
    p = corpus / "logs/payments-2026-11-10.log"
    p.write_text(p.read_text() + "2026-11-12 10:00:00,000 ERROR payments.api - PaymentTimeout again user=zed\n")
    ingest_path(ingested, str(corpus / "logs"))
    assert one(eng.evaluate(f, hp)).value.value == 4
    again = eng.evaluate(f, hp, pins=pins)
    assert one(again).value.value == 3
    assert json.dumps(one(again).to_json()["value"]) == json.dumps(one(first).to_json()["value"])
    assert again.cells[0].steps == first.cells[0].steps


def test_cells_persist_with_dependencies_and_go_stale(eng, ingested, corpus, h):
    ev = eng.evaluate('ANSWER(COUNT(FIND(src:"logs/payments-*.log", re:"ERROR")), MONEY($1."$4.82 million"))', h)
    ingested.save_cells([c.to_json() for c in ev.cells])
    cell = ingested.get_cell(ev.cells[0].id)
    assert cell["display"] == ev.cells[0].display
    deps = ingested.db.execute("SELECT dep_kind FROM cell_dep WHERE cell_id=?", (ev.cells[0].id,)).fetchall()
    assert {d[0] for d in deps} == {"query", "snapshot"}
    assert ingested.staleness(cell) == [] and ingested.staleness(ingested.get_cell(ev.cells[1].id)) == []
    (corpus / "reports/q3-2026.md").write_text("# changed\n")
    p = corpus / "logs/payments-2026-11-10.log"
    p.write_text(p.read_text() + "2026-11-12 10:00:00,000 ERROR x - y\n")
    ingest_path(ingested, str(corpus / "reports"))
    ingest_path(ingested, str(corpus / "logs"))
    assert ingested.staleness(ingested.get_cell(ev.cells[1].id))[0]["reason"] == "source changed"
    assert ingested.staleness(ingested.get_cell(ev.cells[0].id))[0]["kind"] == "query"


def test_stored_cell_can_be_referenced_in_a_follow_up(eng, ingested, h):
    ev = eng.evaluate('MONEY($1."$4.82 million")', h)
    ingested.save_cells([c.to_json() for c in ev.cells])
    cid = ev.cells[0].id
    ev2 = eng.evaluate(f"DIFF([{cid}], MONEY($2.\"$4.31M\"))", h)
    assert one(ev2).display == "$510,000"
    assert {i["kind"] for i in one(ev2).inputs} == {"cell", "span"}
    with pytest.raises(FormulaTypeError):
        eng.evaluate("DIFF([c_missing], NUM($4))", h)


def test_default_template_joins_values(eng, h):
    ev = eng.evaluate('ANSWER(MONEY($1."$4.82 million"), NUM($4."120"))', h)
    assert ev.render() == "$4,820,000, 120"


def test_parse_and_type_errors_surface_from_evaluate(eng, h):
    with pytest.raises(ParseError):
        eng.evaluate("ANSWER(", h)
    with pytest.raises(FormulaTypeError):
        eng.evaluate("SUM($1)", h)


def test_recompute_follows_edits_and_reports_failures(eng, ingested, corpus, h):
    from spanql.engine.recompute import recompute

    ev = eng.evaluate('ANSWER(MONEY($1."$4.82 million"), COUNT(FIND(src:"logs/payments-*.log", re:"ERROR")), text:"{0}/{1}")',
                      h, question_id="q1")
    ingested.save_cells([c.to_json() for c in ev.cells])
    cid = ev.cells[0].id
    # nothing changed: same values, not flagged as changed
    r = recompute(eng, cid)
    assert r.status == "ok" and not r.changed and r.evaluation.render() == ev.render()
    # text moves but the value is unchanged: still resolves, still unchanged
    p = corpus / "reports/q3-2026.md"
    p.write_text("# Preface\n\nSome new intro.\n\n" + p.read_text())
    ingest_path(ingested, str(corpus / "reports"))
    r = recompute(eng, cid)
    assert r.status == "ok" and not r.changed and r.reanchored[0]["status"] == "moved"
    # the value itself changes in the source: the old quote no longer matches, and we say so
    p.write_text(p.read_text().replace("$4.82 million", "$5.10 million"))
    ingest_path(ingested, str(corpus / "reports"))
    r = recompute(eng, cid)
    assert r.status == "failed" and "not found" in r.error["message"]
    assert r.reanchored[0]["status"] == "edited"  # we found where it went, and report that it was edited
    # the file disappears entirely
    p.unlink()
    ingest_path(ingested, str(corpus / "reports"))
    r = recompute(eng, cid)
    assert r.status == "failed" and "no longer be located" in r.error["message"]
    assert ingested.get_cell(cid)["display"] == "$4,820,000"  # the original cell is never rewritten
    with pytest.raises(KeyError):
        recompute(eng, "c_nope")


@pytest.mark.parametrize("pattern", ["(a+)+$", "(a*)*b", "(.*a)+", "(\\\\w+\\\\s*)+x", "(?:x+)*y", "((a+)b)*"])
def test_catastrophic_regexes_are_refused_before_running(eng, pattern):
    with pytest.raises(EvalError) as e:
        eng.evaluate(f'COUNT(FIND(src:"logs/*", re:"{pattern}"))', HandleTable({}))
    assert "exponential" in e.value.message and e.value.hint


@pytest.mark.parametrize("pattern", ["ERROR .*PaymentTimeout", "\\\\bERROR\\\\b|WARN", "(\\\\d+\\\\.){3}\\\\d+", "(?i)timeout", "a{2,5}",
                                     "(foo|bar)baz", "x+y*z?", "user=(alice|bob)"])
def test_ordinary_regexes_are_still_allowed(eng, pattern):
    eng.evaluate(f'COUNT(FIND(src:"logs/*", re:"{pattern}"))', HandleTable({}))


def test_slow_queries_hit_the_deadline(tmp_path):
    from spanql.engine.query import ScanSpec, run_scan

    log = tmp_path / "big.log"
    log.write_text("".join(f"2026-11-10 10:00:00,000 INFO svc - request id={i} status=200 user=u{i % 97}\n"
                           for i in range(40_000)))
    s = Store(":memory:")
    ingest_path(s, str(log))
    spec = ScanSpec("FIND", src="big.log", regex="status=2\\d\\d user=u9")
    assert run_scan(s, spec, timeout=60).total > 0  # fine with a normal budget
    with pytest.raises(EvalError) as e:
        run_scan(s, spec, timeout=0.0005)
    assert "longer than" in e.value.message and "narrow it" in e.value.hint


def test_duration_and_date_aggregates(tmp_path):
    (tmp_path / "jobs.csv").write_text(
        "job,took,done\nbuild,3m 20s,2026-03-01\ntest,45 seconds,2026-03-15\ndeploy,1.5 hours,2026-02-10\n")
    s = Store(":memory:")
    ingest_path(s, str(tmp_path / "jobs.csv"))
    e, hp = Engine(s), HandleTable({})

    def ev(f):
        return e.evaluate(f, hp).cells[0].display

    col = 'COLUMN("took", src:"jobs.csv")'
    assert ev(f'SUM({col}, as:"duration")') == "1h 34m 5s"
    assert ev(f'AVG({col}, as:"duration")') == "31m 21.666667s"
    assert ev(f'MAX({col}, as:"duration")') == "1h 30m" and ev(f'MIN({col}, as:"duration")') == "45s"
    done = 'COLUMN("done", src:"jobs.csv")'
    assert ev(f'MAX({done}, as:"date")') == "2026-03-15" and ev(f'MIN({done}, as:"date")') == "2026-02-10"
    assert ev(f'DIFF(MAX({done}, as:"date"), MIN({done}, as:"date"))') == "33d"
    two = 'SUM(DURATION(LINES(src:"jobs.csv", from:2, to:2)), DURATION(LINES(src:"jobs.csv", from:3, to:3)))'
    assert ev(two) == "4m 5s"
    with pytest.raises(EvalError):  # mixed units are never combined
        s2 = Store(":memory:")
        (tmp_path / "m.csv").write_text("v\n$5\n€6\n")
        ingest_path(s2, str(tmp_path / "m.csv"))
        Engine(s2).evaluate('SUM(COLUMN("v", src:"m.csv"), as:"money")', hp)
