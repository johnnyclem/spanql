"""Documentation and shipped config must keep working (ported from PR #1's test_docs)."""

from __future__ import annotations

import re
from pathlib import Path

import pytest

from spanql.config import load_config
from spanql.engine.run import evaluate_formula
from spanql.engine.typecheck import typecheck
from spanql.formula.parser import parse
from spanql.store.db import Store

ROOT = Path(__file__).resolve().parent.parent


def _architecture_examples() -> list[str]:
    text = (ROOT / "docs/ARCHITECTURE.md").read_text()
    section = text.split("### 2.4 Examples")[1].split("### 2.5")[0]
    out = []
    for block in re.findall(r"```text\n(.*?)```", section, re.S):
        body = block.split("\n", 1)[1] if block.startswith("Q:") else block
        out.append(body.split("\n→")[0].strip())
    return out


def test_architecture_examples_parse_and_typecheck() -> None:
    formulas = _architecture_examples()
    assert len(formulas) == 5
    for f in formulas:
        assert typecheck(parse(f)) == [], f  # the spec's own examples, verbatim


def _readme_evaluations() -> list[tuple[str, str]]:
    text = (ROOT / "README.md").read_text()
    return re.findall(r"spanql evaluate '([^']+)'\n#\s+(.+)", text)


def test_readme_has_runnable_examples() -> None:
    assert _readme_evaluations(), "README should show at least one `spanql evaluate` example"


@pytest.mark.parametrize(("formula", "expected"), _readme_evaluations())
def test_readme_evaluate_examples_print_what_they_say(
    demo_store: Store, formula: str, expected: str
) -> None:
    out = evaluate_formula(demo_store, formula, {})
    assert out.status == "grounded", out.problems
    assert out.answer == expected.strip()


def test_docker_config_loads() -> None:
    cfg = load_config(ROOT / "packaging/docker/config.toml", env={})
    assert str(cfg.store_path) == "/data/spanql.db"
    assert cfg.model.endpoint == "http://llm:8080/v1"
    assert [s.path for s in cfg.sources] == ["/sources"]


def test_adr_index_lists_every_adr() -> None:
    index = (ROOT / "docs/adr/README.md").read_text()
    for adr in sorted((ROOT / "docs/adr").glob("[0-9][0-9][0-9][0-9]-*.md")):
        assert f"({adr.name})" in index, adr.name
