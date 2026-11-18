"""Documentation that ships with the code must keep working."""

from __future__ import annotations

import re
from pathlib import Path

from spanql.config import load_config
from spanql.formula.parser import parse_formula
from spanql.formula.typecheck import typecheck

ROOT = Path(__file__).resolve().parent.parent


def test_example_config_loads_and_matches_defaults():
    cfg = load_config(str(ROOT / "config.example.toml"), env={})
    assert cfg.bind() == ("127.0.0.1", 7717)
    assert cfg.harness_config().max_attempts == 3 and cfg.harness_config().relevance_check is True
    assert cfg.make_client().endpoint == "http://127.0.0.1:7718/v1"
    assert cfg.sources == []  # the examples are commented out


def test_readme_formulas_parse_and_typecheck():
    text = (ROOT / "README.md").read_text()
    found = re.findall(r"spanql evaluate '([^']+)'", text)
    assert found, "README should show at least one runnable formula"
    for f in found:
        typecheck(parse_formula(f))


def test_architecture_examples_parse():
    """Every ANSWER(...)/NOT_FOUND(...)/AMBIGUOUS(...) block in ARCHITECTURE.md's example section must parse."""
    text = (ROOT / "docs/ARCHITECTURE.md").read_text()
    section = text.split("### 2.4 Examples")[1].split("### 2.5")[0]
    blocks = re.findall(r"```text\n(.*?)```", section, re.S)
    formulas = []
    for b in blocks:
        body = b.split("\n", 1)[1] if b.startswith("Q:") else b
        body = body.split("\n→")[0]  # drop the "→ {0} = ..." result line
        formulas.append(body.strip())
    assert len(formulas) == 5
    for f in formulas:
        parse_formula(f)  # the spec's own examples, verbatim
