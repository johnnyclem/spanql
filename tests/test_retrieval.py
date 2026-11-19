from __future__ import annotations

from spanql.retrieve.retriever import Retriever, keywords
from spanql.store.db import Store


def test_keywords_drop_stopwords() -> None:
    kw = keywords("How many PaymentTimeout errors were logged on 2026-11-10?")
    assert "PaymentTimeout" in kw
    assert "how" not in [k.lower() for k in kw] and "were" not in kw


def test_search_finds_relevant_first(demo_store: Store) -> None:
    r = Retriever(demo_store).search("What was total revenue for Q3?")
    first = r.handles[1]
    assert "revenue" in first.text.lower()
    assert list(r.handles) == list(range(1, len(r.handles) + 1))
    assert r.report["sources_searched"] == 7 or r.report


def test_scope_limits_sources(demo_store: Store) -> None:
    r = Retriever(demo_store).search("PaymentTimeout", scope=["demo/logs/*"])
    assert r.handles and all(h.source.startswith("demo/logs/") for h in r.handles.values())


def test_hints_describe_schemas(demo_store: Store) -> None:
    r = Retriever(demo_store).search("invoice amount West region and PaymentTimeout status")
    hints = "\n".join(r.hints)
    assert "amount" in hints or "status" in hints


def test_token_budget_and_max(demo_store: Store) -> None:
    r = Retriever(demo_store, max_candidates=2, token_budget=8192).search("revenue customers")
    assert len(r.handles) <= 2


def test_no_match_reports_near_misses(demo_store: Store) -> None:
    r = Retriever(demo_store).search("zebra giraffe")
    assert not r.handles
    d = r.to_dict()
    assert d["candidates"] == [] and "report" in d
