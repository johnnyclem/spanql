"""Live tests against a real model server. Opt-in:

    SPANQL_LIVE_ENDPOINT=http://127.0.0.1:7718/v1 uv run pytest -m live

Quality assertions are deliberately limited to what the verifier guarantees: every answer is
either grounded in a resolvable span/query or a refusal. Accuracy is the M4 eval's job.
"""

from __future__ import annotations

import os

import pytest

from spanql.config import HarnessConfig, ModelConfig
from spanql.harness.backends import make_backend
from spanql.harness.loop import Harness
from spanql.store.db import Store

ENDPOINT = os.environ.get("SPANQL_LIVE_ENDPOINT")
pytestmark = [
    pytest.mark.live,
    pytest.mark.skipif(not ENDPOINT, reason="set SPANQL_LIVE_ENDPOINT to run live tests"),
]

QUESTIONS = [
    "What was total revenue for the quarter in Q3?",
    "How many PaymentTimeout errors were logged on 2026-11-10?",
    "What was our Q3 churn rate?",
]


@pytest.fixture(autouse=True)
def _keep_endpoint(monkeypatch: pytest.MonkeyPatch) -> None:
    if ENDPOINT:
        monkeypatch.setenv("SPANQL_LIVE_ENDPOINT", ENDPOINT)


@pytest.mark.parametrize("question", QUESTIONS)
def test_live_answers_are_grounded_or_refused(demo_store: Store, question: str) -> None:
    be = make_backend(
        ModelConfig(
            backend=os.environ.get("SPANQL_LIVE_BACKEND", "llama.cpp"), endpoint=ENDPOINT or ""
        )
    )
    res = Harness(demo_store, be, HarnessConfig()).ask(question)
    assert res.status in ("grounded", "not_found", "ambiguous")
    for cell in res.cells:
        for inp in cell["inputs"]:
            if inp["kind"] == "span":
                assert demo_store.resolve(inp.get("consumed_span") or inp["id"]).text
    if "churn" in question:
        assert res.status == "not_found"
