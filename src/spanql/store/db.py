"""The span store: one SQLite file holding snapshots, blocks, indexes and cells.

Everything that touches SQL schema lives under ``spanql.store``; the engine only
talks to this class.
"""

from __future__ import annotations

import json
import re
import sqlite3
import threading
import time
import unicodedata
from bisect import bisect_right
from collections import OrderedDict
from contextlib import contextmanager
from dataclasses import dataclass
from functools import lru_cache
from pathlib import Path
from typing import Iterator

from ..errors import SpanIntegrityError, StoreError
from ..globs import match_uri
from ..spans import SNAPSHOT_PREFIX, Span, parse_span_id, sha256_hex
from .schema import MIGRATIONS, SCHEMA_VERSION

DEFAULT_CACHE_BYTES = 256 * 1024 * 1024


@dataclass(frozen=True, slots=True)
class SourceRow:
    id: int
    uri: str
    kind: str
    root: str | None
    rel_path: str | None
    current_snapshot: str | None


@dataclass(frozen=True, slots=True)
class Location:
    """Where a span lives, for display and citation."""

    sources: tuple[str, ...]  # URIs that have (or had) this snapshot, current first
    line_start: int
    line_end: int
    block_kind: str | None = None
    path: str | None = None
    page: int | None = None
    current: bool = True  # False when every source has moved on to another snapshot



def utc_now() -> str:
    return time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime())


@lru_cache(maxsize=256)
def _compile(pattern: str) -> re.Pattern[str]:
    return re.compile(pattern)


class Store:
    """Connection + read APIs. Not shared across threads: open one per thread."""

    def __init__(self, path: str | Path = ":memory:", *, cache_bytes: int = DEFAULT_CACHE_BYTES):
        self.path = str(path)
        if self.path != ":memory:":
            Path(self.path).expanduser().parent.mkdir(parents=True, exist_ok=True)
            self.path = str(Path(self.path).expanduser())
        self.db = sqlite3.connect(self.path, isolation_level=None)
        self.db.row_factory = sqlite3.Row
        self.db.execute("PRAGMA foreign_keys=ON")
        if self.path != ":memory:":
            self.db.execute("PRAGMA journal_mode=WAL")
            self.db.execute("PRAGMA synchronous=NORMAL")
        self._cache: OrderedDict[str, bytes] = OrderedDict()
        self._cache_bytes = 0
        self._cache_limit = cache_bytes
        self._line_index: OrderedDict[str, list[int]] = OrderedDict()
        self._deadline: float | None = None
        self._lock = threading.RLock()
        self._migrate()
        self._register_functions()

    # ------------------------------------------------------------------ setup

    def _migrate(self) -> None:
        cur = self.db.execute("PRAGMA user_version").fetchone()[0]
        if cur > SCHEMA_VERSION:
            raise StoreError(
                f"store schema v{cur} is newer than this spanql (v{SCHEMA_VERSION}); upgrade spanql"
            )
        for v in range(cur + 1, SCHEMA_VERSION + 1):
            try:
                # executescript with an explicit transaction keeps each migration atomic
                self.db.executescript(f"BEGIN;\n{MIGRATIONS[v]}\nPRAGMA user_version={v};\nCOMMIT;")
            except Exception:
                if self.db.in_transaction:
                    self.db.execute("ROLLBACK")
                raise

    def _register_functions(self) -> None:
        """SQL functions used by planner-generated queries (ARCHITECTURE.md section 3)."""
        d = self.db

        def sp_text(sid, start, end):
            if sid is None or start is None or end is None:
                return None
            return self.snapshot_bytes(sid)[start:end].decode("utf-8", "replace")

        def sp_re(pattern, sid, start, end):
            text = sp_text(sid, start, end)
            return 1 if text is not None and _compile(pattern).search(text) else 0

        def num(v):
            from .numparse import sql_number

            return sql_number(v)

        d.create_function("spanql_text", 3, sp_text, deterministic=True)
        d.create_function("spanql_re", 4, sp_re, deterministic=True)
        d.create_function("spanql_num", 1, num, deterministic=True)
        d.set_progress_handler(self._progress, 100_000)

    def _progress(self) -> int:
        if self._deadline is not None and time.monotonic() > self._deadline:
            return 1  # abort the running statement
        return 0

    @contextmanager
    def deadline(self, seconds: float | None) -> Iterator[None]:
        """Abort long-running statements inside the block (raises sqlite3.OperationalError)."""
        prev = self._deadline
        self._deadline = None if seconds is None else time.monotonic() + seconds
        try:
            yield
        finally:
            self._deadline = prev

    def close(self) -> None:
        self.db.close()

    def __enter__(self) -> "Store":
        return self

    def __exit__(self, *exc) -> None:
        self.close()

    @contextmanager
    def transaction(self) -> Iterator[sqlite3.Connection]:
        with self._lock:
            self.db.execute("BEGIN IMMEDIATE")
            try:
                yield self.db
            except BaseException:
                self.db.execute("ROLLBACK")
                raise
            else:
                self.db.execute("COMMIT")

    # -------------------------------------------------------------- snapshots

    def snapshot_bytes(self, snapshot_id: str) -> bytes:
        data = self._cache.get(snapshot_id)
        if data is not None:
            self._cache.move_to_end(snapshot_id)
            return data
        row = self.db.execute("SELECT text FROM snapshot WHERE id=?", (snapshot_id,)).fetchone()
        if row is None:
            raise StoreError(f"unknown snapshot {snapshot_id[:SNAPSHOT_PREFIX]}")
        data = bytes(row[0])
        self._cache[snapshot_id] = data
        self._cache_bytes += len(data)
        while self._cache_bytes > self._cache_limit and len(self._cache) > 1:
            _, old = self._cache.popitem(last=False)
            self._cache_bytes -= len(old)
        return data

    def resolve_snapshot(self, prefix: str) -> str:
        if len(prefix) == 64:
            rows = self.db.execute("SELECT id FROM snapshot WHERE id=?", (prefix,)).fetchall()
        else:
            rows = self.db.execute(
                "SELECT id FROM snapshot WHERE id >= ? AND id < ? LIMIT 3", (prefix, prefix + "g")
            ).fetchall()
        if not rows:
            raise StoreError(f"no snapshot with prefix {prefix}")
        if len(rows) > 1:
            raise StoreError(f"snapshot prefix {prefix} is ambiguous; use more characters")
        return rows[0][0]

    def verify_snapshot(self, snapshot_id: str) -> bool:
        """Full integrity check: the stored text must hash to its id."""
        return sha256_hex(self.snapshot_bytes(snapshot_id)) == snapshot_id

    # ------------------------------------------------------------------ spans

    def make_span(self, snapshot_id: str, start: int, end: int) -> Span:
        data = self.snapshot_bytes(snapshot_id)
        if not (0 <= start <= end <= len(data)):
            raise StoreError(
                f"span {start}:{end} is outside snapshot {snapshot_id[:SNAPSHOT_PREFIX]} "
                f"({len(data)} bytes)"
            )
        return Span(snapshot_id, start, end, sha256_hex(data[start:end]))

    def span_from_id(self, span_id: str) -> Span:
        prefix, start, end = parse_span_id(span_id)
        return self.make_span(self.resolve_snapshot(prefix), start, end)

    def span_bytes(self, span: Span, *, verify: bool = True) -> bytes:
        data = self.snapshot_bytes(span.snapshot_id)
        if span.end > len(data):
            raise SpanIntegrityError(f"{span.id} extends past the end of its snapshot")
        raw = data[span.start : span.end]
        if verify and sha256_hex(raw) != span.text_sha256:
            raise SpanIntegrityError(
                f"{span.id}: bytes at these offsets no longer hash to the recorded text_sha256"
            )
        return raw

    def span_text(self, span: Span, *, verify: bool = True) -> str:
        try:
            return self.span_bytes(span, verify=verify).decode("utf-8")
        except UnicodeDecodeError as e:
            raise SpanIntegrityError(f"{span.id} does not fall on UTF-8 character boundaries") from e

    def line_starts(self, snapshot_id: str) -> list[int]:
        idx = self._line_index.get(snapshot_id)
        if idx is not None:
            self._line_index.move_to_end(snapshot_id)
            return idx
        data = self.snapshot_bytes(snapshot_id)
        idx = [0]
        pos = data.find(b"\n")
        while pos != -1:
            idx.append(pos + 1)
            pos = data.find(b"\n", pos + 1)
        self._line_index[snapshot_id] = idx
        while len(self._line_index) > 32:
            self._line_index.popitem(last=False)
        return idx

    def line_of(self, snapshot_id: str, offset: int) -> int:
        """1-based line number containing byte ``offset``."""
        return bisect_right(self.line_starts(snapshot_id), offset)

    def line_range(self, span: Span) -> tuple[int, int]:
        first = self.line_of(span.snapshot_id, span.start)
        last = self.line_of(span.snapshot_id, max(span.start, span.end - 1)) if span.end > span.start else first
        return first, last

    def span_for_lines(self, snapshot_id: str, first: int, last: int) -> Span:
        """Span covering whole lines ``first..last`` (1-based, inclusive), without the final newline."""
        starts = self.line_starts(snapshot_id)
        data = self.snapshot_bytes(snapshot_id)
        n_lines = len(starts) - (1 if data.endswith(b"\n") else 0)
        if first < 1 or last < first or first > n_lines:
            raise StoreError(f"line range {first}-{last} is outside the file ({n_lines} lines)")
        last = min(last, n_lines)
        start = starts[first - 1]
        end = starts[last] - 1 if last < len(starts) else len(data)
        return self.make_span(snapshot_id, start, max(start, end))

    def read_span_context(self, span: Span, lines: int = 0) -> dict:
        """Text of a span plus ``lines`` lines of context on each side."""
        text = self.span_text(span)
        first, last = self.line_range(span)
        starts = self.line_starts(span.snapshot_id)
        data = self.snapshot_bytes(span.snapshot_id)
        c_first = max(1, first - lines)
        c_last = min(len(starts), last + lines)
        c_start = starts[c_first - 1]
        c_end = starts[c_last] - 1 if c_last < len(starts) else len(data)
        context = data[c_start : max(c_start, c_end)].decode("utf-8", "replace")
        return {
            "span": span.id,
            "text": text,
            "line_start": first,
            "line_end": last,
            "context": context,
            "context_line_start": c_first,
            "context_line_end": c_last,
        }

    def locate(self, span: Span) -> Location:
        rows = self.db.execute(
            """SELECT s.uri, s.current_snapshot
               FROM source_snapshot ss JOIN source s ON s.id = ss.source_id
               WHERE ss.snapshot_id = ? ORDER BY ss.id DESC""",
            (span.snapshot_id,),
        ).fetchall()
        seen: dict[str, bool] = {}
        for r in rows:
            seen.setdefault(r["uri"], r["current_snapshot"] == span.snapshot_id)
        uris = sorted(seen, key=lambda u: (not seen[u], u))
        first, last = self.line_range(span)
        blk = self.db.execute(
            """SELECT kind, path, page FROM block WHERE snapshot_id=? AND start<=? AND "end">=?
               ORDER BY ("end"-start) ASC LIMIT 1""",
            (span.snapshot_id, span.start, span.end),
        ).fetchone()
        return Location(
            sources=tuple(uris),
            line_start=first,
            line_end=last,
            block_kind=blk["kind"] if blk else None,
            path=blk["path"] if blk else None,
            page=blk["page"] if blk else None,
            current=any(seen.values()),
        )

    # ---------------------------------------------------------------- sources

    def _source(self, r: sqlite3.Row) -> SourceRow:
        return SourceRow(r["id"], r["uri"], r["kind"], r["root"], r["rel_path"], r["current_snapshot"])

    def list_sources(self, glob: str | None = None, *, include_deleted: bool = False) -> list[SourceRow]:
        rows = self.db.execute("SELECT * FROM source ORDER BY uri").fetchall()
        out = [self._source(r) for r in rows]
        if not include_deleted:
            out = [s for s in out if s.current_snapshot is not None]
        if glob:
            out = [s for s in out if match_uri(glob, s.uri)]
        return out

    def get_source(self, uri: str) -> SourceRow | None:
        r = self.db.execute("SELECT * FROM source WHERE uri=?", (uri,)).fetchone()
        return self._source(r) if r else None

    def source_by_id(self, source_id: int) -> SourceRow | None:
        r = self.db.execute("SELECT * FROM source WHERE id=?", (source_id,)).fetchone()
        return self._source(r) if r else None

    def is_current(self, uri: str, snapshot_id: str) -> bool:
        s = self.get_source(uri)
        return bool(s and s.current_snapshot == snapshot_id)

    def status(self) -> dict:
        q = lambda sql: self.db.execute(sql).fetchone()[0]  # noqa: E731
        by_kind = {
            r["kind"]: r["n"]
            for r in self.db.execute(
                "SELECT kind, COUNT(*) n FROM source WHERE current_snapshot IS NOT NULL GROUP BY kind"
            )
        }
        return {
            "store": self.path,
            "schema_version": SCHEMA_VERSION,
            "sources": q("SELECT COUNT(*) FROM source WHERE current_snapshot IS NOT NULL"),
            "sources_by_kind": by_kind,
            "snapshots": q("SELECT COUNT(*) FROM snapshot"),
            "blocks": q("SELECT COUNT(*) FROM block"),
            "cells": q("SELECT COUNT(*) FROM cell"),
            "runs": q("SELECT COUNT(*) FROM run"),
            "bytes": q("SELECT COALESCE(SUM(n_bytes),0) FROM snapshot"),
        }

    # ------------------------------------------------------------------- cells

    def save_cells(self, cells: list["dict"]) -> None:
        """Persist serialized cells (``Cell.to_json()`` dicts) and their dependencies."""
        with self.transaction() as db:
            for c in cells:
                db.execute(
                    "INSERT OR REPLACE INTO cell(id, question_id, formula, value, type, status, created_at, trace)"
                    " VALUES (?,?,?,?,?,?,?,?)",
                    (
                        c["id"],
                        c.get("question_id"),
                        c["formula"],
                        json.dumps(c["value"]),
                        c["type"],
                        c["status"],
                        c["evaluated_at"],
                        json.dumps(c, separators=(",", ":")),
                    ),
                )
                db.execute("DELETE FROM cell_dep WHERE cell_id=?", (c["id"],))
                for dep_kind, ref in cell_deps(c):
                    db.execute(
                        "INSERT INTO cell_dep(cell_id, dep_kind, ref) VALUES (?,?,?)", (c["id"], dep_kind, ref)
                    )

    def get_cell(self, cell_id: str) -> dict | None:
        r = self.db.execute("SELECT trace FROM cell WHERE id=?", (cell_id,)).fetchone()
        return json.loads(r[0]) if r else None

    def cells_for_question(self, question_id: str) -> list[dict]:
        rows = self.db.execute(
            "SELECT trace FROM cell WHERE question_id=? ORDER BY json_extract(trace,'$.index')", (question_id,)
        ).fetchall()
        return [json.loads(r[0]) for r in rows]

    def staleness(self, cell: dict) -> list[dict]:
        """Inputs of a cell whose source has since changed (empty list = fresh)."""
        stale: list[dict] = []
        for inp in cell.get("inputs", []):
            kind = inp.get("kind")
            if kind == "span":
                uri = inp.get("source")
                if uri is None:
                    continue
                src = self.get_source(uri)
                if src is None or src.current_snapshot != inp["snapshot"]:
                    stale.append({"kind": "span", "id": inp["id"], "source": uri,
                                  "reason": "source deleted" if src is None or src.current_snapshot is None
                                  else "source changed"})
            elif kind == "query":
                q = inp["query"]
                kind = q.get("kind") if q.get("op") == "FIND" and q.get("kind") in ("doc", "log", "file", "code") else None
                now = {s.uri: s.current_snapshot for s in self.list_sources(q.get("src"))
                       if kind is None or s.kind == kind}
                if now != inp.get("snapshots", {}):
                    stale.append({"kind": "query", "op": inp.get("op"), "reason": "matching sources changed"})
            elif kind == "cell":
                dep = self.get_cell(inp["id"])
                if dep is None:
                    stale.append({"kind": "cell", "id": inp["id"], "reason": "cell missing"})
                elif self.staleness(dep):
                    stale.append({"kind": "cell", "id": inp["id"], "reason": "depends on a stale cell"})
        return stale

    # -------------------------------------------------------------------- misc

    def block_count(self, snapshot_id: str) -> int:
        return self.db.execute("SELECT COUNT(*) FROM block WHERE snapshot_id=?", (snapshot_id,)).fetchone()[0]


def cell_deps(cell: dict) -> list[tuple[str, str]]:
    """(dep_kind, ref) pairs for the cell_dep table."""
    deps: list[tuple[str, str]] = []
    for inp in cell.get("inputs", []):
        kind = inp.get("kind")
        if kind == "span":
            deps.append(("span", inp["id"]))
        elif kind == "cell":
            deps.append(("cell", inp["id"]))
        elif kind == "query":
            deps.append(("query", json.dumps(inp.get("query"), sort_keys=True)))
            for sid in sorted(set(inp.get("snapshots", {}).values())):
                deps.append(("snapshot", sid))  # keeps the scanned snapshots alive through GC
    return deps




def nfc(text: str) -> str:
    return unicodedata.normalize("NFC", text)
