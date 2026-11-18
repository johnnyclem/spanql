"""Write snapshots, blocks and side tables; garbage-collect old snapshots."""

from __future__ import annotations

import json
import sqlite3
from dataclasses import dataclass

from ..blocks import Block, Document
from ..errors import StoreError
from ..spans import sha256_hex
from .db import Store, utc_now


@dataclass(slots=True)
class CommitResult:
    source_id: int
    snapshot_id: str
    new_snapshot: bool  # False when identical canonical text was already stored


def _line_starts(data: bytes) -> list[int]:
    starts = [0]
    pos = data.find(b"\n")
    while pos != -1:
        starts.append(pos + 1)
        pos = data.find(b"\n", pos + 1)
    return starts


def commit_version(
    store: Store,
    *,
    uri: str,
    kind: str,
    root: str | None,
    rel_path: str | None,
    raw_sha256: str,
    raw_size: int,
    doc: Document,
    git_commit: str | None = None,
) -> CommitResult:
    """Record ``doc`` as the new current snapshot of source ``uri``."""
    from bisect import bisect_right

    now = utc_now()
    sid = sha256_hex(doc.data)
    with store.transaction() as db:
        db.execute(
            """INSERT INTO source(uri, kind, root, rel_path) VALUES (?,?,?,?)
               ON CONFLICT(uri) DO UPDATE SET kind=excluded.kind, root=excluded.root,
                   rel_path=excluded.rel_path, deleted_at=NULL""",
            (uri, kind, root, rel_path),
        )
        source_id = db.execute("SELECT id FROM source WHERE uri=?", (uri,)).fetchone()[0]
        new_snapshot = db.execute("SELECT 1 FROM snapshot WHERE id=?", (sid,)).fetchone() is None
        if new_snapshot:
            db.execute(
                "INSERT INTO snapshot(id, text, n_bytes, segmentation, created_at) VALUES (?,?,?,?,?)",
                (sid, doc.data, len(doc.data), doc.segmentation, now),
            )
            _write_blocks(db, sid, doc.data, doc.blocks, _line_starts(doc.data), bisect_right)
        db.execute(
            "UPDATE source_snapshot SET superseded_at=? WHERE source_id=? AND superseded_at IS NULL",
            (now, source_id),
        )
        db.execute(
            """INSERT INTO source_snapshot(source_id, snapshot_id, ingested_at, raw_sha256, raw_size,
                                           git_commit, offset_map) VALUES (?,?,?,?,?,?,?)""",
            (source_id, sid, now, raw_sha256, raw_size, git_commit, doc.offset_map),
        )
        db.execute("UPDATE source SET current_snapshot=? WHERE id=?", (sid, source_id))
    return CommitResult(source_id, sid, new_snapshot)


def _write_blocks(db: sqlite3.Connection, sid: str, data: bytes, blocks: list[Block], starts, bisect_right) -> None:
    if not blocks:
        return
    base = db.execute("SELECT COALESCE(MAX(id),0) FROM block").fetchone()[0]
    ids = [base + i + 1 for i in range(len(blocks))]
    n = len(data)
    rows, fts, logs, trows, tcells, syms = [], [], [], [], [], []
    for i, b in enumerate(blocks):
        if not (0 <= b.start <= b.end <= n):
            raise StoreError(f"segmenter produced an out-of-range block {b.kind} {b.start}:{b.end} (size {n})")
        bid = ids[i]
        first = bisect_right(starts, b.start)
        last = bisect_right(starts, max(b.start, b.end - 1)) if b.end > b.start else first
        rows.append(
            (bid, sid, b.kind, b.start, b.end, sha256_hex(data[b.start : b.end]), b.path, first, last, b.page,
             1 if b.is_unit else 0)
        )
        if b.fts is not None:
            fts.append((bid, b.fts))
        if b.record is not None:
            r = b.record
            logs.append(
                (bid, r.get("ts"), r.get("ts_utc"), r.get("day"), r.get("level"), r.get("logger"),
                 r.get("message_start"), r.get("message_end"), json.dumps(r.get("fields") or {}, ensure_ascii=False))
            )
        if b.row is not None:
            trows.append((bid, b.row["table_id"], b.row["row"], json.dumps(b.row["fields"], ensure_ascii=False)))
        if b.cell is not None:
            c = b.cell
            tcells.append((bid, c["table_id"], c["row"], c["col"], c["header"], ids[c["row_block"]]))
        if b.symbol is not None:
            s = b.symbol
            parent = ids[s["parent"]] if s.get("parent") is not None else None
            syms.append((bid, s["lang"], s["kind"], s["name"], s["qualified_name"], parent,
                         json.dumps(s.get("calls") or [])))
    db.executemany(
        'INSERT INTO block(id, snapshot_id, kind, start, "end", text_sha256, path, line_start, line_end, page, is_unit)'
        " VALUES (?,?,?,?,?,?,?,?,?,?,?)",
        rows,
    )
    db.executemany("INSERT INTO block_fts(rowid, text) VALUES (?,?)", fts)
    db.executemany(
        "INSERT INTO log_record(block_id, ts, ts_utc, day, level, logger, message_start, message_end, fields)"
        " VALUES (?,?,?,?,?,?,?,?,?)",
        logs,
    )
    db.executemany("INSERT INTO table_row(block_id, table_id, row, fields) VALUES (?,?,?,?)", trows)
    db.executemany(
        "INSERT INTO table_cell(block_id, table_id, row, col, header, row_block) VALUES (?,?,?,?,?,?)", tcells
    )
    db.executemany(
        "INSERT INTO code_symbol(block_id, lang, kind, name, qualified_name, parent_block, calls)"
        " VALUES (?,?,?,?,?,?,?)",
        syms,
    )


def mark_deleted(store: Store, uri: str) -> None:
    """The file vanished: stop matching queries, keep history for cells that cite it."""
    now = utc_now()
    with store.transaction() as db:
        row = db.execute("SELECT id FROM source WHERE uri=?", (uri,)).fetchone()
        if row is None:
            return
        db.execute("UPDATE source SET current_snapshot=NULL, deleted_at=? WHERE id=?", (now, row[0]))
        db.execute(
            "UPDATE source_snapshot SET superseded_at=? WHERE source_id=? AND superseded_at IS NULL", (now, row[0])
        )


def collect_garbage(store: Store, retention_days: int = 90) -> dict:
    """Delete snapshots no source currently uses, no cell cites, and that are older than retention."""
    import time

    cutoff = time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime(time.time() - retention_days * 86400))
    with store.transaction() as db:
        victims = [
            r[0]
            for r in db.execute(
                """SELECT sn.id FROM snapshot sn
                   WHERE NOT EXISTS (SELECT 1 FROM source s WHERE s.current_snapshot = sn.id)
                     AND NOT EXISTS (SELECT 1 FROM cell_dep d WHERE d.dep_kind='snapshot' AND d.ref = sn.id)
                     AND NOT EXISTS (SELECT 1 FROM cell_dep d WHERE d.dep_kind='span'
                                     AND d.ref LIKE 'sp:' || substr(sn.id, 1, 8) || ':%')
                     AND COALESCE((SELECT MAX(COALESCE(ss.superseded_at, ss.ingested_at))
                                   FROM source_snapshot ss WHERE ss.snapshot_id = sn.id), sn.created_at) < ?""",
                (cutoff,),
            )
        ]
        for sid in victims:
            db.execute("DELETE FROM block_fts WHERE rowid IN (SELECT id FROM block WHERE snapshot_id=?)", (sid,))
            db.execute("DELETE FROM source_snapshot WHERE snapshot_id=?", (sid,))
            db.execute("DELETE FROM snapshot WHERE id=?", (sid,))  # blocks cascade
    return {"snapshots_deleted": len(victims)}
