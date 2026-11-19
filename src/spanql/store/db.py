"""The SQLite span store: sources, immutable snapshots, blocks, side tables, cells, runs."""

from __future__ import annotations

import bisect
import hashlib
import json
import re
import sqlite3
import threading
from collections.abc import Iterable, Iterator
from contextlib import contextmanager
from dataclasses import dataclass
from datetime import UTC, datetime
from functools import lru_cache
from pathlib import Path
from typing import Any

from spanql.blocks import BlockSpec, Converted
from spanql.store.globs import glob_match
from spanql.store.schema import SCHEMA, SCHEMA_VERSION

SPAN_RE = re.compile(r"^sp:([0-9a-f]{8,64}):(\d+):(\d+)$")


class SpanError(LookupError):
    """A span id doesn't resolve, or its bytes don't hash to the recorded value."""


def sha256_bytes(data: bytes) -> str:
    return hashlib.sha256(data).hexdigest()


def now_iso() -> str:
    return datetime.now(UTC).astimezone().isoformat(timespec="seconds")


@lru_cache(maxsize=256)
def _compile(pattern: str) -> re.Pattern[str]:
    return re.compile(pattern)


def _regexp(pattern: str | None, value: Any) -> int:
    if pattern is None or value is None:
        return 0
    if isinstance(value, bytes):
        value = value.decode("utf-8", errors="replace")
    return 1 if _compile(pattern).search(str(value)) else 0


_NUM_RE = re.compile(
    r"^\s*\(?\s*[-−]?\s*(?:US\$|\$|€|£|¥)?\s*[-−]?\d[\d,]*(?:\.\d+)?\s*%?\s*\)?\s*$"
)


def _num(value: Any) -> float | None:
    """Lenient numeric view of a field value for filter predicates (``FIELD("x") > 5``)."""
    if value is None or isinstance(value, bool):
        return None
    if isinstance(value, int | float):
        return float(value)
    s = value.decode("utf-8", "replace") if isinstance(value, bytes) else str(value)
    if not _NUM_RE.match(s):
        return None
    neg = "-" in s or "−" in s or (s.strip().startswith("(") and s.strip().endswith(")"))
    digits = re.sub(r"[^\d.]", "", s)
    try:
        f = float(digits)
    except ValueError:
        return None
    return -f if neg else f


@dataclass(frozen=True)
class Span:
    snapshot_id: str
    start: int
    end: int

    @property
    def id(self) -> str:
        return f"sp:{self.snapshot_id[:8]}:{self.start}:{self.end}"

    @staticmethod
    def parse(span_id: str) -> tuple[str, int, int]:
        m = SPAN_RE.match(span_id.strip())
        if not m:
            raise SpanError(f"malformed span id: {span_id!r}")
        start, end = int(m.group(2)), int(m.group(3))
        if end < start:
            raise SpanError(f"span end before start: {span_id!r}")
        return m.group(1), start, end


@dataclass
class ResolvedSpan:
    span: Span
    text: str
    text_sha256: str
    source_uri: str
    source_path: str
    source_kind: str
    line_start: int
    line_end: int
    is_current: bool
    page: int | None = None

    @property
    def id(self) -> str:
        return self.span.id

    def to_dict(self) -> dict[str, Any]:
        return {
            "id": self.id,
            "text": self.text,
            "text_sha256": self.text_sha256,
            "source": self.source_uri,
            "path": self.source_path,
            "kind": self.source_kind,
            "snapshot": self.span.snapshot_id,
            "start": self.span.start,
            "end": self.span.end,
            "line_start": self.line_start,
            "line_end": self.line_end,
            "page": self.page,
            "current": self.is_current,
        }


class Store:
    """Thread-safe wrapper around one SQLite file (WAL mode)."""

    def __init__(self, path: str | Path):
        self.path = str(path)
        if self.path != ":memory:":
            Path(self.path).parent.mkdir(parents=True, exist_ok=True)
        self._lock = threading.RLock()
        self.conn = sqlite3.connect(self.path, check_same_thread=False, isolation_level=None)
        self.conn.row_factory = sqlite3.Row
        self.conn.create_function("regexp", 2, _regexp, deterministic=True)
        self.conn.create_function("spanql_glob", 2, glob_match, deterministic=True)
        self.conn.create_function("spanql_num", 1, _num, deterministic=True)
        self.conn.execute("PRAGMA foreign_keys=ON")
        if self.path != ":memory:":
            self.conn.execute("PRAGMA journal_mode=WAL")
        self.conn.executescript(SCHEMA)
        self.conn.execute(
            "INSERT OR IGNORE INTO meta(key, value) VALUES('schema_version', ?)",
            (str(SCHEMA_VERSION),),
        )
        self._text_cache: dict[str, bytes] = {}
        self._line_cache: dict[str, list[int]] = {}

    def close(self) -> None:
        with self._lock:
            self.conn.close()

    # ------------------------------------------------------------------ basics
    @contextmanager
    def tx(self) -> Iterator[sqlite3.Connection]:
        with self._lock:
            self.conn.execute("BEGIN IMMEDIATE")
            try:
                yield self.conn
            except BaseException:
                self.conn.execute("ROLLBACK")
                raise
            else:
                self.conn.execute("COMMIT")

    def query(self, sql: str, params: Iterable[Any] = ()) -> list[sqlite3.Row]:
        with self._lock:
            return self.conn.execute(sql, tuple(params)).fetchall()

    def one(self, sql: str, params: Iterable[Any] = ()) -> sqlite3.Row | None:
        with self._lock:
            row: sqlite3.Row | None = self.conn.execute(sql, tuple(params)).fetchone()
            return row

    # ----------------------------------------------------------------- sources
    def get_source(self, uri: str) -> sqlite3.Row | None:
        return self.one("SELECT * FROM source WHERE uri = ?", (uri,))

    def list_sources(self, include_removed: bool = False) -> list[sqlite3.Row]:
        where = "" if include_removed else "WHERE removed = 0"
        return self.query(f"SELECT * FROM source {where} ORDER BY uri")

    def mark_removed(self, root: str, keep_uris: set[str]) -> int:
        rows = self.query("SELECT id, uri FROM source WHERE root = ? AND removed = 0", (root,))
        gone = [r["id"] for r in rows if r["uri"] not in keep_uris]
        with self.tx() as c:
            for sid in gone:
                c.execute("UPDATE source SET removed = 1 WHERE id = ?", (sid,))
        return len(gone)

    # --------------------------------------------------------------- snapshots
    def add_snapshot(
        self,
        *,
        uri: str,
        path: str,
        kind: str,
        root: str,
        raw_sha256: str,
        converted: Converted,
        git_commit: str | None = None,
    ) -> tuple[str, bool]:
        """Store a snapshot and its blocks. Returns (snapshot_id, created)."""
        data = converted.text.encode("utf-8")
        text_sha = sha256_bytes(data)
        snap_id = snapshot_id_for(uri, text_sha)
        with self.tx() as c:
            src = c.execute("SELECT * FROM source WHERE uri = ?", (uri,)).fetchone()
            if src is None:
                cur = c.execute(
                    "INSERT INTO source(uri, path, kind, root) VALUES(?,?,?,?)",
                    (uri, path, kind, root),
                )
                source_id = cur.lastrowid
            else:
                source_id = src["id"]
                c.execute(
                    "UPDATE source SET path=?, kind=?, root=?, removed=0 WHERE id=?",
                    (path, kind, root, source_id),
                )
            exists = c.execute("SELECT source_id FROM snapshot WHERE id = ?", (snap_id,)).fetchone()
            created = False
            if exists is None:
                created = True
                c.execute(
                    "INSERT INTO snapshot(id, text_sha256, source_id, ingested_at, raw_sha256,"
                    " converter, git_commit, text, offset_map, meta) VALUES(?,?,?,?,?,?,?,?,?,?)",
                    (
                        snap_id,
                        text_sha,
                        source_id,
                        now_iso(),
                        raw_sha256,
                        converted.converter,
                        git_commit,
                        data,
                        json.dumps(converted.offset_map) if converted.offset_map else None,
                        json.dumps(converted.meta),
                    ),
                )
                self._insert_blocks(c, snap_id, data, converted.blocks)
            c.execute("UPDATE source SET current_snapshot = ? WHERE id = ?", (snap_id, source_id))
        return snap_id, created

    def _insert_blocks(
        self, c: sqlite3.Connection, snap_id: str, data: bytes, blocks: list[BlockSpec]
    ) -> None:
        line_starts = _line_starts(data)
        ids: list[int] = []
        for b in blocks:
            if not (0 <= b.start <= b.end <= len(data)):
                raise ValueError(f"block out of range: {b}")
            chunk = data[b.start : b.end]
            ls = _line_of(line_starts, b.start)
            le = _line_of(line_starts, max(b.start, b.end - 1))
            cur = c.execute(
                "INSERT INTO block(snapshot_id, kind, start, end, text_sha256, path, line_start,"
                " line_end, page) VALUES(?,?,?,?,?,?,?,?,?)",
                (snap_id, b.kind, b.start, b.end, sha256_bytes(chunk), b.path, ls, le, b.page),
            )
            bid = int(cur.lastrowid or 0)
            ids.append(bid)
            if b.index_text:
                c.execute(
                    "INSERT INTO block_fts(rowid, text) VALUES(?, ?)",
                    (bid, b.fts_text or chunk.decode("utf-8", errors="replace")),
                )
            if b.log is not None:
                lg = b.log
                c.execute(
                    "INSERT INTO log_record(block_id, ts, level, logger, message_start,"
                    " message_end, fields) VALUES(?,?,?,?,?,?,?)",
                    (
                        bid,
                        lg.get("ts"),
                        lg.get("level"),
                        lg.get("logger"),
                        lg.get("message_start"),
                        lg.get("message_end"),
                        json.dumps(lg.get("fields") or {}, sort_keys=True),
                    ),
                )
            if b.row is not None:
                c.execute(
                    "INSERT INTO table_row(block_id, table_id, row, fields) VALUES(?,?,?,?)",
                    (bid, b.row["table_id"], b.row["row"], json.dumps(b.row["fields"])),
                )
            if b.cell is not None:
                cl = b.cell
                row_block = ids[cl["row_index"]] if cl.get("row_index") is not None else None
                c.execute(
                    "INSERT INTO table_cell(block_id, table_id, row, col, header, row_block)"
                    " VALUES(?,?,?,?,?,?)",
                    (bid, cl["table_id"], cl["row"], cl["col"], cl.get("header"), row_block),
                )
            if b.symbol is not None:
                sy = b.symbol
                parent = ids[sy["parent_index"]] if sy.get("parent_index") is not None else None
                c.execute(
                    "INSERT INTO code_symbol(block_id, lang, kind, name, qualified_name,"
                    " parent_block, calls) VALUES(?,?,?,?,?,?,?)",
                    (
                        bid,
                        sy.get("lang"),
                        sy.get("kind"),
                        sy.get("name"),
                        sy.get("qualified_name"),
                        parent,
                        json.dumps(sorted(set(sy.get("calls") or []))),
                    ),
                )

    def snapshot_text(self, snap_id: str) -> bytes:
        if snap_id in self._text_cache:
            return self._text_cache[snap_id]
        row = self.one("SELECT text, text_sha256 FROM snapshot WHERE id = ?", (snap_id,))
        if row is None:
            raise SpanError(f"unknown snapshot {snap_id[:12]}")
        data = bytes(row["text"])
        if sha256_bytes(data) != row["text_sha256"]:
            raise SpanError(f"snapshot {snap_id[:12]} is corrupt (hash mismatch)")
        if len(self._text_cache) > 64:
            self._text_cache.clear()
        self._text_cache[snap_id] = data
        return data

    def expand_snapshot_id(self, prefix: str) -> str:
        if len(prefix) == 64:
            return prefix
        rows = self.query(
            "SELECT id FROM snapshot WHERE id >= ? AND id < ? LIMIT 2", (prefix, prefix + "g")
        )
        if not rows:
            raise SpanError(f"unknown snapshot prefix {prefix}")
        if len(rows) > 1:
            raise SpanError(f"snapshot prefix {prefix} is ambiguous; use more characters")
        return str(rows[0]["id"])

    def line_starts(self, snap_id: str) -> list[int]:
        if snap_id not in self._line_cache:
            self._line_cache[snap_id] = _line_starts(self.snapshot_text(snap_id))
        return self._line_cache[snap_id]

    # ------------------------------------------------------------------- spans
    def resolve(self, span: Span | str, expected_sha256: str | None = None) -> ResolvedSpan:
        if isinstance(span, str):
            prefix, start, end = Span.parse(span)
            span = Span(self.expand_snapshot_id(prefix), start, end)
        data = self.snapshot_text(span.snapshot_id)
        if span.end > len(data):
            raise SpanError(f"span {span.id} is past the end of its snapshot")
        chunk = data[span.start : span.end]
        try:
            text = chunk.decode("utf-8")
        except UnicodeDecodeError as exc:
            raise SpanError(f"span {span.id} splits a UTF-8 character") from exc
        digest = sha256_bytes(chunk)
        if expected_sha256 is not None and digest != expected_sha256:
            raise SpanError(f"span {span.id} bytes don't match recorded hash")
        row = self.one(
            "SELECT src.uri, src.path, src.kind, src.current_snapshot FROM snapshot s"
            " JOIN source src ON src.id = s.source_id WHERE s.id = ?",
            (span.snapshot_id,),
        )
        assert row is not None
        ls = self.line_starts(span.snapshot_id)
        page = self.one(
            "SELECT page FROM block WHERE snapshot_id = ? AND start <= ? AND end >= ?"
            " AND page IS NOT NULL LIMIT 1",
            (span.snapshot_id, span.start, span.end),
        )
        return ResolvedSpan(
            span=span,
            text=text,
            text_sha256=digest,
            source_uri=row["uri"],
            source_path=row["path"],
            source_kind=row["kind"],
            line_start=_line_of(ls, span.start),
            line_end=_line_of(ls, max(span.start, span.end - 1)),
            is_current=row["current_snapshot"] == span.snapshot_id,
            page=page["page"] if page else None,
        )

    def block_span(self, block_id: int) -> Span:
        row = self.one("SELECT snapshot_id, start, end FROM block WHERE id = ?", (block_id,))
        if row is None:
            raise SpanError(f"unknown block {block_id}")
        return Span(row["snapshot_id"], row["start"], row["end"])

    def block_text(self, block_id: int) -> str:
        sp = self.block_span(block_id)
        return self.snapshot_text(sp.snapshot_id)[sp.start : sp.end].decode("utf-8")

    def read_with_context(self, span: Span | str, context_lines: int = 2) -> dict[str, Any]:
        rs = self.resolve(span)
        data = self.snapshot_text(rs.span.snapshot_id)
        ls = self.line_starts(rs.span.snapshot_id)
        first = max(1, rs.line_start - context_lines)
        last = min(len(ls), rs.line_end + context_lines)
        a = ls[first - 1]
        b = ls[last] - 1 if last < len(ls) else len(data)
        out = rs.to_dict()
        out["context"] = {
            "line_start": first,
            "line_end": last,
            "text": data[a:b].decode("utf-8", errors="replace"),
        }
        return out

    # ------------------------------------------------------------------- stats
    def stats(self) -> dict[str, Any]:
        def count(sql: str) -> int:
            row = self.one(sql)
            return int(row[0]) if row else 0

        kinds = {
            r["kind"]: r["n"]
            for r in self.query(
                "SELECT kind, COUNT(*) n FROM source WHERE removed = 0 GROUP BY kind"
            )
        }
        return {
            "sources": count("SELECT COUNT(*) FROM source WHERE removed = 0"),
            "sources_by_kind": kinds,
            "snapshots": count("SELECT COUNT(*) FROM snapshot"),
            "blocks": count("SELECT COUNT(*) FROM block"),
            "current_blocks": count(
                "SELECT COUNT(*) FROM block b JOIN source src"
                " ON src.current_snapshot = b.snapshot_id AND src.removed = 0"
            ),
            "cells": count("SELECT COUNT(*) FROM cell"),
            "runs": count("SELECT COUNT(*) FROM run"),
            "schema_version": SCHEMA_VERSION,
        }

    # ------------------------------------------------------------------- cells
    def save_cell(self, cell: dict[str, Any], question_id: str | None = None) -> None:
        with self.tx() as c:
            c.execute(
                "INSERT OR REPLACE INTO cell(id, question_id, formula, value, type, status,"
                " created_at, trace) VALUES(?,?,?,?,?,?,?,?)",
                (
                    cell["id"],
                    question_id,
                    cell["formula"],
                    json.dumps(cell["value"]),
                    cell["value"].get("type", "?"),
                    cell["status"],
                    cell["evaluated_at"],
                    json.dumps(cell),
                ),
            )
            c.execute("DELETE FROM cell_dep WHERE cell_id = ?", (cell["id"],))
            for inp in cell.get("inputs", []):
                c.execute(
                    "INSERT INTO cell_dep(cell_id, dep_kind, ref) VALUES(?,?,?)",
                    (cell["id"], inp["kind"], inp.get("id") or inp.get("query", "")),
                )

    def get_cell(self, cell_id: str) -> dict[str, Any] | None:
        row = self.one("SELECT trace FROM cell WHERE id = ?", (cell_id,))
        return json.loads(row["trace"]) if row else None

    def save_run(self, run_id: str, question: str, status: str, record: dict[str, Any]) -> None:
        with self.tx() as c:
            c.execute(
                "INSERT OR REPLACE INTO run(id, created_at, question, status, record)"
                " VALUES(?,?,?,?,?)",
                (run_id, now_iso(), question, status, json.dumps(record, default=str)),
            )

    def list_runs(self, limit: int = 20) -> list[dict[str, Any]]:
        rows = self.query(
            "SELECT id, created_at, question, status FROM run ORDER BY created_at DESC, id LIMIT ?",
            (limit,),
        )
        return [dict(r) for r in rows]

    def get_run(self, run_id: str) -> dict[str, Any] | None:
        row = self.one("SELECT record FROM run WHERE id = ?", (run_id,))
        return json.loads(row["record"]) if row else None

    # ---------------------------------------------------------------------- gc
    def gc_snapshots(self, retention_days: int) -> int:
        """Delete non-current snapshots older than retention that no cell references."""
        cutoff = datetime.now(UTC).timestamp() - retention_days * 86400
        rows = self.query(
            "SELECT s.id, s.ingested_at FROM snapshot s"
            " WHERE s.id NOT IN (SELECT current_snapshot FROM source"
            " WHERE current_snapshot IS NOT NULL)"
        )
        referenced = {
            r["ref"].split(":")[1]
            for r in self.query("SELECT ref FROM cell_dep WHERE dep_kind = 'span'")
            if r["ref"].startswith("sp:")
        }
        removed = 0
        with self.tx() as c:
            for r in rows:
                if datetime.fromisoformat(r["ingested_at"]).timestamp() > cutoff:
                    continue
                if r["id"][:8] in referenced:
                    continue
                bids = [
                    x[0]
                    for x in c.execute("SELECT id FROM block WHERE snapshot_id = ?", (r["id"],))
                ]
                for t in ("log_record", "table_row", "table_cell", "code_symbol"):
                    c.executemany(f"DELETE FROM {t} WHERE block_id = ?", [(b,) for b in bids])
                c.executemany("DELETE FROM block_fts WHERE rowid = ?", [(b,) for b in bids])
                c.execute("DELETE FROM block WHERE snapshot_id = ?", (r["id"],))
                c.execute("DELETE FROM snapshot WHERE id = ?", (r["id"],))
                removed += 1
        self._text_cache.clear()
        self._line_cache.clear()
        return removed

    def backup(self, dest: str | Path) -> None:
        with self._lock:
            target = sqlite3.connect(str(dest))
            try:
                self.conn.backup(target)
            finally:
                target.close()


def snapshot_id_for(uri: str, text_sha256: str) -> str:
    """Snapshot ids are content-addressed *per source*: identical files under two URIs get
    distinct snapshots, so every span knows which file it came from."""
    return sha256_bytes(uri.encode("utf-8") + b"\0" + text_sha256.encode("ascii"))


def _line_starts(data: bytes) -> list[int]:
    starts = [0]
    i = data.find(b"\n")
    while i != -1:
        starts.append(i + 1)
        i = data.find(b"\n", i + 1)
    return starts


def _line_of(starts: list[int], offset: int) -> int:
    """1-based line number containing byte ``offset``."""
    return bisect.bisect_right(starts, offset)
