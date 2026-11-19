"""Model-written regexes must not be able to hang the engine."""

from __future__ import annotations

import time
from pathlib import Path

import pytest

from spanql.config import SourceConfig
from spanql.engine.run import evaluate_formula
from spanql.ingest.pipeline import ingest_all
from spanql.store import saferegex
from spanql.store.db import Store


@pytest.fixture
def aaa_store(store: Store, tmp_path: Path) -> Store:
    root = tmp_path / "evil"
    root.mkdir()
    lines = [f"2026-11-10T00:00:{i:02d}Z ERROR " + "a" * 40 + "!" for i in range(5)]
    (root / "x.log").write_text("\n".join(lines) + "\n")
    rep = ingest_all(store, [SourceConfig(path=str(root))])
    assert not rep.errors
    return store


@pytest.mark.parametrize("pattern", ["(a+)+b", "(a*)*$", "(?:a|b+)+c", "((ab)+c?)+d"])
def test_nested_unbounded_repeats_are_rejected_up_front(pattern: str) -> None:
    with pytest.raises(saferegex.UnsafePattern) as exc:
        saferegex.check_pattern(pattern)
    assert "repeated group" in str(exc.value)


@pytest.mark.parametrize("pattern", [r"\bERROR\b", "a{1,5}b", "(ab){2,3}", "x+y+", "(a|b)*c"])
def test_ordinary_patterns_pass(pattern: str) -> None:
    saferegex.check_pattern(pattern)


def test_invalid_pattern_is_reported() -> None:
    with pytest.raises(saferegex.UnsafePattern, match="invalid regex"):
        saferegex.check_pattern("([")


def test_search_honours_the_deadline() -> None:
    with saferegex.deadline(0.2), pytest.raises(saferegex.RegexTimeout):
        saferegex.search("(a|aa)+b", "a" * 60 + "!")
    assert not saferegex.timed_out()  # the flag is scoped to the deadline block


@pytest.mark.parametrize("fn", ["FIND", "LINES"])
@pytest.mark.parametrize("pattern", ["(a|aa)+b", "(a|a)*b"])
def test_overlapping_alternation_cannot_hang_a_query(
    aaa_store: Store, fn: str, pattern: str, monkeypatch: pytest.MonkeyPatch
) -> None:
    # (a|aa)+ is not "nested repetition", so only the real timeout can stop it
    monkeypatch.setattr(saferegex, "DEFAULT_TIMEOUT_S", 0.5)
    formula = f'ANSWER(COUNT({fn}(src:"*x.log", re:"{pattern}")))'
    t0 = time.monotonic()
    out = evaluate_formula(aaa_store, formula, {}, question="How many?")
    assert time.monotonic() - t0 < 10
    if out.status == "error":
        assert out.problems[0].code == "V1" and "took longer" in out.problems[0].message
    else:  # the regex engine finished on its own
        assert out.cells[0]["value"]["decimal"] == "0"


def test_slow_pattern_times_out_as_v1(aaa_store: Store, monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(saferegex, "DEFAULT_TIMEOUT_S", 0.3)
    out = evaluate_formula(aaa_store, 'ANSWER(COUNT(FIND(src:"*x.log", re:"(a|aa)+b")))', {})
    assert out.status == "error", out.cells
    assert out.problems[0].code == "V1" and "took longer than 0.3s" in out.problems[0].message


def test_catastrophic_pattern_via_formula_is_a_v1_error(aaa_store: Store) -> None:
    out = evaluate_formula(aaa_store, 'ANSWER(COUNT(FIND(src:"*x.log", re:"(a+)+b")))', {})
    assert out.status == "error" and out.problems[0].code == "V1"
    assert out.problems[0].hint and "bound it" in out.problems[0].hint
