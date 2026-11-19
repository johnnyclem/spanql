from __future__ import annotations

from spanql.config import HarnessConfig
from spanql.harness.backends import BackendError, FakeBackend, ScriptedBackend
from spanql.harness.loop import Harness
from spanql.store.db import Store


def find(handles, needle: str) -> int:
    return next(n for n, h in handles.items() if needle in h.text)


def revenue(question, handles, hints) -> str:
    n = find(handles, "$4.82 million")
    return f'ANSWER(MONEY(${n}."$4.82 million"), text:"Q3 revenue was {{0}}.")'


def test_grounded_first_try(demo_store: Store) -> None:
    be = ScriptedBackend([revenue])
    res = Harness(demo_store, be).ask("What was total revenue for the quarter in Q3?")
    assert res.status == "grounded"
    assert res.answer == "Q3 revenue was $4,820,000."
    assert len(res.attempts) == 1
    assert be.calls[0]["grammar"] and '"$" (' in be.calls[0]["grammar"]
    assert demo_store.get_cell(res.cells[0]["id"]) is not None
    assert demo_store.get_run(res.run_id)["status"] == "grounded"


def test_retry_with_verifier_feedback(demo_store: Store) -> None:
    be = ScriptedBackend(['ANSWER(MONEY($1."$9.99 trillion"))', revenue])
    res = Harness(demo_store, be).ask("What was total revenue for the quarter in Q3?")
    assert res.status == "grounded"
    assert [a.status for a in res.attempts] == ["error", "grounded"]
    retry_prompt = be.calls[1]["messages"][-1]["content"]
    assert "V2" in retry_prompt or "$9.99 trillion" in retry_prompt


def test_harness_refusal_after_max_attempts(demo_store: Store) -> None:
    be = ScriptedBackend(["ANSWER(4820000)"] * 3)
    res = Harness(demo_store, be, HarnessConfig(max_attempts=3)).ask("What was Q3 revenue?")
    assert res.status == "not_found"
    assert res.refusal_by == "harness"
    assert len(res.attempts) == 3
    assert "couldn't find" in res.answer
    assert res.cells == []


def test_model_refusal(demo_store: Store) -> None:
    be = ScriptedBackend(['NOT_FOUND("no churn", searched:[$1])'])
    res = Harness(demo_store, be).ask("What was Q3 churn?")
    assert res.status == "not_found" and res.refusal_by == "model"
    assert res.reason == "no churn"


def test_ambiguous(demo_store: Store) -> None:
    be = ScriptedBackend(['AMBIGUOUS("two figures", [$1, $2])'])
    res = Harness(demo_store, be).ask("What was revenue?")
    assert res.status == "ambiguous" and len(res.conflicting) == 2


def test_widening_on_v2(demo_store: Store) -> None:
    be = ScriptedBackend(['ANSWER(MONEY($1."nope"))', revenue])
    h = Harness(demo_store, be, HarnessConfig(max_candidates=2))
    res = h.ask("What was total revenue for the quarter in Q3?")
    assert len(be.calls[1]["handles"]) >= len(be.calls[0]["handles"])
    assert res.status == "grounded"


def test_backend_error_is_reported(demo_store: Store) -> None:
    class Down(ScriptedBackend):
        def generate(self, *a, **kw):
            raise BackendError("model endpoint unreachable")

    res = Harness(demo_store, Down([])).ask("What was Q3 revenue?")
    assert res.status == "not_found"
    assert "unreachable" in (res.reason or res.answer)


def test_relevance_check_wires_through(demo_store: Store) -> None:
    be = ScriptedBackend([revenue, revenue, revenue], relevant=False)
    res = Harness(demo_store, be, HarnessConfig(relevance_check=True)).ask("What was Q3 revenue?")
    assert res.status == "not_found"
    assert any(p["code"] == "V6" for a in res.attempts for p in a.problems)


def test_events_stream(demo_store: Store) -> None:
    events: list[str] = []
    Harness(demo_store, ScriptedBackend([revenue])).ask(
        "What was total revenue for the quarter in Q3?", on_event=lambda k, d: events.append(k)
    )
    assert events[0] == "retrieval" and "attempt" in events and events[-1] == "answer"


def test_fake_backend_end_to_end(demo_store: Store) -> None:
    h = Harness(demo_store, FakeBackend())
    cases = {
        "How many PaymentTimeout errors were logged on 2026-11-10?": ("grounded", "2"),
        "Which functions call retry_with_backoff?": ("grounded", "charge"),
        "When was the Q3 report published?": ("grounded", "2026-11-03"),
        "What was our Q3 churn rate?": ("not_found", None),
    }
    for q, (status, needle) in cases.items():
        res = h.ask(q)
        assert res.status == status, (q, res.answer, [a.to_dict() for a in res.attempts])
        if needle:
            assert needle in res.answer, (q, res.answer)
