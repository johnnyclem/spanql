from __future__ import annotations

import json
import re
import threading
from datetime import date
from http.server import BaseHTTPRequestHandler, HTTPServer

import pytest

from spanql.engine import Engine, HandleTable
from spanql.errors import LiteralError
from spanql.formula.parser import parse_formula
from spanql.formula.typecheck import typecheck
from spanql.harness import (ClientError, Harness, HarnessConfig, HarnessError, OpenAICompatClient, ScriptedClient,
                            evaluate_formula, extract_formula, list_runs)
from spanql.harness.prompts import EXAMPLE_FORMULAS, SYSTEM
from spanql.ingest import ingest_path
from spanql.retrieve import date_hints, extract_terms, retrieve
from spanql.store import Store
from spanql.verify import check_literals
from spanql.verify.literals import check_prose

TODAY = date(2026, 11, 17)  # a Tuesday


# ------------------------------------------------------------- helpers


def handle_for(messages, needle: str) -> int:
    """Find the handle the retriever assigned to a passage (what a real model reads in its prompt)."""
    prompt = next(m["content"] for m in messages if m["role"] == "user")
    for line in prompt.splitlines():
        m = re.match(r"\[\$(\d+)\] ", line)
        if m and needle in line:
            return int(m.group(1))
    raise AssertionError(f"no candidate contains {needle!r}:\n{prompt}")


def revenue_formula(messages, q3_quote='$4.82 million', q2_quote='$4.31M') -> str:
    a, b = handle_for(messages, "Total revenue for the quarter"), handle_for(messages, "Q2 revenue")
    return (f'ANSWER(DIFF(MONEY(${a}."{q3_quote}"), MONEY(${b}."{q2_quote}")), '
            f'PCT(DIFF(MONEY(${a}."{q3_quote}"), MONEY(${b}."{q2_quote}")), MONEY(${b}."{q2_quote}"), places:1), '
            f'text:"Revenue grew by {{0}} ({{1}}) from Q2 to Q3.")')


@pytest.fixture
def harness_for(ingested):
    def make(responses, **cfg):
        client = ScriptedClient(responses)
        cfg.setdefault("relevance_check", False)
        return Harness(ingested, client, HarnessConfig(**cfg)), client
    return make


Q = "How much did revenue grow from Q2 to Q3?"


# ---------------------------------------------------------------- V4


@pytest.mark.parametrize(
    "formula, question",
    [
        ('ANSWER(MONEY($1."$4.82 million"), text:"Q3 revenue was {0}.")', "What was Q3 revenue?"),
        ('PCT(DIFF(MONEY($1."$1"), MONEY($2."$2")), MONEY($2."$2"), places:1)', None),
        ('COUNT(FIND(src:"logs/api/*.log", FIELD("status") >= 500))', "5xx"),  # filter literal: allowed in queries
        ('COUNT(FIND(src:"logs/*", re:"ERROR 42", day:"2026-11-10"))', ""),  # strings in queries
        ('GT(NUM($1."5"), 100)', "was it more than 100?"),  # literal echoed from the question
        ('DATE($1."Nov 10", year:2026)', "in 2026?"),
        ('ANSWER(NUM($1."5"), text:"Headcount in Q3 and FY2026 was {0} (p95).")', "q"),  # labels glued to letters
        ('ANSWER(NUM($1."5"), text:"In 2026 headcount was {0}.")', "What was headcount in 2026?"),
    ],
)
def test_v4_allows(formula, question):
    check_literals(parse_formula(formula), formula, question)


@pytest.mark.parametrize(
    "formula, question, fragment",
    [
        ('NUM($1."5") + 100', "total?", "number 100"),
        ('ANSWER(NUM($1."5"), text:"Revenue was 4.9 million.")', "q", "contains the number"),
        ('ANSWER(NUM($1."5"), text:"Up 12% in 2026.")', "How much?", "'12'"),
        ('IF(GT(NUM($1."5"), NUM($2."6")), "up", "down")', "q", "used as a value"),
        ('ANSWER(5)', "q", "not derived from any source"),
        ('ANSWER("hello")', "q", "not derived from any source"),
        ('GT(NUM($1."5"), 100)', "was it high?", "number 100"),
        ('ANSWER(NUM($1."5") * 3)', "q", "number 3"),
        ('NOT_FOUND("Revenue was 4.9 million", searched:[$1])', "q", "explanation"),
    ],
)
def test_v4_rejects(formula, question, fragment):
    with pytest.raises(LiteralError) as e:
        check_literals(parse_formula(formula), formula, question)
    assert fragment in " | ".join(e.value.problems), e.value.problems
    assert e.value.check == "V4"


def test_v4_reports_every_violation_at_once():
    f = 'ANSWER(NUM($1."5") + 7, NUM($1."5") * 9, text:"was 4.9")'
    with pytest.raises(LiteralError) as e:
        check_literals(parse_formula(f), f, "q")
    assert len(e.value.problems) == 3


def test_prose_digit_rules():
    assert check_prose("Q3 revenue grew in FY2026", "x", "t") == []
    assert check_prose("grew in 2026", "what about 2026?", "t") == []
    assert check_prose("grew in 2026", "what about 2025?", "t")
    assert check_prose("It was 1,200 units", "q", "t")


# ---------------------------------------------------------- retrieval


def test_retrieval_finds_both_reports_and_numbers_handles(ingested):
    r = retrieve(ingested, Q, today=TODAY)
    uris = [c.uri for c in r.candidates]
    assert "reports/q3-2026.md" in uris and "reports/q2-2026.md" in uris
    assert [c.handle for c in r.candidates] == list(range(1, len(r.candidates) + 1))
    assert set(r.handles.spans) == {c.handle for c in r.candidates}
    c = next(c for c in r.candidates if "Total revenue" in c.excerpt)
    assert ingested.span_text(c.span).startswith("Total revenue for the quarter")
    assert r.terms[:3] == ["revenue", "grow", "q2"] and r.sources_searched == 9
    assert r.catalog and any(d["kind"] == "log" for d in r.catalog)
    assert r.report()["blocks_searched"] > 0


def test_retrieval_scope_symbols_rows_and_budget(ingested):
    r = retrieve(ingested, Q, scope=["src:reports/q3*"], today=TODAY)
    assert {c.uri for c in r.candidates} == {"reports/q3-2026.md"} and r.sources_searched == 1
    r = retrieve(ingested, "Which code calls `retry_with_backoff`?", today=TODAY)
    assert any(c.kind in ("function", "method") and c.uri.endswith("retry.py") for c in r.candidates)
    r = retrieve(ingested, "What are the units for the EMEA widget?", today=TODAY)
    row = next(c for c in r.candidates if c.kind == "table_row")
    assert "region=EMEA" in row.excerpt and "product=widget" in row.excerpt  # headers travel with the row
    tiny = retrieve(ingested, "revenue payments timeout errors", token_budget=40, today=TODAY)
    assert len(tiny.candidates) < len(retrieve(ingested, "revenue payments timeout errors", today=TODAY).candidates)
    assert tiny.near_misses  # ranked hits that did not fit are kept for refusals


def test_retrieval_no_hits_and_overlap_dedup(ingested):
    r = retrieve(ingested, "zzzz qqqq xxxx", today=TODAY)
    assert r.candidates == [] and r.report()["candidates"] == 0
    r = retrieve(ingested, "retry_with_backoff attempts", today=TODAY)
    spans = [(c.span.snapshot_id, c.span.start, c.span.end) for c in r.candidates]
    for i, (s1, a1, b1) in enumerate(spans):  # no candidate overlaps another
        for s2, a2, b2 in spans[i + 1:]:
            assert not (s1 == s2 and a1 < b2 and a2 < b1)


def test_date_hints_and_terms():
    h = date_hints("errors last Tuesday and yesterday, also on 2026-11-01", TODAY)
    assert h == {"yesterday": "2026-11-16", "last tuesday": "2026-11-10", "2026-11-01": "2026-11-01"}
    assert date_hints("last monday", TODAY) == {"last monday": "2026-11-16"}
    assert extract_terms("How many of the payment timeouts did we log?") == ["payment", "timeouts", "log"]


# ------------------------------------------------------------- harness


def test_grounded_answer_end_to_end(harness_for, ingested):
    h, client = harness_for([lambda m, g: revenue_formula(m)])
    res = h.ask(Q, today=TODAY)
    assert res.status == "grounded" and res.attempts == 1
    assert res.text == "Revenue grew by $510,000 (11.8%) from Q2 to Q3."
    assert len(res.cells) == 2 and res.cells[0]["model"] == {"id": "scripted", "formula_attempt": 1}
    # cells and the run are persisted and linked
    assert ingested.get_cell(res.cells[1]["id"])["display"] == "11.8%"
    assert [c["id"] for c in ingested.cells_for_question(res.run_id)] == [c["id"] for c in res.cells]
    run = list_runs(ingested)[0]
    assert run["id"] == res.run_id and run["status"] == "grounded" and run["question"] == Q
    rec = json.loads(ingested.db.execute("SELECT record FROM run WHERE id=?", (res.run_id,)).fetchone()[0])
    assert rec["attempts"][0]["formula"].startswith("ANSWER(") and rec["retrieval"]["candidates_full"]
    # the model saw a grammar restricted to exactly the offered handles, and the DSL description
    call = client.calls[0]
    n = len(rec["retrieval"]["candidates_full"])
    assert f'"{n}"' in call["grammar"] and f'"{n + 1}"' not in call["grammar"]
    assert call["messages"][0]["content"] == SYSTEM


def test_prompt_carries_date_hints_sources_and_schema(harness_for):
    h, client = harness_for(['NOT_FOUND("nothing relevant", searched:[$1])'])
    h.ask("How many errors were logged last Tuesday?", today=TODAY)
    prompt = client.calls[0]["messages"][1]["content"]
    assert "Today is 2026-11-17 (Tuesday)" in prompt and '"last tuesday" = 2026-11-10' in prompt
    assert "logs/payments-2026-11-10.log [log]" in prompt and "levels ERROR:4" in prompt and "user=" in prompt
    assert "data/sales.csv [file]" in prompt and "columns: region=" in prompt


def test_retry_feeds_back_the_exact_verifier_error(harness_for):
    h, client = harness_for([lambda m, g: revenue_formula(m, q3_quote="$4.8 million"),
                             lambda m, g: revenue_formula(m)])
    res = h.ask(Q, today=TODAY)
    assert res.status == "grounded" and res.attempts == 2
    retry_prompt = client.calls[1]["messages"][-1]["content"]
    assert "[V2]" in retry_prompt and "not found in $" in retry_prompt and '"$4.82 million"' in retry_prompt
    assert client.calls[1]["messages"][-2]["role"] == "assistant"  # the failed formula is shown back
    assert res.errors[0]["check"] == "V2"


def test_smuggled_literal_is_rejected_then_corrected(harness_for):
    def smuggled(m, g):
        a = handle_for(m, "Total revenue for the quarter")
        return f'ANSWER(MONEY(${a}."$4.82 million"), text:"Revenue was 4.9 million.")'

    def good(m, g):
        a = handle_for(m, "Total revenue for the quarter")
        return f'ANSWER(MONEY(${a}."$4.82 million"), text:"Q3 revenue was {{0}}.")'

    h, client = harness_for([smuggled, good])
    res = h.ask("What was Q3 revenue?", today=TODAY)
    assert res.status == "grounded" and res.text == "Q3 revenue was $4,820,000." and res.attempts == 2
    assert "[V4]" in client.calls[1]["messages"][-1]["content"]


def test_model_refusal_is_a_first_class_answer_with_a_search_report(harness_for):
    h, _ = harness_for(['NOT_FOUND("No churn or retention figure appears in the candidates.", searched:[$1])'])
    res = h.ask("What was our Q3 churn rate?", today=TODAY)
    assert res.status == "not_found" and res.cells == [] and res.attempts == 1
    assert res.text.startswith("I couldn't find this in your sources. I searched 9 source(s)")
    assert "churn" in res.search_report["terms"] and res.search_report["sources_searched"] == 9
    assert "No churn or retention figure" in res.text and "Closest passages:" in res.text


def test_retry_budget_exhausted_refuses_instead_of_guessing(harness_for):
    h, client = harness_for(["I think the answer is about 600k.", 'ANSWER(NUM($99))', 'ANSWER(MONEY($1."zzz"))'])
    res = h.ask(Q, today=TODAY)
    assert res.status == "not_found" and res.attempts == 3 and len(client.calls) == 3
    assert "No formula that passes verification" in res.text
    assert [e["check"] for e in res.errors] == ["V1", "V2", "V2"]
    assert len(res.search_report["attempt_errors"]) == 3
    assert "600k" not in res.text  # chatter never reaches the user


def test_ambiguous_lists_the_conflicting_sources(harness_for):
    def amb(m, g):
        return f'AMBIGUOUS("Two revenue figures.", [${handle_for(m, "Total revenue for the quarter")}, ${handle_for(m, "Q2 revenue")}])'

    h, _ = harness_for([amb])
    res = h.ask("What was revenue?", today=TODAY)
    assert res.status == "ambiguous" and len(res.conflicts) == 2
    assert {c["source"] for c in res.conflicts} == {"reports/q3-2026.md", "reports/q2-2026.md"}
    assert "Two revenue figures." in res.text and "$4.82 million" in res.text


def test_relevance_check_rejects_an_off_topic_citation(harness_for):
    def two_cites(m, g):
        return revenue_formula(m)

    # formula cites two candidates -> two yes/no calls; the second says "no" -> V6 -> retry -> NOT_FOUND
    h, client = harness_for([two_cites, "yes", "no", 'NOT_FOUND("The Q2 figure is not a revenue statement.", searched:[$1])'],
                            relevance_check=True)
    res = h.ask(Q, today=TODAY)
    assert res.status == "not_found" and res.attempts == 2
    assert res.errors[0]["check"] == "V6" and "does not appear to be about" in client.calls[3]["messages"][-1]["content"]
    assert client.calls[1]["grammar"].strip() == 'root ::= "yes" | "no"'
    # and when the relevance model agrees, the answer stands
    h, _ = harness_for([two_cites, "yes", "yes"], relevance_check=True)
    assert h.ask(Q, today=TODAY).status == "grounded"


def test_query_only_answers_need_no_candidates(harness_for):
    f = ('ANSWER(COUNT(FIND(src:"logs/payments-*.log", re:"PaymentTimeout", day:"2026-11-10")), '
         'COUNT(FIND(src:"logs/payments-*.log", level:"ERROR", day:"2026-11-10")), PCT([0], [1], places:1), '
         'text:"There were {0} payment timeouts out of {1} errors, which is {2}.")')
    h, _ = harness_for([f])
    res = h.ask("How many payment timeouts did we log last Tuesday, and what % of all errors is that?", today=TODAY)
    assert res.status == "grounded"
    assert res.text == "There were 2 payment timeouts out of 3 errors, which is 66.7%."
    assert res.cells[2]["inputs"][0]["kind"] == "cell"


def test_code_fenced_replies_are_accepted(harness_for):
    h, _ = harness_for(['Sure! Here you go:\n```\nANSWER(COUNT(FIND(src:"logs/jobs.jsonl", level:"error")), text:"{0} job failures.")\n```'])
    res = h.ask("How many job failures?", today=TODAY)
    assert res.status == "grounded" and res.text == "2 job failures."


def test_no_sources_and_no_model(store):
    c = ScriptedClient([])
    res = Harness(store, c).ask("anything?")
    assert res.status == "not_found" and "nothing has been ingested" in res.text and c.calls == []
    store2 = Store(":memory:")
    ingest_path(store2, __file__)
    with pytest.raises(HarnessError, match="no model is configured"):
        Harness(store2, None).ask("anything?")
    with pytest.raises(HarnessError):
        Harness(store2, ScriptedClient([])).ask("  ")


def test_model_outage_is_a_harness_error_not_a_wrong_answer(harness_for):
    h, _ = harness_for([])  # ScriptedClient raises ClientError when it has no response
    with pytest.raises(HarnessError):
        h.ask(Q, today=TODAY)


def test_evaluate_formula_power_entry(ingested):
    src = ingested.get_source("reports/q3-2026.md")
    text = ingested.snapshot_bytes(src.current_snapshot)
    span = ingested.make_span(src.current_snapshot, text.index(b"Total revenue"), text.index(b"in Q2.") + 6)
    ev = evaluate_formula(ingested, 'MONEY($1."$4.82 million")', {"$1": span.id})
    assert ev.cells[0].display == "$4,820,000" and ingested.get_cell(ev.cells[0].id)
    with pytest.raises(LiteralError):
        evaluate_formula(ingested, 'MONEY($1."$4.82 million") + 5', {"$1": span.id})
    assert evaluate_formula(ingested, 'MONEY($1."$4.82 million") + MONEY($1."$4.31 million")', {"$1": span.id}).cells[0].display == "$9,130,000"
    assert evaluate_formula(ingested, 'NUM($1."4.82 million") + 5', {"$1": span.id}, verify=False).cells[0].display == "4,820,005"


def test_every_prompt_example_is_a_valid_formula():
    for f in EXAMPLE_FORMULAS:
        typecheck(parse_formula(f))
        assert f.split("(")[0] + "(" in SYSTEM  # the example is actually shown to the model
    assert all(ex in SYSTEM for ex in EXAMPLE_FORMULAS)


def test_prompt_examples_run_against_the_corpus(ingested):
    """The examples that name corpus paths must evaluate, so the prompt never teaches something broken."""
    e = Engine(ingested)
    for f in EXAMPLE_FORMULAS:
        if "$" in f.replace("$4.82", "").replace("$4.31", "") and "src:" not in f:
            continue  # needs handles
        if "$" in re.sub(r'"[^"]*"', "", f):
            continue
        ev = e.evaluate(f, HandleTable({}))
        assert ev.kind == "answer" and ev.render()


def test_extract_formula():
    assert extract_formula('  ANSWER(NUM($1))  ') == "ANSWER(NUM($1))"
    assert extract_formula("```text\nNOT_FOUND(\"x\")\n```") == 'NOT_FOUND("x")'
    assert extract_formula("The answer: AMBIGUOUS(\"a\", [$1])") == 'AMBIGUOUS("a", [$1])'
    assert extract_formula("no formula here") == "no formula here"


# ------------------------------------------------------- HTTP client


class _Server:
    def __init__(self, status=200, body=None):
        outer = self
        self.requests: list[dict] = []
        self.status, self.body = status, body if body is not None else {"choices": [{"message": {"content": "ok"}}]}

        class H(BaseHTTPRequestHandler):
            def do_POST(self):
                n = int(self.headers["Content-Length"])
                outer.requests.append({"path": self.path, "body": json.loads(self.rfile.read(n)),
                                       "auth": self.headers.get("Authorization"), "ua": self.headers.get("User-Agent")})
                data = json.dumps(outer.body).encode()
                self.send_response(outer.status)
                self.send_header("Content-Length", str(len(data)))
                self.end_headers()
                self.wfile.write(data)

            def log_message(self, *a):
                pass

        self.httpd = HTTPServer(("127.0.0.1", 0), H)
        self.url = f"http://127.0.0.1:{self.httpd.server_port}/v1"
        threading.Thread(target=self.httpd.serve_forever, daemon=True).start()

    def close(self):
        self.httpd.shutdown()


def test_openai_compat_client_sends_grammar_and_nothing_personal():
    srv = _Server()
    try:
        c = OpenAICompatClient(srv.url, "qwen", api_key="k")
        assert c.complete([{"role": "user", "content": "hi"}], grammar="root ::= \"x\"") == "ok"
        req = srv.requests[0]
        assert req["path"] == "/v1/chat/completions" and req["auth"] == "Bearer k"
        assert req["body"]["grammar"] == 'root ::= "x"' and req["body"]["temperature"] == 0
        assert req["body"]["chat_template_kwargs"] == {"enable_thinking": False}
        assert req["ua"] == "spanql"  # generic client name only
        # other backends get no grammar field (the verifier covers them)
        c2 = OpenAICompatClient(srv.url, "m", backend="openai-compatible")
        c2.complete([{"role": "user", "content": "hi"}], grammar="g")
        assert "grammar" not in srv.requests[1]["body"] and "chat_template_kwargs" not in srv.requests[1]["body"]
    finally:
        srv.close()


def test_openai_compat_client_errors():
    for status, body in ((500, {"error": "boom"}), (200, {"nope": 1})):
        srv = _Server(status, body)
        try:
            with pytest.raises(ClientError):
                OpenAICompatClient(srv.url).complete([{"role": "user", "content": "x"}])
        finally:
            srv.close()
    with pytest.raises(ClientError, match="cannot reach"):
        OpenAICompatClient("http://127.0.0.1:9/v1", timeout=1).complete([{"role": "user", "content": "x"}])
