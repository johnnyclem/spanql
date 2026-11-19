from __future__ import annotations

from spanql.services import Services

from .helpers import Q3, line_span


def test_status(services: Services) -> None:
    st = services.status()
    assert st["store"]["sources"] == 7
    assert st["model"]["backend"] == "fake"


def test_conversation_keeps_history(services: Services) -> None:
    services.ask("When was the Q3 report published?", conversation_id="c1")
    services.ask("What was our Q3 churn rate?", conversation_id="c1")
    conv = services.conversations["c1"]
    assert len(conv["history"]) == 4


def test_evaluate_with_span_ids(services: Services) -> None:
    sid = line_span(services.store, *Q3)
    out = services.evaluate('ANSWER(MONEY($1."$4.82 million"))', {"1": sid})
    assert out.status == "grounded"


def test_recompute_after_edit(services: Services, demo_dir) -> None:
    sid = line_span(services.store, *Q3)
    out = services.evaluate('ANSWER(MONEY($1."$4.82 million"))', {"1": sid})
    cid = out.cells[0]["id"]
    # unrelated edit: the quote moves but is unchanged -> recompute succeeds, same value
    f = demo_dir / "reports" / "q3-2026.md"
    f.write_text("Preface line.\n\n" + f.read_text())
    services.ingest()
    assert services.staleness(services.store.get_cell(cid))["stale"] is True
    rec = services.recompute(cid)
    assert rec["status"] == "grounded"
    assert rec["cells"][0]["value"]["display"] == "$4,820,000"
    # edit the number: re-anchor reports changed -> stale, not silently recomputed
    f.write_text(f.read_text().replace("$4.82 million", "$4.95 million"))
    services.ingest()
    rec2 = services.recompute(cid)
    assert rec2["status"] in ("stale", "error")


def test_search(services: Services) -> None:
    r = services.search("invoice West", k=3)
    assert 0 < len(r.handles) <= 3
