from __future__ import annotations

import pytest

from spanql.ingest.code import convert_code, module_name, python_symbols, treesitter_available

PY = b"""import time

def retry(fn):
    return fn()

class Client:
    def charge(self, o):
        return retry(lambda: self._post(o))

    def _post(self, o):
        time.sleep(1)
"""


def test_module_name() -> None:
    assert module_name("repo/pkg/mod.py") == "pkg.mod"
    assert module_name("repo/pkg/__init__.py") == "pkg"


def test_python_symbols_and_calls() -> None:
    syms = python_symbols(PY.decode(), "m")
    assert syms is not None
    by = {s.qualified: s for s in syms}
    assert set(by) >= {"m.retry", "m.Client", "m.Client.charge", "m.Client._post"}
    assert by["m.Client.charge"].kind == "method"
    assert "retry" in by["m.Client.charge"].calls
    assert "sleep" in by["m.Client._post"].calls


def test_syntax_error_falls_back() -> None:
    c = convert_code(b"def broken(:\n  pass\n", "r/x.py", ".py")
    assert c.blocks  # chunked rather than failing


def test_symbol_offsets_cover_source() -> None:
    c = convert_code(PY, "r/m.py", ".py")
    text = c.text.encode()
    for b in c.blocks:
        if b.kind == "symbol" and b.symbol["name"] == "charge":
            assert text[b.start : b.end].decode().lstrip().startswith("def charge")


@pytest.mark.skipif(not treesitter_available("typescript"), reason="tree-sitter extra missing")
def test_typescript_treesitter() -> None:
    src = b"export class S { start() { listen(1); } }\nexport function h() { return retry(() => 1); }\n"
    c = convert_code(src, "r/s.ts", ".ts")
    assert c.converter.startswith("code-tree-sitter")
    names = {b.symbol["qualified_name"]: b.symbol for b in c.blocks if b.symbol}
    assert "s.S.start" in names and names["s.S.start"]["kind"] == "method"
    assert "retry" in names["s.h"]["calls"]


@pytest.mark.skipif(not treesitter_available("go"), reason="tree-sitter extra missing")
def test_go_treesitter() -> None:
    src = b"package main\n\nfunc Charge() error { return Retry(nil) }\n"
    c = convert_code(src, "r/main.go", ".go")
    syms = [b.symbol for b in c.blocks if b.symbol]
    assert any(s["name"] == "Charge" and "Retry" in s["calls"] for s in syms)
