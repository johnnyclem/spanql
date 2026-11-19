"""Hybrid retrieval: FTS5 BM25 over current blocks + structural hints (log fields, table
columns, code symbols), packed into numbered handles $1..$k within a token budget."""

from __future__ import annotations

import json
import re
from dataclasses import dataclass, field
from typing import Any

from spanql.engine.handles import Handle, HandleTable
from spanql.store.db import Span, Store

STOPWORDS = frozenset(
    [
        "a",
        "an",
        "and",
        "are",
        "as",
        "at",
        "be",
        "by",
        "did",
        "do",
        "does",
        "for",
        "from",
        "had",
        "has",
        "have",
        "how",
        "i",
        "in",
        "is",
        "it",
        "its",
        "last",
        "many",
        "much",
        "of",
        "on",
        "or",
        "our",
        "over",
        "that",
        "the",
        "their",
        "there",
        "this",
        "to",
        "was",
        "we",
        "were",
        "what",
        "when",
        "where",
        "which",
        "who",
        "why",
        "will",
        "with",
        "you",
        "your",
        "me",
        "my",
        "show",
        "tell",
        "give",
        "find",
        "list",
        "all",
        "any",
        "between",
        "than",
        "then",
        "per",
        "vs",
        "versus",
        "can",
        "could",
        "would",
        "should",
        "about",
        "into",
        "out",
        "up",
        "down",
        "get",
        "got",
        "been",
        "being",
        "also",
        "each",
        "every",
        "some",
        "such",
        "these",
        "those",
    ]
)
_WORD = re.compile(r"[A-Za-z_][\w.\-]*[\w]|[A-Za-z_]|\d+(?:[.,]\d+)*%?")
_BACKTICK = re.compile(r"`([^`]+)`")
_DATE_IN_Q = re.compile(r"\b\d{4}-\d{2}-\d{2}\b")
EXCERPT_CHARS = 1200


@dataclass
class Retrieval:
    handles: HandleTable
    hints: list[str]
    report: dict[str, Any] = field(default_factory=dict)

    def to_dict(self) -> dict[str, Any]:
        return {
            "candidates": [h.to_dict() for h in self.handles.values()],
            "hints": self.hints,
            "report": self.report,
        }


def keywords(question: str) -> list[str]:
    words: list[str] = []
    for m in _BACKTICK.findall(question):
        words.append(m)
    for w in _WORD.findall(question):
        lw = w.lower()
        if lw in STOPWORDS or (len(w) < 2 and not w.isdigit()):
            continue
        words.append(w)
    seen: set[str] = set()
    out = []
    for w in words:
        k = w.lower()
        if k not in seen:
            seen.add(k)
            out.append(w)
    return out


def _fts_terms(words: list[str]) -> list[str]:
    terms = []
    for w in words:
        parts = re.findall(r"\w+", w)
        if not parts:
            continue
        phrase = " ".join(parts)
        terms.append(f'"{phrase}"' + ("*" if len(parts) == 1 and len(parts[0]) >= 4 else ""))
    return terms


class Retriever:
    def __init__(self, store: Store, max_candidates: int = 12, token_budget: int = 8192):
        self.store = store
        self.max_candidates = max_candidates
        self.token_budget = token_budget

    def search(
        self,
        question: str,
        *,
        scope: list[str] | None = None,
        k: int | None = None,
        kinds: list[str] | None = None,
    ) -> Retrieval:
        k = k or self.max_candidates
        words = keywords(question)
        terms = _fts_terms(words)
        report: dict[str, Any] = {
            "query": question,
            "keywords": words,
            "fts": " OR ".join(terms),
            "scope": scope or [],
            "kinds": kinds or [],
        }
        rows: list[Any] = []
        where = ["src.removed = 0", "b.kind NOT IN ('table_cell')"]
        params: list[Any] = []
        if scope:
            where.append("(" + " OR ".join("spanql_glob(?, src.uri)" for _ in scope) + ")")
            params.extend(s.removeprefix("src:") for s in scope)
        if kinds:
            where.append(f"src.kind IN ({', '.join('?' * len(kinds))})")
            params.extend(kinds)
        if terms:
            sql = (
                "SELECT b.id, b.snapshot_id, b.start, b.end, b.kind, b.path, b.line_start,"
                " b.line_end, b.page, b.text_sha256, src.uri, src.kind AS skind,"
                " bm25(block_fts) AS score"
                " FROM block_fts JOIN block b ON b.id = block_fts.rowid"
                " JOIN source src ON src.current_snapshot = b.snapshot_id"
                f" WHERE block_fts MATCH ? AND {' AND '.join(where)}"
                " ORDER BY score, src.uri, b.start LIMIT ?"
            )
            rows = self.store.query(sql, [" OR ".join(terms), *params, k * 4])
        searched = self.store.one(
            "SELECT COUNT(DISTINCT src.id) AS sources, COUNT(b.id) AS blocks FROM block b"
            f" JOIN source src ON src.current_snapshot = b.snapshot_id WHERE {' AND '.join(where)}",
            params,
        )
        report["sources_searched"] = searched["sources"] if searched else 0
        report["blocks_searched"] = searched["blocks"] if searched else 0

        # rerank: bm25 (lower is better) + small boost for blocks containing quantities when
        # the question asks for one; dedupe nested spans (keep the smaller, more precise block)
        wants_number = bool(
            re.search(
                r"(?i)\b(how (many|much)|total|revenue|count|rate|percent|"
                r"average|sum|number|cost|price|amount)\b",
                question,
            )
        )
        scored = []
        for r in rows:
            text = self.store.snapshot_text(r["snapshot_id"])[r["start"] : r["end"]].decode(
                "utf-8", "replace"
            )
            s = float(r["score"])
            if wants_number and re.search(r"\d", text):
                s -= 0.5
            if r["kind"] == "heading":
                s += 1.0
            scored.append((s, r, text))
        scored.sort(key=lambda x: (x[0], x[1]["uri"], x[1]["start"]))

        handles: HandleTable = {}
        chosen: list[Any] = []
        budget = self.token_budget
        for s, r, text in scored:
            if len(handles) >= k:
                break
            if any(
                c["snapshot_id"] == r["snapshot_id"]
                and c["start"] <= r["start"]
                and r["end"] <= c["end"]
                and (c["end"] - c["start"]) < 4 * (r["end"] - r["start"])
                for c in chosen
            ):
                continue
            cost = min(len(text), EXCERPT_CHARS) // 4 + 20
            if cost > budget and handles:
                break
            budget -= cost
            chosen.append(r)
            n = len(handles) + 1
            handles[n] = Handle(
                n,
                Span(r["snapshot_id"], r["start"], r["end"]),
                text,
                r["text_sha256"],
                r["uri"],
                r["line_start"],
                r["line_end"],
                r["kind"],
                r["path"],
                r["page"],
                round(-s, 3),
            )
        hints = self.hints(question, handles, scope)
        report["candidates"] = len(handles)
        report["near_misses"] = [
            {"source": h.source, "location": h.location(), "text": h.text[:200]}
            for h in list(handles.values())[:3]
        ]
        return Retrieval(handles, hints, report)

    # ------------------------------------------------------------------ hints
    def hints(self, question: str, handles: HandleTable, scope: list[str] | None) -> list[str]:
        """Schema hints so the model can write FIND/COLUMN/SYMBOLS queries over data it can't
        see line by line: log fields and levels, table columns, symbol kinds."""
        out: list[str] = []
        uris = {h.source for h in handles.values()}
        q = question.lower()
        wants_logs = bool(
            re.search(
                r"\b(log|logs|logged|error|errors|warn|requests?|status|"
                r"timeouts?|how many|count|times|5\d\d|4\d\d)\b",
                q,
            )
        )
        wants_code = bool(
            re.search(
                r"\b(function|functions|method|methods|class|classes|call|"
                r"calls|symbol|defined|implement\w*|code|repo)\b",
                q,
            )
        )
        params: list[Any] = []
        scope_sql = ""
        if scope:
            scope_sql = " AND (" + " OR ".join("spanql_glob(?, src.uri)" for _ in scope) + ")"
            params = [s.removeprefix("src:") for s in scope]
        sources = self.store.query(
            "SELECT src.uri, src.kind, src.current_snapshot, s.meta FROM source src JOIN snapshot s"
            f" ON s.id = src.current_snapshot WHERE src.removed = 0{scope_sql} ORDER BY src.uri",
            params,
        )
        for s in sources:
            meta = json.loads(s["meta"] or "{}")
            if s["kind"] == "log" and (wants_logs or s["uri"] in uris):
                info = self.store.one(
                    "SELECT MIN(r.ts) AS lo, MAX(r.ts) AS hi, COUNT(*) AS n FROM log_record r JOIN"
                    " block b ON b.id = r.block_id WHERE b.snapshot_id = ?",
                    (s["current_snapshot"],),
                )
                levels = [
                    r["level"]
                    for r in self.store.query(
                        "SELECT DISTINCT r.level FROM log_record r JOIN block b ON b.id = r.block_id"
                        " WHERE b.snapshot_id = ? AND r.level IS NOT NULL ORDER BY r.level",
                        (s["current_snapshot"],),
                    )
                ]
                keys: set[str] = set()
                for r in self.store.query(
                    "SELECT r.fields FROM log_record r JOIN block b ON b.id = r.block_id"
                    " WHERE b.snapshot_id = ? LIMIT 200",
                    (s["current_snapshot"],),
                ):
                    keys.update(json.loads(r["fields"] or "{}").keys())
                n = info["n"] if info else 0
                span = f", time {info['lo']} .. {info['hi']}" if info and info["lo"] else ""
                out.append(
                    f"log {s['uri']}: {n} records (parser {meta.get('parser', '?')}{span}); "
                    f"levels {levels or '-'}; fields {sorted(keys)[:20] or '-'}"
                )
            elif "columns" in meta and (
                s["uri"] in uris
                or re.search(
                    r"\b(total|sum|average|"
                    r"rows?|column)\b",
                    q,
                )
            ):
                out.append(
                    f"table {s['uri']}: {meta.get('rows', '?')} rows; columns {meta['columns']}"
                )
            elif s["kind"] == "code" and wants_code and meta.get("symbols"):
                out.append(f"code {s['uri']}: {meta['symbols']} symbols ({meta.get('lang')})")
            if len(out) >= 12:
                break
        dates = _DATE_IN_Q.findall(question)
        if dates:
            out.append(f"dates mentioned in the question: {', '.join(dates)}")
        return out
