"""Lowering of query calls (FIND, SYMBOLS, LINES, COLUMN, FIELD) to parameterised SQL over the
span store. Every query is pinned to the snapshots that were current at evaluation time, and
returns the contributing spans so aggregates stay traceable."""

from __future__ import annotations

import difflib
import json
import re
from dataclasses import dataclass, field
from decimal import Decimal
from typing import Any

from spanql.engine.errors import extraction, support, syntax
from spanql.formula.ast import BoolLit, Call, Compare, Node, NumberLit, StringLit, unparse
from spanql.ingest.logs import LEVELS
from spanql.store.db import Span, Store
from spanql.store.globs import glob_match

SOURCE_KINDS = {"doc", "log", "file", "code"}
BLOCK_KINDS = {
    "paragraph",
    "heading",
    "list_item",
    "code_fence",
    "table_row",
    "table_cell",
    "table_header",
    "log_record",
    "symbol",
    "chunk",
}
BLOCK_ALIASES = {
    "row": "table_row",
    "cell": "table_cell",
    "record": "log_record",
    "line": "log_record",
}
DEFAULT_EXCLUDED = ("table_cell", "table_header", "heading")
MAX_ROWS = 1_000_000
_DAY_RE = re.compile(r"^\d{4}-\d{2}-\d{2}$")
_TS_RE = re.compile(
    r"^\d{4}-\d{2}-\d{2}(?:[T ]\d{2}:\d{2}(?::\d{2}(?:\.\d+)?)?(?:Z|[+-]\d{2}:\d{2})?)?$"
)
SPECIAL_FIELDS = {"level": "r.level", "logger": "r.logger", "ts": "r.ts"}


@dataclass
class QueryResult:
    spans: list[Span]
    block_ids: list[int | None]
    uris: list[str]
    sql: str
    params: list[Any]
    label: str
    extra: dict[str, Any] = field(default_factory=dict)

    @property
    def snapshot_set(self) -> list[str]:
        return sorted({s.snapshot_id[:8] for s in self.spans})

    def record(self) -> dict[str, Any]:
        first = self.spans[0].id if self.spans else None
        return {
            "kind": "query",
            "query": self.label,
            "sql": self.sql,
            "params": self.params,
            "count": len(self.spans),
            "snapshot_set": self.snapshot_set,
            "spans": [s.id for s in self.spans[:50]],
            "first": first,
            "first_source": self.uris[0] if self.uris else None,
            **self.extra,
        }


def _lit(node: Node | None, name: str, fn: str) -> str:
    if node is None:
        raise syntax(f"{fn}: missing {name}")
    if not isinstance(node, StringLit):
        raise syntax(f"{fn}: {name} must be a string literal", pos=node.pos)
    return node.value


class _Builder:
    def __init__(self) -> None:
        self.where: list[str] = []
        self.params: list[Any] = []
        self.need_snapshot = False

    def add(self, clause: str, *params: Any) -> None:
        self.where.append(clause)
        self.params.extend(params)

    # FIELD("x") expression
    def field_expr(self, name: str) -> str:
        if name in SPECIAL_FIELDS:
            return SPECIAL_FIELDS[name]
        if name == "message":
            self.need_snapshot = True
            return (
                "CAST(substr(s.text, r.message_start + 1, r.message_end - r.message_start) AS TEXT)"
            )
        path = '$."' + name.replace('"', '\\"') + '"'
        return f"COALESCE(json_extract(r.fields, '{_sql_str(path)}'), json_extract(tr.fields, '{_sql_str(path)}'))"

    def predicate(self, node: Node, fn: str) -> str:
        """SQL for a predicate over FIELD(...) values."""
        if isinstance(node, Compare):
            return self._cmp(node.op, node.left, node.right, fn, node.pos)
        if isinstance(node, Call):
            if node.name in ("EQ", "GT", "LT") and len(node.args) == 2:
                op = {"EQ": "==", "GT": ">", "LT": "<"}[node.name]
                return self._cmp(op, node.args[0], node.args[1], fn, node.pos)
            if node.name == "CONTAINS" and len(node.args) == 2:
                fexpr = self._field_of(node.args[0], fn)
                needle = _lit(node.args[1], "CONTAINS needle", fn)
                self.params.append(needle)
                return f"instr(CAST({fexpr} AS TEXT), ?) > 0"
            if node.name in ("AND", "OR") and node.args:
                parts = [self.predicate(a, fn) for a in node.args]
                return "(" + f" {node.name} ".join(parts) + ")"
            if node.name == "NOT" and len(node.args) == 1:
                return f"NOT ({self.predicate(node.args[0], fn)})"
        if isinstance(node, BoolLit):
            return "1" if node.value else "0"
        raise syntax(
            f'{fn}: positional arguments must be predicates like FIELD("status") >= 500',
            pos=node.pos,
        )

    def _field_of(self, node: Node, fn: str) -> str:
        if isinstance(node, Call) and node.name == "FIELD" and len(node.args) == 1:
            return self.field_expr(_lit(node.args[0], "field name", "FIELD"))
        raise syntax(f'{fn}: predicate must compare FIELD("name") to a literal', pos=node.pos)

    def _cmp(self, op: str, left: Node, right: Node, fn: str, pos: int) -> str:
        flip = {">": "<", "<": ">", ">=": "<=", "<=": ">=", "==": "==", "!=": "!="}
        if not (isinstance(left, Call) and left.name == "FIELD"):
            left, right, op = right, left, flip[op]
        fexpr = self._field_of(left, fn)
        sql_op = {"==": "=", "!=": "!="}.get(op, op)
        if isinstance(right, NumberLit):
            self.params.append(float(right.value))
            return f"spanql_num({fexpr}) {sql_op} ?"
        if isinstance(right, StringLit):
            if op not in ("==", "!="):
                # ordered comparison on strings: timestamps etc.
                self.params.append(right.value)
                return f"CAST({fexpr} AS TEXT) {sql_op} ?"
            val = right.value
            if fexpr == "r.level":
                val = LEVELS.get(val.upper(), val.upper())
            self.params.append(val)
            return f"CAST({fexpr} AS TEXT) {sql_op} ?"
        if isinstance(right, BoolLit):
            self.params.append(1 if right.value else 0)
            return f"{fexpr} {sql_op} ?"
        raise syntax(f"{fn}: compare FIELD(...) with a number or string literal", pos=pos)


def _sql_str(s: str) -> str:
    return s.replace("'", "''")


def check_src(store: Store, pattern: str, fn: str, pos: int | None = None) -> None:
    """V5: a ``src:`` glob that matches no ingested source is an error, not "0 matches".

    Otherwise a typo (``logz/*.log``) silently counts nothing. The hint lists the closest
    source URIs so the model (or the user) can fix the glob."""
    uris = [r["uri"] for r in store.query("SELECT uri FROM source WHERE removed = 0 ORDER BY uri")]
    if any(glob_match(pattern, u) for u in uris):
        return
    if not uris:
        raise support(
            f"{fn}: src:{pattern!r} matches no source; nothing has been ingested", pos=pos
        )
    target = pattern.replace("**/", "").replace("*", "")
    ranked = sorted(
        uris,
        key=lambda u: (
            -max(
                difflib.SequenceMatcher(None, target, u).ratio(),
                difflib.SequenceMatcher(None, target, u.split("/", 1)[-1]).ratio(),
            ),
            u,
        ),
    )
    raise support(
        f"{fn}: src:{pattern!r} matches no ingested source",
        hint="did you mean " + ", ".join(repr(u) for u in ranked[:3]) + "?",
        pos=pos,
    )


_BASE = (
    "SELECT b.id, b.snapshot_id, b.start, b.end, src.uri FROM block b\n"
    "JOIN source src ON src.current_snapshot = b.snapshot_id AND src.removed = 0\n"
    "{snapshot}"
    "LEFT JOIN log_record r ON r.block_id = b.id\n"
    "LEFT JOIN table_row tr ON tr.block_id = b.id\n"
    "{extra}"
)


def _common(
    store: Store, bld: _Builder, call: Call, fn: str, *, default_kinds: bool = True
) -> None:
    src = call.kw("src")
    if src is not None:
        pattern = _lit(src, "src", fn)
        check_src(store, pattern, fn, src.pos)
        bld.add("spanql_glob(?, src.uri)", pattern)
    kind = call.kw("kind")
    kind_v = _lit(kind, "kind", fn) if kind is not None else None
    if fn == "FIND":
        if kind_v is None:
            if default_kinds:
                bld.add(
                    f"b.kind NOT IN ({', '.join('?' * len(DEFAULT_EXCLUDED))})", *DEFAULT_EXCLUDED
                )
        else:
            parts = [p.strip() for p in kind_v.split("|") if p.strip()]
            src_kinds = [p for p in parts if p in SOURCE_KINDS]
            blk_kinds = [BLOCK_ALIASES.get(p, p) for p in parts if p not in SOURCE_KINDS]
            bad = [b for b in blk_kinds if b not in BLOCK_KINDS]
            if bad or not parts or (src_kinds and blk_kinds):
                raise syntax(
                    f"FIND: unknown kind {kind_v!r}",
                    hint=f"use source kinds {sorted(SOURCE_KINDS)} or block kinds "
                    f"{sorted(BLOCK_KINDS)}, optionally joined with |",
                )
            if src_kinds:
                bld.add(f"src.kind IN ({', '.join('?' * len(src_kinds))})", *src_kinds)
                if default_kinds:
                    bld.add(
                        f"b.kind NOT IN ({', '.join('?' * len(DEFAULT_EXCLUDED))})",
                        *DEFAULT_EXCLUDED,
                    )
            else:
                bld.add(f"b.kind IN ({', '.join('?' * len(blk_kinds))})", *blk_kinds)
    if (rx := call.kw("re")) is not None:
        pattern = _lit(rx, "re", fn)
        try:
            re.compile(pattern)
        except re.error as exc:
            raise syntax(f"{fn}: invalid regex {pattern!r}: {exc}", pos=rx.pos) from exc
        bld.need_snapshot = True
        bld.add("regexp(?, substr(s.text, b.start + 1, b.end - b.start))", pattern)


def _fts_query(text: str) -> str:
    words = re.findall(r"[\w.\-]+", text)
    if not words:
        raise syntax("match: needs at least one word")
    return " ".join('"' + w.replace('"', "") + '"' for w in words)


def run_find(store: Store, call: Call) -> QueryResult:
    bld = _Builder()
    _common(store, bld, call, "FIND")
    if (m := call.kw("match")) is not None:
        bld.add(
            "b.id IN (SELECT rowid FROM block_fts WHERE block_fts MATCH ?)",
            _fts_query(_lit(m, "match", "FIND")),
        )
    if (d := call.kw("day")) is not None:
        day = _lit(d, "day", "FIND")
        if not _DAY_RE.match(day):
            raise syntax(f"FIND: day must be YYYY-MM-DD, got {day!r}", pos=d.pos)
        bld.add("substr(r.ts, 1, 10) = ?", day)
    for key, op in (("since", ">="), ("until", "<")):
        if (t := call.kw(key)) is not None:
            ts = _lit(t, key, "FIND")
            if not _TS_RE.match(ts):
                raise syntax(f"FIND: {key} must be an ISO date or date-time, got {ts!r}", pos=t.pos)
            bld.add(f"replace(r.ts, ' ', 'T') {op} ?", ts.replace(" ", "T"))
    if (lv := call.kw("level")) is not None:
        levels = [
            LEVELS.get(x.strip().upper(), x.strip().upper())
            for x in re.split(r"[|,]", _lit(lv, "level", "FIND"))
            if x.strip()
        ]
        bld.add(f"r.level IN ({', '.join('?' * len(levels))})", *levels)
    if (lg := call.kw("logger")) is not None:
        bld.add("r.logger GLOB ?", _lit(lg, "logger", "FIND"))
    for pred in call.args:
        bld.where.append(bld.predicate(pred, "FIND"))
    return _execute(store, bld, call, "")


def run_symbols(store: Store, call: Call) -> QueryResult:
    bld = _Builder()
    _common(store, bld, call, "SYMBOLS")
    if (k := call.kw("kind")) is not None:
        kv = _lit(k, "kind", "SYMBOLS")
        kinds: set[str] = set()
        for part in kv.split("|"):
            part = part.strip()
            if part in ("callable", "function_or_method"):
                kinds |= {"function", "method"}
            elif part:
                kinds.add(part)
        bld.add(f"cs.kind IN ({', '.join('?' * len(kinds))})", *sorted(kinds))
    for key, col in (("name", "cs.name"), ("qualified", "cs.qualified_name")):
        if (v := call.kw(key)) is not None:
            bld.add(f"{col} GLOB ?", _lit(v, key, "SYMBOLS"))
    if (c := call.kw("calls")) is not None:
        bld.add(
            "EXISTS (SELECT 1 FROM json_each(cs.calls) j WHERE j.value = ?)",
            _lit(c, "calls", "SYMBOLS"),
        )
    if (lang := call.kw("lang")) is not None:
        bld.add("cs.lang = ?", _lit(lang, "lang", "SYMBOLS"))
    if call.args:
        raise syntax("SYMBOLS takes only keyword arguments", pos=call.pos)
    return _execute(
        store,
        bld,
        call,
        "JOIN code_symbol cs ON cs.block_id = b.id\n",
        select_extra=", cs.qualified_name",
    )


def run_column(store: Store, call: Call) -> QueryResult:
    if not call.args:
        raise syntax('COLUMN needs a header, e.g. COLUMN(src:"data.csv", "amount")', pos=call.pos)
    header = _lit(call.args[0], "header", "COLUMN")
    bld = _Builder()
    if (src := call.kw("src")) is not None:
        pattern = _lit(src, "src", "COLUMN")
        check_src(store, pattern, "COLUMN", src.pos)
        bld.add("spanql_glob(?, src.uri)", pattern)
    for pred in call.args[1:]:
        bld.where.append(bld.predicate(pred, "COLUMN"))
    where = " AND ".join(bld.where) if bld.where else "1"
    sql = (
        "SELECT c.id, c.snapshot_id, c.start, c.end, src.uri FROM table_cell tc\n"
        "JOIN block c ON c.id = tc.block_id\n"
        "JOIN source src ON src.current_snapshot = c.snapshot_id AND src.removed = 0\n"
        "JOIN snapshot s ON s.id = c.snapshot_id\n"
        "JOIN table_row tr ON tr.block_id = tc.row_block\n"
        "LEFT JOIN log_record r ON r.block_id = tc.row_block\n"
        f"WHERE tc.header = ? COLLATE NOCASE AND {where}\n"
        "ORDER BY src.uri, c.start"
    )
    params = [header, *bld.params]
    rows = store.query(sql, params)
    if not rows:
        _check_header_exists(store, header, call)
    return QueryResult(
        [Span(r["snapshot_id"], r["start"], r["end"]) for r in rows],
        [r["id"] for r in rows],
        [r["uri"] for r in rows],
        sql,
        params,
        unparse(call),
        {"header": header},
    )


def _check_header_exists(store: Store, header: str, call: Call) -> None:
    """Turn 'no such column' into an extraction error listing real headers (useful feedback)."""
    src = call.kw("src")
    pattern = _lit(src, "src", "COLUMN") if src is not None else None
    rows = store.query(
        "SELECT DISTINCT tc.header, src.uri FROM table_cell tc\n"
        "JOIN block c ON c.id = tc.block_id\n"
        "JOIN source src ON src.current_snapshot = c.snapshot_id AND src.removed = 0\n"
        + ("WHERE spanql_glob(?, src.uri)" if pattern else ""),
        [pattern] if pattern else [],
    )
    headers = sorted({r["header"] for r in rows if r["header"]})
    if headers and header.lower() not in {h.lower() for h in headers}:
        raise extraction(f"COLUMN: no column {header!r}; columns are {headers[:20]}", pos=call.pos)


def run_lines(store: Store, call: Call) -> QueryResult:
    rx = call.kw("re")
    pattern = _lit(rx, "re", "LINES")
    try:
        creg = re.compile(pattern)
    except re.error as exc:
        raise syntax(f"LINES: invalid regex {pattern!r}: {exc}") from exc
    where, params = ["src.removed = 0", "src.current_snapshot IS NOT NULL"], []
    if (src := call.kw("src")) is not None:
        pattern = _lit(src, "src", "LINES")
        check_src(store, pattern, "LINES", src.pos)
        where.append("spanql_glob(?, src.uri)")
        params.append(pattern)
    if (k := call.kw("kind")) is not None:
        where.append("src.kind = ?")
        params.append(_lit(k, "kind", "LINES"))
    sql = f"SELECT src.uri, src.current_snapshot FROM source src WHERE {' AND '.join(where)} ORDER BY src.uri"
    spans: list[Span] = []
    uris: list[str] = []
    for row in store.query(sql, params):
        data = store.snapshot_text(row["current_snapshot"])
        pos = 0
        for line in data.split(b"\n"):
            text = line.decode("utf-8", "replace")
            if creg.search(text):
                spans.append(Span(row["current_snapshot"], pos, pos + len(line)))
                uris.append(row["uri"])
            pos += len(line) + 1
    return QueryResult(
        spans,
        [None] * len(spans),
        uris,
        sql + f"\n-- then line scan: re {pattern!r}",
        params,
        unparse(call),
    )


def _execute(
    store: Store, bld: _Builder, call: Call, extra_join: str, select_extra: str = ""
) -> QueryResult:
    snap = "JOIN snapshot s ON s.id = b.snapshot_id\n" if bld.need_snapshot else ""
    sql = _BASE.format(snapshot=snap, extra=extra_join)
    if select_extra:
        sql = sql.replace("src.uri FROM block b", f"src.uri{select_extra} FROM block b", 1)
    if bld.where:
        sql += "WHERE " + "\n  AND ".join(bld.where) + "\n"
    sql += f"ORDER BY src.uri, b.start\nLIMIT {MAX_ROWS}"
    rows = store.query(sql, bld.params)
    res = QueryResult(
        [Span(r["snapshot_id"], r["start"], r["end"]) for r in rows],
        [r["id"] for r in rows],
        [r["uri"] for r in rows],
        sql,
        list(bld.params),
        unparse(call),
    )
    if select_extra:
        res.extra["names"] = [r["qualified_name"] for r in rows]
    return res


def field_values(store: Store, name: str, result: QueryResult) -> list[tuple[Any, Span]]:
    """Values of field ``name`` for each record/row in ``result``, each with the most precise
    span available (the table cell, the ``name=value`` text in a log line, or the record)."""
    out: list[tuple[Any, Span]] = []
    for bid, sp in zip(result.block_ids, result.spans, strict=True):
        if bid is None:
            continue
        row = store.one(
            "SELECT r.fields AS lf, r.level, r.logger, r.ts, tr.fields AS tf FROM block b"
            " LEFT JOIN log_record r ON r.block_id = b.id"
            " LEFT JOIN table_row tr ON tr.block_id = b.id WHERE b.id = ?",
            (bid,),
        )
        if row is None:
            continue
        if name in SPECIAL_FIELDS and row[name] is not None:
            out.append((row[name], _locate(store, sp, str(row[name]), None)))
            continue
        if row["tf"] is not None:
            fields = json.loads(row["tf"])
            if name in fields:
                cell = store.one(
                    "SELECT c.snapshot_id, c.start, c.end FROM table_cell tc JOIN block c"
                    " ON c.id = tc.block_id WHERE tc.row_block = ? AND tc.header = ?",
                    (bid, name),
                )
                csp = Span(cell["snapshot_id"], cell["start"], cell["end"]) if cell else sp
                out.append((fields[name], csp))
            continue
        if row["lf"] is not None:
            fields = json.loads(row["lf"])
            if name in fields:
                out.append((fields[name], _locate(store, sp, fields[name], name)))
    return out


def _locate(store: Store, sp: Span, value: Any, name: str | None) -> Span:
    data = store.snapshot_text(sp.snapshot_id)[sp.start : sp.end]
    val = str(value).encode("utf-8")
    if isinstance(value, bool):
        val = b"true" if value else b"false"
    candidates: list[bytes] = []
    if name:
        n = name.encode("utf-8")
        candidates += [
            n + b"=" + val,
            n + b'="' + val + b'"',
            b'"' + n + b'": ' + val,
            b'"' + n + b'":' + val,
            b'"' + n + b'": "' + val + b'"',
            b'"' + n + b'":"' + val + b'"',
        ]
    for cand in candidates:
        i = data.find(cand)
        if i != -1 and data.find(cand, i + 1) == -1:
            j = i + cand.rfind(val)
            return Span(sp.snapshot_id, sp.start + j, sp.start + j + len(val))
    i = data.find(val)
    if i != -1 and data.find(val, i + 1) == -1:
        return Span(sp.snapshot_id, sp.start + i, sp.start + i + len(val))
    return sp


def as_decimal(v: Any) -> Decimal | None:
    if isinstance(v, bool):
        return None
    if isinstance(v, int):
        return Decimal(v)
    if isinstance(v, float):
        return Decimal(repr(v))
    return None
