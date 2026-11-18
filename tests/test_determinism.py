"""Property-style determinism tests (roadmap M2 exit criterion): the same formula
over the same snapshots yields the same value, trace and display, regardless of
evaluation order, database row ids, or ingestion order."""

from __future__ import annotations

import json
import random

from conftest import block_span, write_corpus
from spanql.engine import Engine, HandleTable
from spanql.errors import SpanqlError
from spanql.ingest import ingest_path
from spanql.store import Store

GLOBS = ['"logs/*"', '"logs/payments-*.log"', '"logs/api/*.log"', '"logs/jobs.jsonl"', '"data/*"', '"reports/*.md"',
         '"repo/**/*.py"']
REGEXES = ['"ERROR"', '"Timeout"', '"user=[a-d]"', '"\\\\d{3}"', '"revenue"', '"job failed"', '"def "']
EXTRA = ['', ', day:"2026-11-10"', ', level:"error"', ', kind:"log"', ', FIELD("status") >= 400',
         ', FIELD("units") > 3', ', since:"2026-11-10T09:00:03Z"']
CATALOG = [
    lambda r: f'COUNT(FIND(src:{r.choice(GLOBS)}, re:{r.choice(REGEXES)}{r.choice(EXTRA)}))',
    lambda r: f'COUNT(FIND(src:{r.choice(GLOBS)}{r.choice(EXTRA)}))',
    lambda r: f'PCT(COUNT(FIND(src:"logs/*", re:{r.choice(REGEXES)})), COUNT(FIND(src:"logs/*")), places:{r.randint(0, 3)})',
    lambda r: f'{r.choice(["SUM", "AVG", "MIN", "MAX"])}(COLUMN("units", src:"data/sales.csv"{r.choice(EXTRA[:1] + [", FIELD(\"region\") == \"EMEA\""])}))',
    lambda r: 'DISTINCT(SYMBOLS(src:"repo/**/*.py", kind:"function", calls:"retry_with_backoff"))',
    lambda r: f'COUNT(SYMBOLS(src:"repo/**/*.py", kind:{r.choice(["\"function\"", "\"method\"", "\"class\"", "\"module\""])}))',
    lambda r: 'DIFF(MONEY($1."$4.82 million"), MONEY($2."$4.31M"))',
    lambda r: 'PCT(DIFF(MONEY($1."$4.82 million"), MONEY($2."$4.31M")), MONEY($2."$4.31M"), places:1)',
    lambda r: 'DAYS_BETWEEN(DATE($3."2026-03-01"), DATE($3."March 15, 2026"))',
    lambda r: f'FIND(src:{r.choice(GLOBS)}, re:{r.choice(REGEXES)})',
]


def make_store(corpus, order):
    s = Store(":memory:")
    for sub in order:
        ingest_path(s, str(corpus / sub))
    return s


def handles(s):
    return HandleTable({
        1: block_span(s, "reports/q3-2026.md", "Total revenue"),
        2: block_span(s, "reports/q2-2026.md", "Q2 revenue"),
        3: block_span(s, "reports/q2-2026.md", "The invoice was issued"),
    })


def outcome(engine, formula, h):
    try:
        ev = engine.evaluate(formula, h)
    except SpanqlError as e:  # errors must be deterministic too
        return ("error", e.check, e.message)
    return ("ok", [(c.display, json.dumps(c.to_json()["value"], sort_keys=True),
                    [(s["op"], s["out"]) for s in c.steps],
                    [i.get("count") if i["kind"] == "query" else i.get("consumed") for i in c.inputs]) for c in ev.cells])


def test_same_formula_same_result_across_stores_and_runs(tmp_path):
    corpus = write_corpus(tmp_path / "c")
    a = make_store(corpus, ["logs", "reports", "data", "repo"])
    b = make_store(corpus, ["repo", "data", "reports", "logs"])  # different ingestion order => different row ids
    ea, eb = Engine(a), Engine(b)
    ha, hb = handles(a), handles(b)
    rng = random.Random(20261118)
    checked_ok = 0
    for _ in range(300):
        f = rng.choice(CATALOG)(rng)
        first = outcome(ea, f, ha)
        assert first == outcome(ea, f, ha), f  # repeat on the same store
        assert first == outcome(eb, f, hb), f  # and on an independently built store
        checked_ok += first[0] == "ok"
    assert checked_ok > 150  # the generator mostly exercises successful evaluations
