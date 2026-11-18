from __future__ import annotations

import json
import re
import threading
import urllib.error
import urllib.request

import pytest

from conftest import write_corpus
from spanql import cli
from spanql.api import make_server
from spanql.config import Config, ConfigError, load_config
from spanql.harness import ScriptedClient
from spanql.store import Store


@pytest.fixture
def env(tmp_path, monkeypatch):
    """A file-backed store with the corpus ingested via the CLI, isolated from the user's config."""
    monkeypatch.chdir(tmp_path)
    for k in list(__import__("os").environ):
        if k.startswith("SPANQL_"):
            monkeypatch.delenv(k)
    corpus = write_corpus(tmp_path / "corpus")
    db = str(tmp_path / "s.db")
    for sub in ("logs", "reports", "data", "repo"):
        assert cli.main(["--db", db, "ingest", str(corpus / sub)]) == 0
    return {"db": db, "corpus": corpus}


def run(capsys, *argv):
    rc = cli.main(list(argv))
    out = capsys.readouterr()
    return rc, out.out, out.err


# ------------------------------------------------------------------- CLI


def test_ingest_is_idempotent_and_json_output(env, capsys):
    rc, out, _ = run(capsys, "ingest", str(env["corpus"] / "reports"), "--db", env["db"], "--json")
    rep = json.loads(out)
    assert rc == 0 and rep["added"] == [] and rep["unchanged"] == 2
    rc, out, err = run(capsys, "--db", env["db"], "ingest", str(env["corpus"] / "nope"))
    assert rc == 1 and "no such file" in err


def test_sources_status_describe_and_options_in_either_position(env, capsys):
    rc, out, _ = run(capsys, "--db", env["db"], "sources", "logs/*")
    assert rc == 0 and "logs/api/access.log" in out and "3 source(s)" in out
    rc, out, _ = run(capsys, "sources", "--db", env["db"], "--json")
    assert len(json.loads(out)) == 9
    rc, out, _ = run(capsys, "--db", env["db"], "status", "--json")
    st = json.loads(out)
    assert st["sources"] == 9 and st["model"]["backend"] == "llama.cpp"
    rc, out, _ = run(capsys, "--db", env["db"], "describe", "data/sales.csv")
    assert json.loads(out)[0]["columns"]["region"] == "EMEA"


def test_evaluate_read_span_trace_recompute_flow(env, capsys):
    db = env["db"]
    with Store(db) as s:
        src = s.get_source("reports/q3-2026.md")
        data = s.snapshot_bytes(src.current_snapshot)
        span = s.make_span(src.current_snapshot, data.index(b"Total revenue"), data.index(b"in Q2.") + 6)
    rc, out, _ = run(capsys, "--db", db, "read-span", span.id, "--context", "1")
    assert rc == 0 and "reports/q3-2026.md" in out and "Total revenue" in out
    rc, out, _ = run(capsys, "--db", db, "evaluate", 'MONEY($1."$4.82 million")', "--handle", f"$1={span.id}")
    assert rc == 0 and out.splitlines()[0] == "$4,820,000"
    cid = re.search(r"c_[A-Za-z0-9]{6}", out).group(0)
    rc, out, _ = run(capsys, "--db", db, "trace", cid)
    assert rc == 0 and "fresh" in out and "«$4.82 million»" in out and "MONEY read" in out
    rc, out, _ = run(capsys, "--db", db, "recompute", cid)
    assert rc == 0 and "(unchanged)" in out
    # source changes -> trace says STALE; recompute follows the move
    p = env["corpus"] / "reports/q3-2026.md"
    p.write_text("# New heading\n\n" + p.read_text())
    assert cli.main(["--db", db, "ingest", str(env["corpus"] / "reports")]) == 0
    capsys.readouterr()
    rc, out, _ = run(capsys, "--db", db, "trace", cid)
    assert "STALE" in out and "source changed" in out
    rc, out, _ = run(capsys, "--db", db, "recompute", cid)
    assert rc == 0 and out.splitlines()[0].startswith("$4,820,000")
    rc, out, err = run(capsys, "--db", db, "trace", "c_nope00")
    assert rc == 1 and "no cell" in err


def test_evaluate_failures_exit_nonzero_with_feedback(env, capsys):
    rc, out, err = run(capsys, "--db", env["db"], "evaluate", 'COUNT(FIND(src:"nowhere/*"))')
    assert rc == 1 and "[V5]" in err and "matches no ingested source" in err
    rc, out, err = run(capsys, "--db", env["db"], "evaluate", 'SUM($1)')
    assert rc == 1 and "[V1]" in err
    rc, out, err = run(capsys, "--db", env["db"], "evaluate", "NUM($1)", "--handle", "oops")
    assert rc == 2 and "--handle must look like" in err
    # V4: a typed number is rejected unless the question contains it, or verification is disabled
    rc, _, err = run(capsys, "--db", env["db"], "evaluate", 'COUNT(FIND(src:"logs/*", day:"2026-11-10")) + 5')
    assert rc == 1 and "[V4]" in err
    rc, out, _ = run(capsys, "--db", env["db"], "evaluate", 'COUNT(FIND(src:"logs/jobs.jsonl")) + 5', "--no-verify")
    assert rc == 0 and out.startswith("8")


def test_ask_without_a_reachable_model_is_a_clear_error(env, capsys, monkeypatch):
    monkeypatch.setenv("SPANQL_MODEL__ENDPOINT", "http://127.0.0.1:9/v1")
    monkeypatch.setenv("SPANQL_MODEL__TIMEOUT", "1")
    rc, out, err = run(capsys, "--db", env["db"], "ask", "what was revenue?")
    assert rc == 2 and "cannot reach the model endpoint" in err


def test_ask_prints_cells_and_runs_lists_them(env, capsys, monkeypatch):
    monkeypatch.setattr("spanql.config.Config.make_client",
                        lambda self: ScriptedClient(['ANSWER(COUNT(FIND(src:"logs/jobs.jsonl", level:"error")), text:"{0} failures.")']))
    rc, out, _ = run(capsys, "--db", env["db"], "ask", "how many job failures?")
    assert rc == 0 and out.startswith("2 failures.") and "[0] 2" in out and "COUNT(FIND(" in out and "[grounded" in out
    rc, out, _ = run(capsys, "--db", env["db"], "runs")
    assert "grounded" in out and "how many job failures?" in out
    monkeypatch.setattr("spanql.config.Config.make_client",
                        lambda self: ScriptedClient(['NOT_FOUND("Nothing about churn.", searched:[$1])']))
    rc, out, _ = run(capsys, "--db", env["db"], "ask", "churn?", "--json")
    j = json.loads(out)
    assert rc == 0 and j["status"] == "not_found" and j["search_report"]["sources_searched"] == 9  # refusal is exit 0


def test_grammar_gc_and_selftest(env, capsys):
    rc, out, _ = run(capsys, "grammar", "--handles", "3")
    assert rc == 0 and out.startswith("root ::=") and '"3"' in out and '"4"' not in out.split("handle ::=")[1].split("\n")[0]
    rc, out, _ = run(capsys, "--db", env["db"], "gc", "--days", "90")
    assert json.loads(out) == {"snapshots_deleted": 0}
    rc, out, _ = run(capsys, "selftest", "--no-model")
    assert rc == 0 and out.strip() == "ok"


def test_ingest_from_config_sources(env, capsys, tmp_path, monkeypatch):
    cfg = tmp_path / "c.toml"
    cfg.write_text(f'[store]\npath = "{tmp_path / "cfg.db"}"\n\n[[sources]]\npath = "{env["corpus"] / "reports"}"\nname = "wiki"\n')
    rc, out, _ = run(capsys, "--config", str(cfg), "ingest")
    assert rc == 0 and "2 added" in out
    rc, out, _ = run(capsys, "--config", str(cfg), "sources")
    assert "wiki/q3-2026.md" in out
    rc, _, err = run(capsys, "--db", str(tmp_path / "empty.db"), "ingest")
    assert rc == 2 and "nothing to ingest" in err


# ---------------------------------------------------------------- config


def test_config_defaults_file_and_env_overrides(tmp_path):
    cfg = tmp_path / "c.toml"
    cfg.write_text('[model]\nbackend = "ollama"\nendpoint = "http://x:1/v1"\n[harness]\nmax_attempts = 5\n')
    c = load_config(str(cfg), env={"SPANQL_MODEL__MODEL": "qwen", "SPANQL_HARNESS__RELEVANCE_CHECK": "false",
                                   "SPANQL_STORE__PATH": str(tmp_path / "x.db"), "SPANQL_BOGUS__X": "1", "OTHER": "1"})
    assert c.data["model"]["backend"] == "ollama" and c.data["model"]["model"] == "qwen"
    h = c.harness_config()
    assert h.max_attempts == 5 and h.relevance_check is False and h.candidate_token_budget == 8192
    assert c.store_path == str(tmp_path / "x.db")
    client = c.make_client()
    assert client.endpoint == "http://x:1/v1" and client.backend == "ollama"
    assert Config().bind() == ("127.0.0.1", 7717)


def test_config_errors(tmp_path):
    with pytest.raises(ConfigError, match="not found"):
        load_config(str(tmp_path / "missing.toml"), env={})
    bad = tmp_path / "bad.toml"
    bad.write_text("this is = = not toml")
    with pytest.raises(ConfigError):
        load_config(str(bad), env={})
    c = load_config(env={"SPANQL_MODEL__RUNTIME": "managed"})
    with pytest.raises(ConfigError, match="not implemented"):
        c.make_client()
    c = load_config(env={"SPANQL_MODEL__ENDPOINT": ""})
    assert c.make_client() is None
    c = load_config(env={"SPANQL_SERVER__BIND": "nonsense"})
    with pytest.raises(ConfigError):
        c.bind()


# ------------------------------------------------------------------- API


class Api:
    def __init__(self, cfg, client=None, token=None):
        self.srv = make_server(cfg, client=client, token=token, host="127.0.0.1", port=0)
        self.base = f"http://127.0.0.1:{self.srv.server_address[1]}"
        self.token = token
        threading.Thread(target=self.srv.serve_forever, daemon=True).start()

    def call(self, method, path, body=None, token="default"):
        data = json.dumps(body).encode() if body is not None else None
        req = urllib.request.Request(self.base + path, data=data, method=method,
                                     headers={"Content-Type": "application/json"})
        t = self.token if token == "default" else token
        if t:
            req.add_header("Authorization", f"Bearer {t}")
        try:
            with urllib.request.urlopen(req, timeout=10) as r:
                return r.status, json.loads(r.read())
        except urllib.error.HTTPError as e:
            return e.code, json.loads(e.read())

    def close(self):
        self.srv.shutdown()
        self.srv.server_close()


@pytest.fixture
def api(env):
    cfg = Config()
    cfg.data["store"]["path"] = env["db"]
    a = Api(cfg, client=ScriptedClient(['ANSWER(COUNT(FIND(src:"logs/jobs.jsonl", level:"error")), text:"{0} failures.")']))
    a.env = env
    yield a
    a.close()


def wait_job(api, job_id):
    import time

    for _ in range(100):
        st, j = api.call("GET", f"/v1/jobs/{job_id}")
        if j["status"] != "running":
            return j
        time.sleep(0.05)
    raise AssertionError("job did not finish")


def test_api_health_status_sources(api):
    assert api.call("GET", "/v1/health") == (200, {"status": "ok", "version": "0.1.0"})
    st, j = api.call("GET", "/v1/status")
    assert st == 200 and j["sources"] == 9 and j["model"]["configured"] is True
    st, j = api.call("GET", "/v1/sources?glob=data/*&describe=1")
    assert [s["uri"] for s in j["sources"]] == ["data/items.json", "data/sales.csv"] and j["schemas"]
    assert api.call("GET", "/v1/nope")[0] == 404
    assert api.call("POST", "/v1/health", {})[0] == 405


def test_api_ingest_jobs_and_errors(api, tmp_path):
    d = tmp_path / "extra"
    d.mkdir()
    (d / "n.md").write_text("# Notes\n\nHello world.\n")
    st, j = api.call("POST", "/v1/sources", {"path": str(d), "name": "notes"})
    assert st == 202
    job = wait_job(api, j["job_id"])
    assert job["status"] == "done" and job["result"]["added"] == ["notes/n.md"]
    st, j = api.call("GET", "/v1/sources?glob=notes/*")
    sid = j["sources"][0]["id"]
    st, j = api.call("POST", f"/v1/ingest/{sid}", {})
    assert st == 202 and wait_job(api, j["job_id"])["result"]["unchanged"] == 1
    bad = wait_job(api, api.call("POST", "/v1/sources", {"path": str(tmp_path / "missing")})[1]["job_id"])
    assert bad["status"] == "failed" and "no such file" in bad["error"]
    assert api.call("POST", "/v1/sources", {})[0] == 400
    assert api.call("POST", "/v1/sources", {"path": str(d), "watch": True})[0] == 400
    assert api.call("POST", "/v1/ingest/99999", {})[0] == 404
    assert api.call("GET", "/v1/jobs/j_nope")[0] == 404


def test_api_evaluate_cells_trace_recompute(api):
    f = 'ANSWER(COUNT(FIND(src:"logs/payments-*.log", re:"PaymentTimeout")), text:"{0} timeouts")'
    st, j = api.call("POST", "/v1/evaluate", {"formula": f})
    assert st == 200 and j["text"] == "3 timeouts" and j["kind"] == "answer"
    cid = j["cells"][0]["id"]
    st, c = api.call("GET", f"/v1/cells/{cid}")
    assert st == 200 and c["display"] == "3" and c["stale"] == []
    st, t = api.call("GET", f"/v1/cells/{cid}/trace")
    assert st == 200 and t["steps"][-1]["op"] == "COUNT" and t["inputs"][0]["kind"] == "query"
    # more errors arrive in the log: the cell goes stale, recompute yields a new cell and flags the change
    log = api.env["corpus"] / "logs/payments-2026-11-10.log"
    log.write_text(log.read_text() + "2026-11-12 10:00:00,000 ERROR x - PaymentTimeout z\n")
    assert cli.main(["--db", api.env["db"], "ingest", str(api.env["corpus"] / "logs")]) == 0
    assert api.call("GET", f"/v1/cells/{cid}")[1]["stale"][0]["kind"] == "query"
    st, r = api.call("POST", f"/v1/cells/{cid}/recompute", {})
    assert st == 200 and r["status"] == "ok" and r["changed"] is True and r["cells"][0]["display"] == "4"
    assert api.call("GET", f"/v1/cells/{r['cells'][0]['id']}")[0] == 200  # the new cell was saved
    assert api.call("GET", f"/v1/cells/{cid}")[1]["display"] == "3"  # the old one is untouched
    assert api.call("POST", "/v1/cells/c_nope/recompute", {})[0] == 404
    assert api.call("GET", "/v1/cells/c_nope")[0] == 404


def test_api_evaluate_error_shapes(api):
    st, j = api.call("POST", "/v1/evaluate", {"formula": 'COUNT(FIND(src:"nowhere"))'})
    assert st == 422 and j["error"]["check"] == "V5" and "ingested sources include" in j["error"]["hint"]
    st, j = api.call("POST", "/v1/evaluate", {"formula": "ANSWER("})
    assert st == 422 and j["error"]["check"] == "V1"
    assert api.call("POST", "/v1/evaluate", {})[0] == 400
    assert api.call("POST", "/v1/evaluate", {"formula": "1", "handles": [1]})[0] == 400
    st, j = api.call("POST", "/v1/evaluate", {"formula": 'NUM($1)', "handles": {"$1": "sp:deadbeef:0:1"}})
    assert st == 400 or st == 422


def test_api_ask_spans_runs(api):
    st, j = api.call("POST", "/v1/ask", {"question": "how many job failures?", "conversation_id": "t1"})
    assert st == 200 and j["status"] == "grounded" and j["answer"]["text"] == "2 failures."
    assert j["answer"]["cells"] == [j["cells"][0]["id"]]
    assert api.call("GET", "/v1/runs?limit=5")[1]["runs"][0]["id"] == j["run_id"]
    assert api.call("POST", "/v1/ask", {"question": ""})[0] == 400
    assert api.call("POST", "/v1/ask", {"question": "x", "scope": "nope"})[0] == 400
    sp = j["cells"][0]["inputs"][0]["sample"][0]
    st, s = api.call("GET", f"/v1/spans/{sp}?context=1")
    assert st == 200 and "job failed" in s["text"] and s["sources"] == ["logs/jobs.jsonl"] and s["current"] is True


def test_api_without_a_model_still_serves_evaluate(env):
    cfg = Config()
    cfg.data["store"]["path"] = env["db"]
    cfg.data["model"]["endpoint"] = ""
    a = Api(cfg)
    try:
        st, j = a.call("POST", "/v1/ask", {"question": "anything"})
        assert st == 503 and "no model is configured" in j["error"]["message"]
        assert a.call("POST", "/v1/evaluate", {"formula": 'COUNT(FIND(src:"logs/*"))'})[0] == 200
    finally:
        a.close()


def test_api_token_and_bind_security(env):
    cfg = Config()
    cfg.data["store"]["path"] = env["db"]
    a = Api(cfg, token="s3cret")
    try:
        assert a.call("GET", "/v1/health", token=None)[0] == 200  # health is always open
        assert a.call("GET", "/v1/status", token=None)[0] == 401
        assert a.call("GET", "/v1/status", token="wrong")[0] == 401
        assert a.call("GET", "/v1/status")[0] == 200
        assert a.call("POST", "/v1/evaluate", {"formula": "1"}, token=None)[0] == 401
    finally:
        a.close()
    with pytest.raises(ConfigError, match="SPANQL_API_TOKEN"):
        make_server(cfg, host="0.0.0.0", port=0)
    make_server(cfg, token="t", host="0.0.0.0", port=0).server_close()
    mem = Config()
    mem.data["store"]["path"] = ":memory:"
    with pytest.raises(ConfigError, match="file-backed"):
        make_server(mem, port=0)


def test_api_rejects_oversized_and_malformed_bodies(api):
    import http.client

    conn = http.client.HTTPConnection("127.0.0.1", api.srv.server_address[1], timeout=5)
    conn.request("POST", "/v1/evaluate", body=b"{not json", headers={"Content-Length": "9"})
    r = conn.getresponse()
    assert r.status == 400 and "invalid JSON" in json.loads(r.read())["error"]["message"]
    conn = http.client.HTTPConnection("127.0.0.1", api.srv.server_address[1], timeout=5)
    conn.request("POST", "/v1/evaluate", body=b"[]", headers={"Content-Length": "2"})
    assert conn.getresponse().status == 400
    conn = http.client.HTTPConnection("127.0.0.1", api.srv.server_address[1], timeout=5)
    conn.putrequest("POST", "/v1/evaluate")
    conn.putheader("Content-Length", str(2_000_000))
    conn.endheaders()
    assert conn.getresponse().status == 413
