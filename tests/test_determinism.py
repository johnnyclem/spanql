"""Property-style determinism check (roadmap M2 exit criterion, ported from PR #1): the same
formula over the same sources yields the same value, cells, trace and display, regardless of
repetition, database row ids or ingestion order."""

from __future__ import annotations

import json
import random
from pathlib import Path
from typing import Any

from spanql.config import SourceConfig
from spanql.engine.run import evaluate_formula
from spanql.ingest.pipeline import ingest_all
from spanql.store.db import Store

from .helpers import handles

GLOBS = ['"logs/*"', '"logs/payments-*.log"', '"*.log"', '"reports/*.md"', '"repo/**"', '"data/*"']
REGEXES = ['"ERROR"', '"Timeout"', '"order=A1\\\\d\\\\d"', '"\\\\d{3}"', '"revenue"', '"def "']
EXTRA = ["", ', day:"2026-11-10"', ', level:"ERROR"', ', kind:"log"', ', FIELD("status") >= 500']
COLS = ['"amount"', '"customer"']
REGION = ["", ', FIELD("region") == "West"']
CATALOG = [
    lambda r: f"COUNT(FIND(src:{r.choice(GLOBS)}, re:{r.choice(REGEXES)}{r.choice(EXTRA)}))",
    lambda r: f"COUNT(FIND(src:{r.choice(GLOBS)}{r.choice(EXTRA)}))",
    lambda r: (
        f'COUNT(FIND(src:{r.choice(GLOBS)}, match:"{r.choice(["revenue", "PaymentTimeout", "retry"])}"))'
    ),
    lambda r: (
        f'PCT(COUNT(FIND(src:"logs/*", re:{r.choice(REGEXES)})), COUNT(FIND(src:"logs/*")), '
        f"places:{r.randint(0, 3)})"
    ),
    lambda r: (
        f'{r.choice(["SUM", "AVG", "MIN", "MAX"])}(COLUMN(src:"data/invoices.csv", "amount"'
        f"{r.choice(REGION)}))"
    ),
    lambda r: f'COUNT(DISTINCT(COLUMN(src:"data/invoices.csv", {r.choice(COLS)})))',
    lambda r: (
        f'COUNT(SYMBOLS(src:"repo/**", kind:"{r.choice(["function", "method", "class", "function|method"])}"))'
    ),
    lambda r: (
        'DISTINCT(SYMBOLS(src:"repo/**", kind:"function|method", calls:"retry_with_backoff"))'
    ),
    lambda r: 'DIFF(MONEY($1."$4.82 million"), MONEY($2."$4.31M"))',
    lambda r: (
        'PCT(DIFF(MONEY($1."$4.82 million"), MONEY($2."$4.31M")), MONEY($2."$4.31M"), places:1)'
    ),
    lambda r: f'ROUND(MONEY($1."$4.82 million"), places:{r.randint(0, 2)})',
]


def _store(path: Path, demo_dir: Path, order: list[str]) -> Store:
    st = Store(path)
    rep = ingest_all(st, [SourceConfig(path=str(demo_dir / sub)) for sub in order])
    assert not rep.errors
    return st


def _outcome(store: Store, formula: str) -> tuple[Any, ...]:
    hs = handles(store, ("reports/q3-2026.md", "$4.82 million"), ("reports/q2-2026.md", "$4.31M"))
    out = evaluate_formula(store, f"ANSWER({formula})", hs, question="How many?")
    if out.status != "grounded":  # errors must be deterministic too
        return ("error", tuple((p.code, p.message) for p in out.problems))
    return (
        "ok",
        out.answer,
        tuple(
            (
                c["id"],
                c["formula"],
                json.dumps(c["value"], sort_keys=True),
                json.dumps([(s["op"], s["out"]) for s in c["steps"]]),
                json.dumps(
                    [
                        i.get("count") if i["kind"] == "query" else i.get("consumed")
                        for i in c["inputs"]
                    ]
                ),
            )
            for c in out.cells
        ),
    )


def test_same_formula_same_result_across_stores_and_runs(tmp_path: Path, demo_dir: Path) -> None:
    a = _store(tmp_path / "a.db", demo_dir, ["logs", "reports", "data", "repo"])
    b = _store(tmp_path / "b.db", demo_dir, ["repo", "data", "reports", "logs"])  # other row ids
    rng = random.Random(20261119)
    ok = 0
    try:
        for _ in range(250):
            f = rng.choice(CATALOG)(rng)
            first = _outcome(a, f)
            assert first == _outcome(a, f), f  # repeat on the same store
            assert first == _outcome(b, f), f  # and on an independently built store
            ok += first[0] == "ok"
    finally:
        a.close()
        b.close()
    assert ok > 150, ok  # the generator mostly exercises successful evaluations
