from __future__ import annotations

from pathlib import Path

import pytest

from spanql.config import SourceConfig
from spanql.ingest.pipeline import detect_kind, ingest_all
from spanql.store.db import Span, SpanError, Store, snapshot_id_for
from spanql.store.globs import glob_match
from spanql.store.reanchor import reanchor

from .conftest import write


def _span_of(store: Store, uri: str, needle: str) -> Span:
    row = store.get_source(uri)
    assert row is not None
    data = store.snapshot_text(row["current_snapshot"])
    i = data.index(needle.encode())
    return Span(row["current_snapshot"], i, i + len(needle.encode()))


def test_ingest_demo_counts(demo_store: Store) -> None:
    st = demo_store.stats()
    assert st["sources"] == 7
    assert st["snapshots"] == 7
    assert st["sources_by_kind"]["code"] == 2  # client.py, server.ts (config.yaml is a file)
    assert demo_store.one("SELECT COUNT(*) FROM log_record")[0] == 7
    assert demo_store.one("SELECT COUNT(*) FROM code_symbol")[0] >= 10


def test_span_ids_resolve_exact_bytes(demo_store: Store) -> None:
    sp = _span_of(demo_store, "demo/reports/q3-2026.md", "$4.82 million")
    rs = demo_store.resolve(sp.id)
    assert rs.text == "$4.82 million"
    assert rs.is_current
    assert rs.source_uri == "demo/reports/q3-2026.md"
    assert rs.line_start >= 1
    ctx = demo_store.read_with_context(sp.id, 1)
    assert "$4.82 million" in ctx["context"]["text"]


def test_span_hash_check_and_errors(demo_store: Store) -> None:
    sp = _span_of(demo_store, "demo/reports/q3-2026.md", "$4.82 million")
    rs = demo_store.resolve(sp)
    assert demo_store.resolve(sp, rs.text_sha256).text == rs.text
    with pytest.raises(SpanError, match="hash"):
        demo_store.resolve(sp, "0" * 64)
    with pytest.raises(SpanError, match="malformed"):
        demo_store.resolve("sp:zz")
    with pytest.raises(SpanError, match="past the end"):
        demo_store.resolve(Span(sp.snapshot_id, 0, 10**9))
    with pytest.raises(SpanError, match="unknown snapshot"):
        demo_store.resolve("sp:deadbeef:0:1")


def test_corrupt_snapshot_detected(demo_store: Store) -> None:
    sp = _span_of(demo_store, "demo/reports/q3-2026.md", "$4.82 million")
    with demo_store.tx() as con:
        con.execute("UPDATE snapshot SET text = ? WHERE id = ?", (b"tampered", sp.snapshot_id))
    demo_store._text_cache.clear()
    with pytest.raises(SpanError, match="corrupt"):
        demo_store.resolve(sp)


def test_snapshot_id_is_per_source() -> None:
    assert snapshot_id_for("a/x.md", "h") != snapshot_id_for("b/x.md", "h")


def test_identical_files_get_distinct_snapshots(store: Store, tmp_path: Path) -> None:
    write(tmp_path / "r" / "a.md", "same text\n")
    write(tmp_path / "r" / "b.md", "same text\n")
    ingest_all(store, [SourceConfig(path=str(tmp_path / "r"))])
    a, b = store.get_source("r/a.md"), store.get_source("r/b.md")
    assert a["current_snapshot"] != b["current_snapshot"]


def test_reingest_is_noop_then_update_then_remove(store: Store, tmp_path: Path) -> None:
    root = tmp_path / "notes"
    f = write(root / "n.md", "# T\n\nAlpha is 10 widgets.\n")
    write(root / "other.md", "other\n")
    src = [SourceConfig(path=str(root))]
    r1 = ingest_all(store, src)
    assert r1.added == 2
    r2 = ingest_all(store, src)
    assert r2.unchanged == 2 and r2.added == 0 and r2.updated == 0
    old = _span_of(store, "notes/n.md", "Alpha is 10 widgets.")

    f.write_text("# T\n\nNew intro line.\n\nAlpha is 10 widgets.\n")
    r3 = ingest_all(store, src)
    assert r3.updated == 1
    ra = reanchor(store, old)
    assert ra.status == "moved" and ra.new_text == "Alpha is 10 widgets."
    assert not store.resolve(old).is_current  # old snapshot still resolvable

    f.write_text("# T\n\nNew intro line.\n\nAlpha is 12 widgets.\n")
    ingest_all(store, src)
    ra2 = reanchor(store, old)
    assert ra2.status == "changed" and "12" in (ra2.new_text or "")

    f.write_text("# T\n\nCompletely different content about gardening.\n")
    ingest_all(store, src)
    assert reanchor(store, old).status == "missing"

    (root / "other.md").unlink()
    r4 = ingest_all(store, src)
    assert r4.removed == 1
    assert store.get_source("notes/other.md")["removed"] == 1


def test_single_file_source_does_not_remove_siblings(store: Store, tmp_path: Path) -> None:
    write(tmp_path / "d" / "a.log", "2026-11-10T08:00:00Z INFO x: a\n")
    write(tmp_path / "d" / "b.log", "2026-11-10T08:00:00Z INFO x: b\n")
    ingest_all(store, [SourceConfig(path=str(tmp_path / "d"))])
    rep = ingest_all(store, [SourceConfig(path=str(tmp_path / "d" / "a.log"))])
    assert rep.removed == 0


def test_gitignore_and_binary_skipped(store: Store, tmp_path: Path) -> None:
    root = tmp_path / "proj"
    write(root / ".gitignore", "secret/\n*.tmp\n")
    write(root / "secret" / "k.md", "do not index\n")
    write(root / "x.tmp", "nope\n")
    write(root / "ok.md", "fine\n")
    write(root / "blob.bin.txt", b"\x00\x01\x02binary")
    rep = ingest_all(store, [SourceConfig(path=str(root))])
    uris = {r["uri"] for r in store.list_sources()}
    assert uris == {"proj/ok.md", "proj/.gitignore"} or uris == {"proj/ok.md"}
    assert any("binary" in why for _, why in rep.skipped)


def test_glob_source(store: Store, tmp_path: Path) -> None:
    write(tmp_path / "logs" / "a.log", "2026-11-10T08:00:00Z INFO x: a\n")
    write(tmp_path / "logs" / "b.txt", "not a log\n")
    ingest_all(store, [SourceConfig(path=str(tmp_path / "logs" / "*.log"), kind="log")])
    assert [r["uri"] for r in store.list_sources()] == ["logs/a.log"]


def test_detect_kind() -> None:
    assert detect_kind("a/b.py") == "code"
    assert detect_kind("a/b.md") == "doc"
    assert detect_kind("a/b.log") == "log"
    assert detect_kind("a/b.csv") in ("file", "table", "doc")


@pytest.mark.parametrize(
    "pattern,uri,ok",
    [
        ("demo/logs/*.log", "demo/logs/a.log", True),
        ("logs/*.log", "demo/logs/a.log", True),  # root name optional
        ("*.log", "demo/logs/a.log", True),  # basename match
        ("**/*.py", "demo/repo/payments/client.py", True),
        ("demo/**", "demo/a/b/c", True),
        ("*.{md,csv}", "demo/data/x.csv", True),
        ("demo/[ab]*.md", "demo/b1.md", True),
        ("logs/*.log", "demo/logs/sub/a.log", False),
        ("*.md", "demo/x.mdx", False),
        ("./x.md", "demo/x.md", True),
    ],
)
def test_globs(pattern: str, uri: str, ok: bool) -> None:
    assert bool(glob_match(pattern, uri)) is ok


def test_gc_and_backup(demo_store: Store, tmp_path: Path) -> None:
    assert demo_store.gc_snapshots(0) == 0  # only current snapshots exist
    dest = tmp_path / "b.db"
    demo_store.backup(dest)
    copy = Store(dest)
    assert copy.stats()["sources"] == 7
    copy.close()


def test_cells_and_runs_roundtrip(demo_store: Store) -> None:
    cell = {
        "id": "c_x",
        "formula": "NUM($1)",
        "value": {"type": "Number", "v": "1"},
        "status": "grounded",
        "evaluated_at": "2026-11-18T00:00:00Z",
        "inputs": [{"kind": "span", "id": "sp:abcd1234:0:1"}],
    }
    demo_store.save_cell(cell)
    assert demo_store.get_cell("c_x") == cell
    assert demo_store.one("SELECT ref FROM cell_dep WHERE cell_id='c_x'")[0] == "sp:abcd1234:0:1"
    demo_store.save_run("r_1", "q?", "grounded", {"a": 1})
    assert demo_store.get_run("r_1") == {"a": 1}
    assert demo_store.list_runs()[0]["id"] == "r_1"
    assert demo_store.get_cell("nope") is None


def test_reanchor_finds_a_value_edited_in_place(store: Store, tmp_path: Path) -> None:
    root = tmp_path / "fin"
    f = write(root / "t.md", "# Finance\n\nQ2 revenue was $4.31M. Q3 revenue was $4.82 million.\n")
    src = [SourceConfig(path=str(root))]
    ingest_all(store, src)
    old = _span_of(store, "fin/t.md", "$4.31M")
    q3 = _span_of(store, "fin/t.md", "$4.82 million")
    # insert a paragraph above and edit the value itself
    f.write_text(
        "# Finance\n\nNew intro.\n\nQ2 revenue was $4.35M. Q3 revenue was $4.82 million.\n"
    )
    ingest_all(store, src)
    ra = reanchor(store, old)
    assert ra.status == "changed" and ra.method == "anchors", ra
    assert ra.new_text == "$4.35M"
    assert ra.new and store.resolve(ra.new).text == "$4.35M"
    # the untouched neighbour is still found exactly
    ra3 = reanchor(store, q3)
    assert ra3.status == "moved" and ra3.new_text == "$4.82 million"
