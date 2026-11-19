from __future__ import annotations

import json
from pathlib import Path

import pytest

from spanql.cli import main


@pytest.fixture
def db(tmp_path: Path, demo_dir: Path, capsys) -> list[str]:
    base = ["--db", str(tmp_path / "cli.db"), "--backend", "fake"]
    assert main([*base, "ingest", str(demo_dir)]) == 0
    capsys.readouterr()
    return base


def out_json(capsys) -> dict:
    return json.loads(capsys.readouterr().out)


def test_init_writes_config(tmp_path: Path, capsys) -> None:
    cfg = tmp_path / "c.toml"
    assert main(["--config", str(cfg), "init", "--store", str(tmp_path / "s.db")]) == 0
    assert cfg.exists() and "[model]" in cfg.read_text()
    assert main(["--config", str(cfg), "init"]) == 0
    assert "already exists" in capsys.readouterr().out


def test_ingest_is_idempotent(db: list[str], demo_dir: Path, capsys) -> None:
    assert main([*db, "ingest", str(demo_dir), "--json"]) == 0
    rep = out_json(capsys)
    assert rep["unchanged"] == 7 and rep["added"] == 0


def test_sources_status_search(db: list[str], capsys) -> None:
    assert main([*db, "sources", "--json"]) == 0
    assert len(out_json(capsys)) == 7
    assert main([*db, "status"]) == 0
    assert "sources" in capsys.readouterr().out
    assert main([*db, "search", "PaymentTimeout", "--json"]) == 0
    assert out_json(capsys)["candidates"]


def test_ask_evaluate_trace_readspan(db: list[str], capsys) -> None:
    assert main([*db, "ask", "When was the Q3 report published?", "--json"]) == 0
    res = out_json(capsys)
    assert res["status"] == "grounded"
    cid = res["answer"]["cells"][0]
    assert main([*db, "trace", cid, "--json"]) == 0
    trace = out_json(capsys)
    span = next(i for i in trace["inputs"] if i["kind"] == "span")["id"]
    assert main([*db, "read-span", span]) == 0
    assert "November 3, 2026" in capsys.readouterr().out
    rc = main([*db, "evaluate", 'ANSWER(DATE($1."November 3, 2026"))', "--handle", f"1={span}"])
    assert rc == 0 and "2026-11-03" in capsys.readouterr().out
    assert main([*db, "evaluate", "ANSWER(123456)"]) == 1
    assert "V4" in capsys.readouterr().out
    assert main([*db, "runs"]) == 0
    assert main([*db, "recompute", cid]) == 0


def test_refusal_exit_code(db: list[str], capsys) -> None:
    rc = main([*db, "ask", "What was our Q3 churn rate?"])
    assert rc == 0
    assert "couldn't find" in capsys.readouterr().out


def test_grammar_and_models(tmp_path: Path, capsys) -> None:
    assert main(["grammar", "--handles", "3"]) == 0
    assert 'handle ::= "$"' in capsys.readouterr().out
    assert main(["model", "list"]) == 0
    assert "qwen3.5-4b-instruct-q4_k_m" in capsys.readouterr().out
    assert main(["model", "pull", "nope"]) == 2


def test_selftest_offline(capsys) -> None:
    assert main(["selftest", "--no-model"]) == 0
    assert "FAIL" not in capsys.readouterr().out


def test_health_unreachable(capsys) -> None:
    assert main(["health", "--url", "http://127.0.0.1:9/v1/health"]) == 1


def test_config_error_exit_code(tmp_path: Path, capsys) -> None:
    bad = tmp_path / "bad.toml"
    bad.write_text("[nope]\n")
    assert main(["--config", str(bad), "status"]) == 2


def test_gc_backup(db: list[str], tmp_path: Path, capsys) -> None:
    assert main([*db, "gc", "--retention-days", "0"]) == 0
    assert main([*db, "backup", str(tmp_path / "bk.db")]) == 0
    assert (tmp_path / "bk.db").exists()


def test_global_options_after_subcommand(tmp_path: Path, demo_dir: Path, capsys) -> None:
    db = str(tmp_path / "after.db")
    assert main(["ingest", str(demo_dir), "--db", db, "--json"]) == 0
    assert json.loads(capsys.readouterr().out)["added"] == 7
    assert main(["--db", db, "sources", "--json"]) == 0
    assert len(json.loads(capsys.readouterr().out)) == 7
    cfg = tmp_path / "c.toml"
    assert main(["init", "--config", str(cfg)]) == 0
    assert cfg.exists()


def test_ask_with_unreachable_model_exits_nonzero(tmp_path: Path, demo_dir: Path, capsys) -> None:
    base = ["--db", str(tmp_path / "down.db"), "--backend", "llama.cpp"]
    base += ["--endpoint", "http://127.0.0.1:9/v1"]
    assert main([*base, "ingest", str(demo_dir)]) == 0
    capsys.readouterr()
    assert main([*base, "ask", "What was Q3 revenue?"]) == 2
    io = capsys.readouterr()
    assert "model backend failed" in io.err and "couldn't find" not in io.out + io.err
