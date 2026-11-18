"""Compile query calls (FIND / COLUMN / SYMBOLS / LINES) to SQL over the span store.

All values reach SQLite as bound parameters; nothing from a formula is ever
interpolated into SQL text. Scans run over a snapshot set that is pinned when the
query starts and recorded in the cell, so re-evaluation is reproducible.
"""

from __future__ import annotations

import difflib
import re
import sqlite3
from dataclasses import dataclass, field
from decimal import Decimal

from ..errors import EmptySupportError, EvalError, FormulaTypeError
from ..formula.ast import Call, Num, Str
from ..formula.predicate import PAnd, PCmp, PContains, PNot, POr, PTrue, Pred, to_predicate
from ..ingest.logs import LEVEL_ALIASES, parse_timestamp
from ..spans import Span
from ..store import Store
from .values import Item

SOURCE_KINDS = ("doc", "log", "file", "code")
SYMBOL_KINDS = ("function", "method", "class", "module")
MAX_MATERIALIZE = 200_000  # rows an aggregate may read
DEFAULT_ITEMS = 2_000  # rows materialized for display / extraction
SAMPLE = 50  # contributing spans kept inline in a trace
MAX_LINES = 2_000
QUERY_TIMEOUT_S = 30.0

_SQLOP = {"==": "=", "!=": "<>", ">": ">", ">=": ">=", "<": "<", "<=": "<="}


def check_regex(pattern: str) -> None:
    """Compile ``pattern`` and reject nested unbounded repetition such as ``(a+)+$``.

    Python's ``re`` has no timeout and runs inside a SQL callback that cannot be interrupted, so a
    catastrophic pattern would hang the worker. Nested unbounded repeats are the classic
    exponential family; rejecting them structurally is cheap and accurate for it.
    """
    try:
        re.compile(pattern)
    except re.error as e:
        raise EvalError(f"`re:` is not a valid regular expression: {e}",
                        hint="Python regex syntax; escape backslashes inside the quoted string") from e
    try:
        from re import _constants as C, _parser as P  # stdlib internals; skip the guard if they move
    except ImportError:  # pragma: no cover
        return

    def unbounded_inside(sub) -> bool:
        for op, av in sub:
            if op in (C.MAX_REPEAT, C.MIN_REPEAT) and av[1] == C.MAXREPEAT:
                return True
            if any(unbounded_inside(x) for x in _children(op, av)):
                return True
        return False

    def _children(op, av):
        if op in (C.MAX_REPEAT, C.MIN_REPEAT):
            return [av[2]]
        if op == C.SUBPATTERN:
            return [av[3]]
        if op == C.BRANCH:
            return list(av[1])
        if op in (C.ASSERT, C.ASSERT_NOT):
            return [av[1]]
        if op == C.ATOMIC_GROUP:
            return [av]
        return []

    def risky(sub) -> bool:
        for op, av in sub:
            if op in (C.MAX_REPEAT, C.MIN_REPEAT) and av[1] == C.MAXREPEAT and unbounded_inside(av[2]):
                return True
            if any(risky(x) for x in _children(op, av)):
                return True
        return False

    try:
        tree = P.parse(pattern)
    except Exception:  # pragma: no cover - compile() already accepted it
        return
    if risky(tree):
        raise EvalError(
            f"`re:` {pattern!r} repeats a repeated group (like (a+)+), which can take exponential time",
            hint="simplify it: drop the outer repeat, bound it like {1,5}, or use contains:\"...\"")


@dataclass(frozen=True)
class ScanSpec:
    op: str  # FIND | COLUMN | SYMBOLS | LINES
    src: str | None = None
    kind: str | None = None
    regex: str | None = None
    contains: str | None = None
    day: str | None = None
    since: str | None = None  # normalized UTC (fixed width)
    until: str | None = None
    level: str | None = None
    logger: str | None = None
    name: str | None = None
    calls: str | None = None
    column: str | None = None
    line_from: int | None = None
    line_to: int | None = None
    pred: Pred | None = field(default=None, compare=False, hash=False)
    pred_text: str | None = None

    def describe(self) -> dict:
        d = {"op": self.op}
        for k in ("src", "kind", "regex", "contains", "day", "since", "until", "level", "logger", "name", "calls",
                  "column", "line_from", "line_to"):
            v = getattr(self, k)
            if v is not None:
                d[k] = v
        if self.pred_text:
            d["where"] = self.pred_text
        return d


@dataclass(slots=True)
class ScanResult:
    items: list[Item]
    total: int
    snapshots: dict[str, str]  # uri -> snapshot id actually scanned
    matched_sources: int
    blocks_searched: int
    sample: list[str]  # first SAMPLE contributing span ids


# --------------------------------------------------------------- compilation


def _literal(node, kw: str, want: type):
    if want is str and isinstance(node, Str):
        return node.value
    if want is int and isinstance(node, Num) and node.value == node.value.to_integral_value():
        return int(node.value)
    raise FormulaTypeError(f"`{kw}:` must be a {'quoted string' if want is str else 'whole number'} literal, "
                           "not a computed value", pos=node.pos)


def _ts(value: str, kw: str) -> str:
    ts = parse_timestamp(value if "T" in value or " " in value else value + "T00:00:00")
    if ts is None:
        raise EvalError(f"`{kw}:` {value!r} is not a date or timestamp (use 2026-11-10 or 2026-11-10T09:00:00Z)")
    return ts.utc


def compile_scan(call: Call, source_text: str) -> ScanSpec:
    """Validate a query call and turn it into a :class:`ScanSpec`."""
    kw: dict = {}
    for name, node in call.kwargs:
        kw[name] = _literal(node, name, int if name in ("from", "to") else str)
    preds = [to_predicate(a) for a in (call.args[1:] if call.name == "COLUMN" else call.args)] \
        if call.name in ("FIND", "COLUMN") else []
    pred = None if not preds else (preds[0] if len(preds) == 1 else PAnd(tuple(preds)))
    pred_text = None
    if preds:
        arg_nodes = call.args[1:] if call.name == "COLUMN" else call.args
        pred_text = " AND ".join(source_text[a.pos : a.end] for a in arg_nodes)
    src = kw.get("src")
    if src is not None and not src.strip():
        raise EvalError("`src:` is empty")

    if call.name == "FIND":
        regex = kw.get("re")
        if regex is not None:
            check_regex(regex)
        if kw.get("kind") and kw["kind"] not in SOURCE_KINDS and not re.fullmatch(r"[a-z_]{2,20}", kw["kind"]):
            raise EvalError(f"`kind:` {kw['kind']!r} is not a source kind ({', '.join(SOURCE_KINDS)}) or block kind")
        if kw.get("day") is not None and not re.fullmatch(r"\d{4}-\d{2}-\d{2}", kw["day"]):
            raise EvalError(f"`day:` must look like 2026-11-10, got {kw['day']!r}")
        level = kw.get("level")
        if level is not None:
            level = LEVEL_ALIASES.get(level.strip().upper(), level.strip().upper())
        return ScanSpec("FIND", src=src, kind=kw.get("kind"), regex=regex, contains=kw.get("contains"),
                        day=kw.get("day"), since=_ts(kw["since"], "since") if "since" in kw else None,
                        until=_ts(kw["until"], "until") if "until" in kw else None, level=level,
                        logger=kw.get("logger"), pred=pred, pred_text=pred_text)
    if call.name == "COLUMN":
        col = call.args[0].value if call.args and isinstance(call.args[0], Str) else None
        if not col:
            raise EvalError("COLUMN needs the column name as its first argument")
        return ScanSpec("COLUMN", src=src, column=col, pred=pred, pred_text=pred_text)
    if call.name == "SYMBOLS":
        kind = kw.get("kind")
        if kind is not None and kind not in SYMBOL_KINDS:
            raise EvalError(f"`kind:` for SYMBOLS must be one of {', '.join(SYMBOL_KINDS)}, got {kind!r}")
        regex = kw.get("re")
        if regex is not None:
            check_regex(regex)
        return ScanSpec("SYMBOLS", src=src, kind=kind, name=kw.get("name"), calls=kw.get("calls"), regex=regex)
    if call.name == "LINES":
        a, b = kw.get("from"), kw.get("to")
        if a is None or b is None:
            raise EvalError("LINES needs from: and to: line numbers")
        if a < 1 or b < a:
            raise EvalError(f"LINES range {a}..{b} is invalid (1-based, from <= to)")
        if b - a + 1 > MAX_LINES:
            raise EvalError(f"LINES may cover at most {MAX_LINES} lines")
        return ScanSpec("LINES", src=src, line_from=a, line_to=b)
    raise EvalError(f"{call.name} is not a query function")


# ---------------------------------------------------------------------- SQL


class _Builder:
    """Accumulates SQL text and bound parameters in the same order."""

    def __init__(self, op: str):
        self.op = op
        self.params: list = []
        self.has_log = op == "FIND"

    def rowfields(self) -> str:
        return "COALESCE(r.fields, t.fields)" if self.op == "FIND" else "t.fields"

    def fexpr(self, name: str) -> str:
        if self.has_log:
            if name == "level":
                return "r.level"
            if name == "logger":
                return "r.logger"
            if name == "day":
                return "r.day"
            if name == "message":
                return ("COALESCE(json_extract(r.fields, '$.message'), "
                        "spanql_text(b.snapshot_id, r.message_start, r.message_end))")
        f = self.rowfields()
        self.params.append(f'$."{name}"')
        if "." in name:
            self.params.append(f"$.{name}")
            return f"COALESCE(json_extract({f}, ?), json_extract({f}, ?))"
        return f"json_extract({f}, ?)"

    def pred(self, p: Pred) -> str:
        if isinstance(p, PTrue):
            return "1" if p.value else "0"
        if isinstance(p, PAnd):
            return "(" + " AND ".join(self.pred(i) for i in p.items) + ")"
        if isinstance(p, POr):
            return "(" + " OR ".join(self.pred(i) for i in p.items) + ")"
        if isinstance(p, PNot):
            return "NOT (" + self.pred(p.item) + ")"
        if isinstance(p, PContains):
            e = self.fexpr(p.field)
            self.params.append(p.text)
            return f"instr(CAST({e} AS TEXT), ?) > 0"
        if isinstance(p, PCmp):
            op = _SQLOP[p.op]
            if p.field == "ts" and self.has_log:
                ts = parse_timestamp(str(p.value))
                if ts is None:
                    raise EvalError(f"FIELD(\"ts\") must be compared with a timestamp, got {p.value!r}")
                self.params.append(ts.utc)
                return f"r.ts_utc {op} ?"
            e = self.fexpr(p.field)
            if isinstance(p.value, bool):
                self.params.append(int(p.value))
                return f"{e} {op} ?"
            if isinstance(p.value, Decimal):
                self.params.append(float(p.value))
                return f"spanql_num({e}) {op} ?"
            value = p.value
            if p.field == "level" and self.has_log:
                value = LEVEL_ALIASES.get(value.strip().upper(), value.strip().upper())
            self.params.append(value)
            return f"CAST({e} AS TEXT) {op} ?"
        raise EvalError(f"unsupported filter {p!r}")


def _sources_for(store: Store, spec: ScanSpec, pins: dict[str, str] | None) -> list[tuple[int, str, str]]:
    """(source_id, uri, snapshot_id) triples to scan."""
    rows: list[tuple[int, str, str, str]] = []  # (id, uri, snapshot, source kind)
    if pins is not None:
        from ..globs import match_uri

        for uri, sid in sorted(pins.items()):
            if spec.src is None or match_uri(spec.src, uri):
                row = store.get_source(uri)
                if row is not None:
                    rows.append((row.id, uri, sid, row.kind))
    else:
        rows = [(s.id, s.uri, s.current_snapshot, s.kind) for s in store.list_sources(spec.src)]
    if spec.op == "FIND" and spec.kind in SOURCE_KINDS:
        rows = [r for r in rows if r[3] == spec.kind]
    out = [r[:3] for r in rows]
    if not out:
        _no_sources(store, spec)
    return out


def _no_sources(store: Store, spec: ScanSpec) -> None:
    uris = [s.uri for s in store.list_sources()]
    glob = spec.src or "(all sources)"
    close = difflib.get_close_matches((spec.src or "").split("*")[0] or "", uris, n=3, cutoff=0.4)
    sample = close or uris[:5]
    kind = f" of kind {spec.kind!r}" if spec.kind in SOURCE_KINDS else ""
    raise EmptySupportError(
        f"src:{glob!r} matches no ingested source{kind}",
        hint=("ingested sources include: " + ", ".join(sample)) if sample else "nothing has been ingested yet",
    )


def _prepare(store: Store, triples: list[tuple[int, str, str]]) -> None:
    store.db.execute("CREATE TEMP TABLE IF NOT EXISTS scan_set(source_id INTEGER, uri TEXT, snapshot_id TEXT)")
    store.db.execute("DELETE FROM scan_set")
    store.db.executemany("INSERT INTO scan_set VALUES (?,?,?)", triples)


def run_scan(
    store: Store,
    spec: ScanSpec,
    *,
    pins: dict[str, str] | None = None,
    limit: int | None = DEFAULT_ITEMS,
    timeout: float | None = QUERY_TIMEOUT_S,
) -> ScanResult:
    """Execute a scan. ``limit=0`` counts without fetching rows (COUNT pushdown)."""
    if spec.op == "LINES":
        return _run_lines(store, spec, pins)
    triples = _sources_for(store, spec, pins)
    _prepare(store, triples)
    snapshots = {uri: sid for _, uri, sid in triples}
    b = _Builder(spec.op)

    joins = "FROM scan_set ss JOIN block b ON b.snapshot_id = ss.snapshot_id"
    base: list[str] = []
    base_params: list = []
    label = "NULL"
    if spec.op == "FIND":
        joins += " LEFT JOIN log_record r ON r.block_id = b.id LEFT JOIN table_row t ON t.block_id = b.id"
        if spec.kind and spec.kind not in SOURCE_KINDS:
            base.append("b.kind = ?")
            base_params.append(spec.kind)
        else:
            base.append("b.is_unit = 1")
    elif spec.op == "COLUMN":
        joins += (" JOIN table_cell tc ON tc.block_id = b.id JOIN table_row t ON t.block_id = tc.row_block")
        base.append("tc.header = ? COLLATE NOCASE")
        base_params.append(spec.column)
    elif spec.op == "SYMBOLS":
        joins += " JOIN code_symbol cs ON cs.block_id = b.id"
        label = "cs.qualified_name"
        if spec.kind == "function":
            base.append("cs.kind IN ('function', 'method')")
        elif spec.kind:
            base.append("cs.kind = ?")
            base_params.append(spec.kind)

    conds: list[str] = []
    if spec.op == "FIND":
        for col, val in (("r.day", spec.day), ("r.level", spec.level), ("r.logger", spec.logger)):
            if val is not None:
                conds.append(f"{col} = ?")
                b.params.append(val)
        if spec.since is not None:
            conds.append("r.ts_utc >= ?")
            b.params.append(spec.since)
        if spec.until is not None:
            conds.append("r.ts_utc < ?")
            b.params.append(spec.until)
    if spec.op == "SYMBOLS":
        if spec.name is not None:
            conds.append("(cs.name = ? OR cs.qualified_name = ?)")
            b.params += [spec.name, spec.name]
        if spec.calls is not None:
            conds.append("EXISTS (SELECT 1 FROM json_each(cs.calls) j WHERE j.value = ? OR j.value LIKE ? ESCAPE '\\')")
            esc = spec.calls.replace("\\", "\\\\").replace("%", "\\%").replace("_", "\\_")
            b.params += [spec.calls, "%." + esc]
    if spec.regex is not None:
        conds.append('spanql_re(?, b.snapshot_id, b.start, b."end")')
        b.params.append(spec.regex)
    if spec.contains is not None:
        conds.append('instr(spanql_text(b.snapshot_id, b.start, b."end"), ?) > 0')
        b.params.append(spec.contains)
    if spec.pred is not None:
        conds.append(b.pred(spec.pred))

    where_base = " AND ".join(base) if base else "1"
    where_all = " AND ".join(base + conds) if (base or conds) else "1"
    base_sql = f"SELECT COUNT(*) {joins} WHERE {where_base}"
    count_sql = f"SELECT COUNT(*) {joins} WHERE {where_all}"
    # parameter order in where_all: base params first, then the condition params
    all_params = base_params + b.params

    try:
        with store.deadline(timeout):
            searched = store.db.execute(base_sql, base_params).fetchone()[0]
            total = store.db.execute(count_sql, all_params).fetchone()[0]
            n_fetch = SAMPLE if limit == 0 else (total if limit is None else min(limit, total))
            n_fetch = max(n_fetch, 0)
            rows = []
            if n_fetch:
                rows = store.db.execute(
                    f'SELECT b.id, b.snapshot_id, b.start, b."end", b.text_sha256, b.kind, b.path, b.line_start, '
                    f"ss.uri, {label} AS label {joins} WHERE {where_all} ORDER BY ss.uri, b.start, b.id LIMIT ?",
                    all_params + [n_fetch],
                ).fetchall()
    except sqlite3.OperationalError as e:
        if "interrupt" in str(e).lower():
            raise EvalError(f"query ran longer than {timeout:.0f}s and was stopped",
                            hint="narrow it with src:, day:, since:/until: or a more specific re:") from e
        raise EvalError(f"query failed: {e}") from e

    if spec.op == "COLUMN" and searched == 0:
        heads = [r[0] for r in store.db.execute(
            "SELECT DISTINCT tc.header FROM scan_set ss JOIN block b ON b.snapshot_id = ss.snapshot_id "
            "JOIN table_cell tc ON tc.block_id = b.id ORDER BY 1 LIMIT 30")]
        raise EmptySupportError(f"no column named {spec.column!r} in the matched sources",
                                hint=("columns available: " + ", ".join(heads)) if heads
                                else "the matched sources have no table columns")
    items = [Item(Span(r["snapshot_id"], r["start"], r["end"], r["text_sha256"]), r["uri"], r["kind"], r["label"],
                  r["line_start"], r["path"]) for r in rows]
    return ScanResult(items if limit != 0 else [], total, snapshots, len(triples), searched,
                      [i.span.id for i in items[:SAMPLE]])


def _run_lines(store: Store, spec: ScanSpec, pins: dict[str, str] | None) -> ScanResult:
    triples = _sources_for(store, spec, pins)
    if len(triples) != 1:
        raise EvalError(f"LINES needs src: to match exactly one source, but {spec.src!r} matched {len(triples)}",
                        hint="use the full path, e.g. src:\"logs/app.log\"")
    _, uri, sid = triples[0]
    span = store.span_for_lines(sid, spec.line_from, spec.line_to)
    first, last = store.line_range(span)
    item = Item(span, uri, "lines", f"{uri}:L{first}-L{last}", first)
    return ScanResult([item], 1, {uri: sid}, 1, 1, [span.id])
