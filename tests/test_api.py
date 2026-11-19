from __future__ import annotations

import json
import time

import pytest
from starlette.testclient import TestClient

from spanql.api.app import create_app
from spanql.services import Services

from .helpers import Q3, line_span


@pytest.fixture
def client(services: Services) -> TestClient:
    return TestClient(create_app(services))


def test_health_status_ui(client: TestClient) -> None:
    assert client.get("/v1/health").json()["ok"] is True
    assert client.get("/v1/status").json()["store"]["sources"] == 7
    r = client.get("/")
    assert r.status_code == 200 and "spanql" in r.text.lower()


def test_ask_json(client: TestClient) -> None:
    r = client.post("/v1/ask", json={"question": "When was the Q3 report published?"})
    body = r.json()
    assert r.status_code == 200
    assert body["status"] == "grounded"
    assert "2026-11-03" in body["answer"]["text"]
    cid = body["answer"]["cells"][0]
    cell = client.get(f"/v1/cells/{cid}").json()
    assert cell["formula"].startswith("DATE(")
    trace = client.get(f"/v1/cells/{cid}/trace").json()
    assert trace["inputs"]
    span = next(i for i in trace["inputs"] if i["kind"] == "span")
    got = client.get(f"/v1/spans/{span['id']}").json()
    assert "November 3, 2026" in got["text"]
    assert client.get("/v1/runs").json()


def test_ask_sse_stream(client: TestClient) -> None:
    with client.stream(
        "POST", "/v1/ask?stream=1", json={"question": "What was our Q3 churn rate?"}
    ) as r:
        text = "".join(r.iter_text())
    events = [ln.split(": ", 1)[1] for ln in text.splitlines() if ln.startswith("event: ")]
    assert events[0] == "retrieval" and events[-1] == "answer"


def test_ask_validation(client: TestClient) -> None:
    assert client.post("/v1/ask", json={}).status_code == 400
    assert client.post("/v1/ask", content=b"not json").status_code == 400


def test_evaluate_and_search(client: TestClient, services: Services) -> None:
    sid = line_span(services.store, *Q3)
    r = client.post(
        "/v1/evaluate", json={"formula": 'ANSWER(MONEY($1."$4.82 million"))', "handles": {"1": sid}}
    )
    assert r.status_code == 200 and r.json()["answer"] == "$4,820,000"
    bad = client.post("/v1/evaluate", json={"formula": "ANSWER(42000)"})
    assert bad.status_code == 422
    s = client.post("/v1/search", json={"query": "PaymentTimeout"}).json()
    assert s["candidates"]


def test_openai_chat_shim(client: TestClient) -> None:
    r = client.post(
        "/v1/chat/completions",
        json={
            "model": "spanql",
            "messages": [{"role": "user", "content": "When was the Q3 report published?"}],
        },
    )
    body = r.json()
    assert body["object"] == "chat.completion"
    assert "2026-11-03" in body["choices"][0]["message"]["content"]
    assert body["spanql"]["status"] == "grounded"
    models = client.get("/v1/models").json()
    assert any(m["id"] == "spanql" for m in models["data"])


def test_openai_chat_stream(client: TestClient) -> None:
    with client.stream(
        "POST",
        "/v1/chat/completions",
        json={
            "stream": True,
            "messages": [{"role": "user", "content": "What was our Q3 churn rate?"}],
        },
    ) as r:
        lines = [ln for ln in r.iter_lines() if ln.startswith("data: ")]
    assert lines[-1] == "data: [DONE]"
    first = json.loads(lines[0][6:])
    assert first["object"] == "chat.completion.chunk"


def test_sources_and_ingest_job(client: TestClient, tmp_path) -> None:
    d = tmp_path / "more"
    d.mkdir()
    (d / "x.md").write_text("Widget count is 42.\n")
    r = client.post("/v1/sources", json={"path": str(d)})
    assert r.status_code == 202
    job = r.json()["job_id"]
    for _ in range(100):
        st = client.get(f"/v1/jobs/{job}").json()
        if st["status"] in ("done", "error"):
            break
        time.sleep(0.05)
    assert st["status"] == "done", st
    uris = [s["uri"] for s in client.get("/v1/sources").json()["sources"]]
    assert "more/x.md" in uris
    assert client.get("/v1/jobs/nope").status_code == 404


def test_metrics(client: TestClient) -> None:
    client.post("/v1/ask", json={"question": "What was our Q3 churn rate?"})
    text = client.get("/metrics").text
    assert 'spanql_asks_total{status="not_found"} 1' in text


def test_token_auth(services: Services) -> None:
    services.config.server.api_token = "s3cret"
    c = TestClient(create_app(services))
    assert c.get("/v1/health").status_code == 200
    assert c.get("/v1/status").status_code == 401
    assert c.get("/v1/status", headers={"Authorization": "Bearer wrong"}).status_code == 401
    assert c.get("/v1/status", headers={"Authorization": "Bearer s3cret"}).status_code == 200


def test_unknown_cell_and_span(client: TestClient) -> None:
    assert client.get("/v1/cells/c_nope").status_code == 404
    assert client.get("/v1/spans/sp:zz").status_code in (400, 404)


def test_backend_outage_is_503_not_a_refusal(services: Services) -> None:
    from spanql.harness.backends import BackendError, ScriptedBackend

    class Down(ScriptedBackend):
        def generate(self, *a, **kw):
            raise BackendError("model endpoint unreachable (test)")

    services.harness.backend = Down([])
    client = TestClient(create_app(services))
    r = client.post("/v1/ask", json={"question": "What was Q3 revenue?"})
    assert r.status_code == 503
    assert r.json()["status"] == "error" and "unreachable" in r.json()["error"]
    r = client.post("/v1/chat/completions", json={"messages": [{"role": "user", "content": "Q3?"}]})
    assert r.status_code == 503 and r.json()["error"]["type"] == "backend_error"
