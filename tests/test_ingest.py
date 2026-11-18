from __future__ import annotations

import json

from spanql.ingest import convert, ingest_path
from spanql.ingest.normalize import canonical_to_raw, normalize
from spanql.ingest.tables import parse_csv, segment_json
from spanql.store import collect_garbage, reanchor


def blocks_of(store, uri, kind=None):
    src = store.get_source(uri)
    q = "SELECT * FROM block WHERE snapshot_id=?" + (" AND kind=?" if kind else "") + " ORDER BY start"
    args = (src.current_snapshot, kind) if kind else (src.current_snapshot,)
    return store.db.execute(q, args).fetchall()


def text_of(store, row):
    return store.snapshot_bytes(row["snapshot_id"])[row["start"] : row["end"]].decode()


# ---------------------------------------------------------------- normalize


def test_normalize_identity_for_canonical_text():
    n = normalize("héllo\nworld\n".encode())
    assert n.offset_map is None and n.data == "héllo\nworld\n".encode()


def test_normalize_crlf_bom_nfc_and_offset_map():
    raw = b"\xef\xbb\xbfab\r\ncd\r\ne\xcc\x81x\r\nlast"
    n = normalize(raw)
    assert n.data == "ab\ncd\néx\nlast".encode()
    assert canonical_to_raw(n.offset_map, 3) == raw.index(b"cd")
    assert canonical_to_raw(n.offset_map, 10) == raw.index(b"last")
    # inside the NFC-changed line offsets snap to the line
    assert canonical_to_raw(n.offset_map, 7) == raw.index(b"e\xcc")


# ------------------------------------------------------------------- tables


def test_csv_tokenizer_handles_quotes_and_newlines():
    d = b'a,b\n"x, y","say ""hi"""\n"multi\nline",z\n'
    rows = parse_csv(d)
    assert [[c.value for c in r] for r in rows] == [["a", "b"], ["x, y", 'say "hi"'], ["multi\nline", "z"]]
    c = rows[1][0]
    assert d[c.start : c.end] == b"x, y" and d[c.outer_start : c.outer_end] == b'"x, y"'


def test_csv_blocks_cells_and_rows(ingested):
    rows = blocks_of(ingested, "data/sales.csv", "table_row")
    assert len(rows) == 4
    cells = ingested.db.execute(
        "SELECT b.id FROM table_cell c JOIN block b ON b.id=c.block_id WHERE c.header='amount' ORDER BY b.start"
    ).fetchall()
    texts = [text_of(ingested, ingested.db.execute("SELECT * FROM block WHERE id=?", (c[0],)).fetchone()) for c in cells]
    assert texts == ["1,200.50", "300", "950", 'a ""quoted"" one']
    snap = ingested.get_source("data/sales.csv").current_snapshot
    fields = json.loads(ingested.db.execute(
        "SELECT t.fields FROM table_row t JOIN block b ON b.id=t.block_id WHERE b.snapshot_id=? ORDER BY t.row LIMIT 1",
        (snap,)).fetchone()[0])
    assert fields == {"region": "EMEA", "product": "widget", "amount": "1,200.50", "units": "10"}


def test_json_array_becomes_table_and_nonarray_falls_back():
    d = b'[{"a": 1, "b": "x"}, {"a": 2}]'
    blocks = segment_json(d)
    assert [b.kind for b in blocks] == ["table_row", "table_cell", "table_cell", "table_row", "table_cell"]
    assert d[blocks[1].start : blocks[1].end] == b"1" and d[blocks[2].start : blocks[2].end] == b"x"
    assert segment_json(b'{"a": 1}') is None and segment_json(b"[1,2]") is None and segment_json(b"[{") is None


# --------------------------------------------------------------------- logs


def test_app_log_records_continuations_and_fields(ingested):
    recs = blocks_of(ingested, "logs/payments-2026-11-10.log", "log_record")
    assert len(recs) == 7
    stack = [text_of(ingested, r) for r in recs if "Traceback" in text_of(ingested, r)]
    assert len(stack) == 1 and stack[0].count("\n") == 3  # continuation lines stay in the record
    row = ingested.db.execute(
        "SELECT l.* FROM log_record l WHERE l.fields LIKE '%bob%'"
    ).fetchone()
    assert (row["level"], row["logger"], row["day"]) == ("ERROR", "payments.api", "2026-11-10")
    assert json.loads(row["fields"]) == {"user": "bob", "latency_ms": 3000}
    msg = ingested.snapshot_bytes(ingested.get_source("logs/payments-2026-11-10.log").current_snapshot)
    assert msg[row["message_start"] : row["message_start"] + 14] == b"PaymentTimeout"


def test_nginx_and_jsonl_profiles(ingested):
    r = ingested.db.execute(
        "SELECT l.fields, l.day FROM log_record l JOIN block b ON b.id=l.block_id "
        "JOIN source s ON s.current_snapshot=b.snapshot_id WHERE s.uri='logs/api/access.log' ORDER BY b.start"
    ).fetchall()
    assert [json.loads(x["fields"])["status"] for x in r] == [200, 500, 404, 503, 502]
    assert r[-1]["day"] == "2026-11-17"
    j = ingested.db.execute(
        "SELECT l.level, l.ts_utc FROM log_record l JOIN block b ON b.id=l.block_id "
        "JOIN source s ON s.current_snapshot=b.snapshot_id WHERE s.uri='logs/jobs.jsonl' ORDER BY b.start"
    ).fetchall()
    assert [x["level"] for x in j] == ["INFO", "ERROR", "ERROR"]
    assert j[0]["ts_utc"] == "2026-11-12T10:00:00.000000Z"


def test_log_timezone_keeps_local_day_but_sortable_utc():
    doc = convert(b"2026-11-10T23:30:00-06:00 INFO late\n", "log", "log", "x.log")
    rec = doc.blocks[0].record
    assert rec["day"] == "2026-11-10" and rec["ts_utc"].startswith("2026-11-11T05:30:00")


def test_custom_and_plain_log_profiles():
    doc = convert(b"[2026-01-02 03:04:05] user=7 hello\n", "log", "log", "x.log", parser=r"re:^\[(?P<ts>[^\]]+)\] (?P<message>.*)")
    assert doc.blocks[0].record["day"] == "2026-01-02" and doc.segmentation.startswith("log:re:")
    doc = convert(b"no timestamps here\nat all\n", "log", "log", "x.log")
    assert doc.segmentation.startswith("log:plain") and len(doc.blocks) == 2


# --------------------------------------------------------------------- text


def test_markdown_blocks_and_heading_paths(ingested):
    rows = blocks_of(ingested, "reports/q3-2026.md")
    kinds = [r["kind"] for r in rows]
    assert kinds == ["heading", "heading", "paragraph", "heading", "list_item", "list_item", "code_block"]
    para = next(r for r in rows if r["kind"] == "paragraph")
    assert para["path"] == "Q3 2026 Report > Revenue"
    assert text_of(ingested, para).startswith("Total revenue for the quarter")
    assert (para["line_start"], para["line_end"]) == (5, 5)


# --------------------------------------------------------------------- code


def test_python_symbols_and_calls(ingested):
    rows = ingested.db.execute(
        "SELECT c.*, b.kind bk FROM code_symbol c JOIN block b ON b.id=c.block_id ORDER BY b.start"
    ).fetchall()
    by_name = {r["qualified_name"]: r for r in rows}
    assert by_name["payments.retry"]["kind"] == "module"
    assert by_name["payments.retry.Client.get"]["kind"] == "method"
    assert by_name["payments.retry.standalone"]["kind"] == "function"
    assert "retry_with_backoff" in json.loads(by_name["payments.retry.Client.get"]["calls"])
    assert "retry_with_backoff" not in json.loads(by_name["payments.retry.Client._post"]["calls"])
    # a call inside a nested/other def is not attributed to the enclosing class
    assert "retry_with_backoff" not in json.loads(by_name["payments.retry.Client"]["calls"])


def test_syntax_error_python_falls_back_to_chunks():
    doc = convert(b"def broken(:\n    pass\n", "code", "python", "x.py")
    assert doc.segmentation.startswith("code:chunks")
    assert any(b.kind == "line" for b in doc.blocks)


# ----------------------------------------------------------------- pipeline


def test_reingest_unchanged_is_a_noop(corpus, store):
    first = ingest_path(store, str(corpus / "reports"))
    before = store.status()
    second = ingest_path(store, str(corpus / "reports"))
    assert len(first.added) == 2 and second.unchanged == 2 and not second.changed
    assert store.status() == before


def test_every_block_round_trips_byte_exact(ingested):
    from hashlib import sha256

    for r in ingested.db.execute("SELECT snapshot_id, start, \"end\", text_sha256 FROM block"):
        data = ingested.snapshot_bytes(r["snapshot_id"])
        assert sha256(data[r["start"] : r["end"]]).hexdigest() == r["text_sha256"]
        data[r["start"] : r["end"]].decode("utf-8")  # on a char boundary


def test_edit_creates_new_snapshot_keeps_old_and_deleted_files_are_marked(corpus, store):
    ingest_path(store, str(corpus / "reports"))
    src = store.get_source("reports/q2-2026.md")
    old = src.current_snapshot
    (corpus / "reports/q2-2026.md").write_text("# Q2 2026 Report\n\nQ2 revenue: $4.31M\n\nExtra paragraph.\n")
    (corpus / "reports/q3-2026.md").unlink()
    rep = ingest_path(store, str(corpus / "reports"))
    assert rep.updated == ["reports/q2-2026.md"] and rep.removed == ["reports/q3-2026.md"]
    assert store.get_source("reports/q2-2026.md").current_snapshot != old
    assert store.snapshot_bytes(old)  # old snapshot still readable
    assert store.get_source("reports/q3-2026.md") is not None
    assert [s.uri for s in store.list_sources()] == ["reports/q2-2026.md"]


def test_identical_files_share_one_snapshot_but_count_per_source(tmp_path, store):
    for n in ("a", "b"):
        (tmp_path / f"{n}.md").write_text("same text\n")
    ingest_path(store, str(tmp_path))
    assert store.status()["snapshots"] == 1 and store.status()["sources"] == 2


def test_unsupported_and_binary_files_are_skipped_with_reason(tmp_path, store):
    (tmp_path / "doc.pdf").write_bytes(b"%PDF-1.4")
    (tmp_path / "blob.bin").write_bytes(b"\x00\x01\x02")
    (tmp_path / "ok.txt").write_text("hi\n")
    rep = ingest_path(store, str(tmp_path))
    reasons = dict(rep.skipped)
    assert "Docling" in reasons[f"{tmp_path.name}/doc.pdf"] and "binary" in reasons[f"{tmp_path.name}/blob.bin"]
    assert rep.added == [f"{tmp_path.name}/ok.txt"]


def test_spanqlignore_and_gitignore(tmp_path, store):
    import subprocess

    (tmp_path / "keep.txt").write_text("k\n")
    (tmp_path / "skip.txt").write_text("s\n")
    (tmp_path / ".spanqlignore").write_text("skip.txt\n")
    rep = ingest_path(store, str(tmp_path))
    assert [u.rsplit("/", 1)[1] for u in rep.added] == ["keep.txt"]
    # git repo: ignored files are excluded and the commit is recorded
    repo = tmp_path / "repo"
    repo.mkdir()
    (repo / "a.py").write_text("x = 1\n")
    (repo / "junk.log").write_text("junk\n")
    (repo / ".gitignore").write_text("*.log\n")
    subprocess.run(["git", "init", "-q", str(repo)], check=True)
    subprocess.run(["git", "-C", str(repo), "add", "."], check=True)
    subprocess.run(["git", "-C", str(repo), "-c", "user.name=t", "-c", "user.email=t@t", "commit", "-qm", "init"], check=True)
    rep = ingest_path(store, str(repo))
    assert sorted(u.rsplit("/", 1)[1] for u in rep.added) == [".gitignore", "a.py"]
    commit = store.db.execute(
        "SELECT git_commit FROM source_snapshot ss JOIN source s ON s.id=ss.source_id WHERE s.uri LIKE '%/a.py'"
    ).fetchone()[0]
    assert commit and len(commit) == 40


# ------------------------------------------------------------- spans / store


def test_span_round_trip_integrity_and_location(ingested):
    src = ingested.get_source("reports/q3-2026.md")
    data = ingested.snapshot_bytes(src.current_snapshot)
    s = data.index(b"$4.82 million")
    span = ingested.make_span(src.current_snapshot, s, s + 13)
    assert ingested.span_text(span) == "$4.82 million"
    again = ingested.span_from_id(span.id)
    assert again == span
    loc = ingested.locate(span)
    assert loc.sources == ("reports/q3-2026.md",) and loc.line_start == 5 and loc.path == "Q3 2026 Report > Revenue"
    # corruption guard
    from dataclasses import replace
    from spanql.errors import SpanIntegrityError
    import pytest

    with pytest.raises(SpanIntegrityError):
        ingested.span_text(replace(span, text_sha256="0" * 64))


def test_reanchor_after_edit(corpus, store):
    ingest_path(store, str(corpus / "reports"))
    src = store.get_source("reports/q3-2026.md")
    data = store.snapshot_bytes(src.current_snapshot)
    s = data.index(b"$4.82 million")
    span = store.make_span(src.current_snapshot, s, s + 13)
    assert reanchor(store, span)[0].status == "unchanged"
    p = corpus / "reports/q3-2026.md"
    p.write_text("# Intro added above\n\nNew paragraph.\n\n" + p.read_text())
    ingest_path(store, str(corpus / "reports"))
    r = reanchor(store, span)[0]
    assert r.status == "moved" and store.span_text(r.span) == "$4.82 million"
    # edited value: fuzzy re-anchor reports the new text, never silently the old
    p.write_text(p.read_text().replace("$4.82 million", "$4.85 million"))
    ingest_path(store, str(corpus / "reports"))
    r = reanchor(store, span)[0]
    assert r.status in ("edited", "moved") and "4.8" in store.span_text(r.span)
    p.unlink()
    ingest_path(store, str(corpus / "reports"))
    assert reanchor(store, span)[0].status == "missing"


def test_gc_keeps_current_and_cited_snapshots(corpus, store):
    ingest_path(store, str(corpus / "reports"))
    old = store.get_source("reports/q2-2026.md").current_snapshot
    (corpus / "reports/q2-2026.md").write_text("# changed\n")
    ingest_path(store, str(corpus / "reports"))
    assert collect_garbage(store, retention_days=90)["snapshots_deleted"] == 0  # within retention
    assert collect_garbage(store, retention_days=-1)["snapshots_deleted"] >= 1  # past retention
    import pytest
    from spanql.errors import StoreError

    store._cache.clear()
    with pytest.raises(StoreError):
        store.snapshot_bytes(old)
