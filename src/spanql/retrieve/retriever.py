"""Hybrid retrieval: FTS5 BM25 + structural lookups -> numbered candidate handles.

The candidates are the *only* spans the model may cite (the grammar admits only
``$1..$k``). Vectors are opt-in and not implemented here (BM25 is the default).
"""

from __future__ import annotations

import json
import re
from dataclasses import dataclass, field
from datetime import date

from ..engine.resolve import HandleTable
from ..globs import match_uri
from ..spans import Span
from ..store import Store, describe_sources
from .dates import date_hints

STOPWORDS = set(
    "a an and are as at be been but by can could did do does for from had has have how i if in into is it its of "
    "on or our over per than that the their them then there these they this to was we were what when where which "
    "who whom whose why will with would you your me my many much most any all each every about during after "
    "before between total number amount please tell show give find".split()
)
EXCERPT_CHARS = 420
MAX_SCHEMA_SOURCES = 6
MAX_CATALOG = 40


@dataclass(slots=True)
class Candidate:
    handle: int
    span: Span
    uri: str
    kind: str
    line_start: int
    line_end: int
    path: str | None
    excerpt: str
    score: float

    def location(self) -> str:
        return f"L{self.line_start}" if self.line_start == self.line_end else f"L{self.line_start}-L{self.line_end}"

    def to_json(self) -> dict:
        return {"handle": f"${self.handle}", "span": self.span.id, "source": self.uri, "kind": self.kind,
                "lines": [self.line_start, self.line_end], "path": self.path, "excerpt": self.excerpt}


@dataclass(slots=True)
class Retrieval:
    candidates: list[Candidate]
    handles: HandleTable
    terms: list[str]
    fts_query: str
    sources_searched: int
    blocks_searched: int
    schemas: list[dict] = field(default_factory=list)
    catalog: list[dict] = field(default_factory=list)
    date_hints: dict[str, str] = field(default_factory=dict)
    near_misses: list[Candidate] = field(default_factory=list)  # ranked hits that did not fit the budget

    def report(self) -> dict:
        """The traceable 'what I searched' behind any refusal."""
        top = self.candidates[:5] or self.near_misses[:5]
        return {
            "terms": self.terms, "query": self.fts_query, "sources_searched": self.sources_searched,
            "blocks_searched": self.blocks_searched, "candidates": len(self.candidates),
            "near_misses": [c.to_json() for c in top],
        }


def extract_terms(question: str) -> list[str]:
    """Keywords for BM25: lowercase word tokens minus stopwords, in first-seen order."""
    seen: dict[str, None] = {}
    for tok in re.findall(r"[\w]+", question.lower()):
        if tok in STOPWORDS or (len(tok) < 2 and not tok.isdigit()):
            continue
        seen.setdefault(tok, None)
    return list(seen)


def _phrases(question: str) -> list[str]:
    """Adjacent content words as they appear in the question ("default model"): phrase hits outrank scattered ones."""
    toks = re.findall(r"[\w]+", question.lower())
    out: list[str] = []
    for a, b in zip(toks, toks[1:]):
        if a not in STOPWORDS and b not in STOPWORDS and len(a) > 1 and len(b) > 1:
            out.append(f"{a} {b}")
    return list(dict.fromkeys(out))[:6]


def _fts_query(terms: list[str], phrases: list[str] | None = None) -> str:
    parts = ['"' + t.replace('"', "") + '"' for t in terms]
    parts += ['"' + p.replace('"', "") + '"' for p in (phrases or [])]
    return " OR ".join(parts)


def _identifiers(question: str) -> list[str]:
    """Code-like tokens (backticked, snake_case, CamelCase, dotted) worth a direct symbol lookup."""
    out = re.findall(r"`([^`]{2,80})`", question)
    out += [t for t in re.findall(r"[A-Za-z_][\w.]*", question)
            if "_" in t or re.search(r"[a-z][A-Z]", t) or ("." in t.strip(".") and not t.endswith("."))]
    seen: dict[str, None] = {}
    for t in out:
        seen.setdefault(t.strip("`. "), None)
    return [t for t in seen if t]


def _excerpt(text: str, terms: list[str], n: int = EXCERPT_CHARS) -> str:
    text = text.strip()
    if len(text) <= n:
        return text
    low = text.lower()
    pos = min((p for p in (low.find(t) for t in terms) if p >= 0), default=0)
    start = max(0, pos - n // 3)
    chunk = text[start : start + n]
    return ("…" if start else "") + chunk + ("…" if start + n < len(text) else "")


def _display_text(store: Store, row, terms: list[str]) -> str:
    """Excerpt of a block; table rows are shown as ``column=value`` pairs so the model sees headers."""
    data = store.snapshot_bytes(row["snapshot_id"])
    text = data[row["start"] : row["end"]].decode("utf-8", "replace")
    if row["kind"] == "table_row":
        f = store.db.execute("SELECT fields FROM table_row WHERE block_id=?", (row["id"],)).fetchone()
        if f:
            pairs = [f"{k}={v}" for k, v in json.loads(f[0]).items() if not isinstance(v, (dict, list))]
            return _excerpt("; ".join(pairs) + "  | raw: " + text, terms)
    return _excerpt(text, terms)


def retrieve(
    store: Store,
    question: str,
    *,
    scope: list[str] | None = None,
    k: int = 40,
    token_budget: int = 8192,
    today: date | None = None,
) -> Retrieval:
    scope = [s[4:] if s.startswith("src:") else s for s in (scope or [])]
    sources = [s for s in store.list_sources() if not scope or any(match_uri(g, s.uri) for g in scope)]
    terms = extract_terms(question)
    fts = _fts_query(terms, _phrases(question))
    blocks_searched = store.db.execute(
        "SELECT COUNT(*) FROM block b JOIN source s ON s.current_snapshot = b.snapshot_id WHERE b.is_unit = 1"
    ).fetchone()[0] if not scope else sum(store.block_count(s.current_snapshot) for s in sources)

    rows: list[tuple[float, object, str]] = []
    if fts:
        sql = ("SELECT b.id, b.snapshot_id, b.start, b.\"end\", b.text_sha256, b.kind, b.path, b.line_start, "
               "b.line_end, s.uri, bm25(block_fts) AS score FROM block_fts "
               "JOIN block b ON b.id = block_fts.rowid JOIN source s ON s.current_snapshot = b.snapshot_id "
               "WHERE block_fts MATCH ? ORDER BY score LIMIT ?")
        for r in store.db.execute(sql, (fts, k * 6 if scope else k * 3)):
            if not scope or any(match_uri(g, r["uri"]) for g in scope):
                rows.append((float(r["score"]), r, r["uri"]))
    # structural: a named symbol is a strong hint regardless of BM25 rank
    for ident in _identifiers(question)[:6]:
        for r in store.db.execute(
            "SELECT b.id, b.snapshot_id, b.start, b.\"end\", b.text_sha256, b.kind, b.path, b.line_start, "
            "b.line_end, s.uri, -100.0 AS score FROM code_symbol c JOIN block b ON b.id = c.block_id "
            "JOIN source s ON s.current_snapshot = b.snapshot_id "
            "WHERE c.name = ? OR c.qualified_name = ? LIMIT 5", (ident.split(".")[-1], ident)):
            if not scope or any(match_uri(g, r["uri"]) for g in scope):
                rows.append((-100.0, r, r["uri"]))
    rows.sort(key=lambda t: t[0])

    chosen: list[Candidate] = []
    skipped: list[Candidate] = []
    used_chars = 0
    taken: dict[str, list[tuple[int, int]]] = {}
    seen_text: set[str] = set()
    for score, r, uri in rows:
        span = Span(r["snapshot_id"], r["start"], r["end"], r["text_sha256"])
        overlaps = any(span.start < e and s < span.end for s, e in taken.get(span.snapshot_id, []))
        shown = _display_text(store, r, terms)
        key = " ".join(shown.split())
        cand = Candidate(len(chosen) + 1, span, uri, r["kind"], r["line_start"], r["line_end"], r["path"], shown, score)
        if overlaps or key in seen_text:
            continue
        cost = len(shown) + len(uri) + 24
        if len(chosen) >= k or (used_chars + cost) // 4 > token_budget:
            skipped.append(cand)
            continue
        used_chars += cost
        taken.setdefault(span.snapshot_id, []).append((span.start, span.end))
        seen_text.add(key)
        chosen.append(cand)

    # structural hints: a catalog of what exists (aggregates via FIND need no candidate) and
    # detailed schemas for the sources the candidates came from
    cat = describe_sources(store, limit=MAX_CATALOG) if not scope else [
        d for d in describe_sources(store, limit=500) if any(match_uri(g, d["uri"]) for g in scope)][:MAX_CATALOG]
    cand_uris = []
    for c in chosen:
        if c.uri not in cand_uris:
            cand_uris.append(c.uri)
    structured = [d["uri"] for d in cat if d["kind"] in ("log", "file", "code")]
    detail_uris = (cand_uris + [u for u in structured if u not in cand_uris])[:MAX_SCHEMA_SOURCES]
    by_uri = {d["uri"]: d for d in cat}
    schemas = [by_uri[u] for u in detail_uris if u in by_uri and by_uri[u]["kind"] in ("log", "file", "code")]

    return Retrieval(
        candidates=chosen, handles=HandleTable({c.handle: c.span for c in chosen}), terms=terms, fts_query=fts,
        sources_searched=len(sources), blocks_searched=blocks_searched, schemas=schemas, catalog=cat,
        date_hints=date_hints(question, today or date.today()), near_misses=skipped[:5],
    )
