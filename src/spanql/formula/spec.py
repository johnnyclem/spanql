"""The spanql formula language: function and keyword tables (single source of truth for the
parser, type checker, GBNF generator and prompt)."""

from __future__ import annotations

EXTRACTION = ("NUM", "MONEY", "PCT_OF", "DATE", "DATETIME", "DURATION", "TEXT", "BOOL")
AGGREGATION = ("SUM", "AVG", "MIN", "MAX", "COUNT", "DISTINCT")
MATH = ("ROUND", "PCT", "DIFF", "RATIO", "ABS")
DATES = ("DAYS_BETWEEN", "BEFORE", "AFTER")
LOGIC = ("IF", "AND", "OR", "NOT", "EQ", "GT", "LT", "CONTAINS")
QUERY = ("FIND", "FIELD", "COLUMN", "SYMBOLS", "LINES")

FUNCTIONS: tuple[str, ...] = EXTRACTION + AGGREGATION + MATH + DATES + LOGIC + QUERY
QUERY_FUNCTIONS = frozenset({"FIND", "COLUMN", "SYMBOLS", "LINES"})

# keyword arguments allowed per function (anything else is a type error)
KWARGS: dict[str, frozenset[str]] = {
    "FIND": frozenset({"src", "kind", "re", "match", "day", "since", "until", "level", "logger"}),
    "SYMBOLS": frozenset({"src", "kind", "name", "qualified", "calls", "lang", "re"}),
    "LINES": frozenset({"src", "re", "kind"}),
    "COLUMN": frozenset({"src"}),
    "ROUND": frozenset({"places"}),
    "PCT": frozenset({"places"}),
    "RATIO": frozenset({"places"}),
    "AVG": frozenset({"places"}),
    "DATE": frozenset({"format"}),
    "MONEY": frozenset({"currency"}),
    "DISTINCT": frozenset({"by"}),
}
ALL_KWARGS: tuple[str, ...] = tuple(sorted({k for v in KWARGS.values() for k in v}))
ANSWER_KWARGS = ("text",)

FORMS = ("ANSWER", "NOT_FOUND", "AMBIGUOUS")
CMP_OPS = ("==", "!=", ">=", "<=", ">", "<")
