"""Function signatures: the single source of truth for the type checker, the V4
literal policy and the GBNF generator. Implementations live in ``engine``.

Argument *roles* drive the V4 "data comes only from references" rule:

``data``     value-bearing position; numeric literals must come from the question, strings are forbidden
``compare``  a comparison operand; numbers must come from the question, strings are allowed
``query``    a query constraint or filter (source globs, regexes, FIND predicates); anything goes
``format``   presentation (``places:``, ``locale:``); anything goes
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Callable

TEXT, NUMBER, DATE, DATETIME, DURATION, BOOL = "Text", "Number", "Date", "DateTime", "Duration", "Bool"
SPAN, SPANSET, FIELD, ANY = "Span", "SpanSet", "Field", "Any"
ORDERED = frozenset({NUMBER, DATE, DATETIME, DURATION})
ALL_VALUES = frozenset({TEXT, NUMBER, DATE, DATETIME, DURATION, BOOL, SPAN, SPANSET})

Returns = str | Callable[[list[str], dict[str, str | None]], str]


@dataclass(frozen=True)
class Param:
    types: frozenset[str]
    role: str = "data"


@dataclass(frozen=True)
class FuncSpec:
    name: str
    group: str
    params: tuple[Param, ...] = ()
    variadic: Param | None = None
    min_args: int | None = None
    kwargs: dict[str, Param] = field(default_factory=dict)
    returns: Returns = NUMBER
    query: bool = False  # positional BOOL args are per-row predicates over FIELD(...)
    doc: str = ""

    @property
    def required(self) -> int:
        return len(self.params) if self.min_args is None else self.min_args


def _p(*types: str, role: str = "data") -> Param:
    return Param(frozenset(types), role)


_SPANISH = (SPAN, SPANSET)  # anything that names source text
PLACES = {"places": _p(NUMBER, role="format")}
LOCALE = {"locale": _p(TEXT, role="format")}
AS = {"as": _p(TEXT, role="format")}

AS_TYPES = {"num": NUMBER, "money": NUMBER, "pct": NUMBER, "date": DATE, "datetime": DATETIME, "duration": DURATION}


def _agg_returns(arg_types: list[str], kw: dict[str, str | None]) -> str:
    if arg_types and arg_types[0] == SPANSET:
        return AS_TYPES.get((kw.get("as") or "num").lower(), NUMBER)
    return arg_types[0] if arg_types else NUMBER


def _diff_returns(arg_types: list[str], kw) -> str:
    return DURATION if arg_types and arg_types[0] in (DATE, DATETIME) else (arg_types[0] if arg_types else NUMBER)


def _same_returns(arg_types: list[str], kw) -> str:
    return arg_types[0] if arg_types else NUMBER


def _if_returns(arg_types: list[str], kw) -> str:
    return arg_types[1] if len(arg_types) > 1 else ANY


_QUERY_KW = {k: _p(TEXT, role="query") for k in ("src", "kind", "re", "contains", "day", "since", "until", "level", "logger")}

_SPECS: list[FuncSpec] = [
    # extraction: deterministic parsers over a span
    FuncSpec("NUM", "extraction", (_p(*_SPANISH),), kwargs=LOCALE, returns=NUMBER, doc="a bare number from a span"),
    FuncSpec("MONEY", "extraction", (_p(*_SPANISH),), kwargs=LOCALE, returns=NUMBER, doc="amount with currency"),
    FuncSpec("PCT_OF", "extraction", (_p(*_SPANISH),), kwargs=LOCALE, returns=NUMBER, doc="a percentage from a span"),
    FuncSpec("DATE", "extraction", (_p(*_SPANISH),), kwargs={"fmt": _p(TEXT, role="format"), "year": _p(NUMBER, role="compare")},
             returns=DATE, doc="calendar date"),
    FuncSpec("DATETIME", "extraction", (_p(*_SPANISH),), returns=DATETIME, doc="date and time"),
    FuncSpec("DURATION", "extraction", (_p(*_SPANISH),), returns=DURATION, doc="elapsed time"),
    FuncSpec("TEXT", "extraction", (_p(*_SPANISH),), returns=TEXT, doc="the text of a span"),
    FuncSpec("BOOL", "extraction", (_p(*_SPANISH),), returns=BOOL, doc="yes/no, true/false, enabled/disabled"),
    # aggregation
    FuncSpec("SUM", "aggregation", variadic=_p(NUMBER, DURATION, SPANSET), min_args=1, kwargs=AS, returns=_agg_returns),
    FuncSpec("AVG", "aggregation", variadic=_p(NUMBER, DURATION, SPANSET), min_args=1, kwargs=AS, returns=_agg_returns),
    FuncSpec("MIN", "aggregation", variadic=_p(NUMBER, DATE, DATETIME, DURATION, SPANSET), min_args=1, kwargs=AS, returns=_agg_returns),
    FuncSpec("MAX", "aggregation", variadic=_p(NUMBER, DATE, DATETIME, DURATION, SPANSET), min_args=1, kwargs=AS, returns=_agg_returns),
    FuncSpec("COUNT", "aggregation", (_p(SPANSET),), returns=NUMBER),
    FuncSpec("DISTINCT", "aggregation", (_p(SPANSET),), returns=SPANSET),
    # math
    FuncSpec("ROUND", "math", (_p(NUMBER),), kwargs=PLACES, returns=NUMBER),
    FuncSpec("PCT", "math", (_p(NUMBER), _p(NUMBER)), kwargs=PLACES, returns=NUMBER, doc="a / b * 100"),
    FuncSpec("DIFF", "math", (_p(NUMBER, DATE, DATETIME, DURATION), _p(NUMBER, DATE, DATETIME, DURATION)), returns=_diff_returns, doc="a - b"),
    FuncSpec("RATIO", "math", (_p(NUMBER), _p(NUMBER)), kwargs=PLACES, returns=NUMBER, doc="a / b"),
    FuncSpec("ABS", "math", (_p(NUMBER, DURATION),), returns=_same_returns),
    # dates
    FuncSpec("DAYS_BETWEEN", "dates", (_p(DATE, DATETIME), _p(DATE, DATETIME)), returns=NUMBER, doc="whole days apart, always >= 0"),
    FuncSpec("BEFORE", "dates", (_p(DATE, DATETIME), _p(DATE, DATETIME)), returns=BOOL),
    FuncSpec("AFTER", "dates", (_p(DATE, DATETIME), _p(DATE, DATETIME)), returns=BOOL),
    # logic
    FuncSpec("IF", "logic", (_p(BOOL), _p(*ALL_VALUES), _p(*ALL_VALUES)), returns=_if_returns),
    FuncSpec("AND", "logic", variadic=_p(BOOL, role="compare"), min_args=2, returns=BOOL),
    FuncSpec("OR", "logic", variadic=_p(BOOL, role="compare"), min_args=2, returns=BOOL),
    FuncSpec("NOT", "logic", (_p(BOOL, role="compare"),), returns=BOOL),
    FuncSpec("EQ", "logic", (_p(*ALL_VALUES, FIELD, role="compare"), _p(*ALL_VALUES, FIELD, role="compare")), returns=BOOL),
    FuncSpec("NE", "logic", (_p(*ALL_VALUES, FIELD, role="compare"), _p(*ALL_VALUES, FIELD, role="compare")), returns=BOOL),
    FuncSpec("GT", "logic", (_p(*ORDERED, TEXT, FIELD, role="compare"), _p(*ORDERED, TEXT, FIELD, role="compare")), returns=BOOL),
    FuncSpec("GTE", "logic", (_p(*ORDERED, TEXT, FIELD, role="compare"), _p(*ORDERED, TEXT, FIELD, role="compare")), returns=BOOL),
    FuncSpec("LT", "logic", (_p(*ORDERED, TEXT, FIELD, role="compare"), _p(*ORDERED, TEXT, FIELD, role="compare")), returns=BOOL),
    FuncSpec("LTE", "logic", (_p(*ORDERED, TEXT, FIELD, role="compare"), _p(*ORDERED, TEXT, FIELD, role="compare")), returns=BOOL),
    FuncSpec("CONTAINS", "logic", (_p(TEXT, SPAN, SPANSET, FIELD, role="compare"), _p(TEXT, role="compare")), returns=BOOL),
    # query: compiled to SQL over the span store
    FuncSpec("FIND", "query", variadic=_p(BOOL, role="query"), min_args=0, kwargs=_QUERY_KW, returns=SPANSET, query=True,
             doc="blocks matching constraints"),
    FuncSpec("FIELD", "query", (_p(TEXT, role="query"),), returns=FIELD, doc="a record field; only inside FIND/COLUMN predicates"),
    FuncSpec("COLUMN", "query", (_p(TEXT, role="query"),), variadic=_p(BOOL, role="query"), kwargs={"src": _p(TEXT, role="query")},
             returns=SPANSET, query=True, doc="cells of a table column"),
    FuncSpec("SYMBOLS", "query", kwargs={k: _p(TEXT, role="query") for k in ("src", "kind", "name", "calls", "re")},
             returns=SPANSET, query=True, doc="code symbols"),
    FuncSpec("LINES", "query", kwargs={"src": _p(TEXT, role="query"), "from": _p(NUMBER, role="query"), "to": _p(NUMBER, role="query")},
             returns=SPANSET, query=True, doc="a line range of one source"),
]

FUNCTIONS: dict[str, FuncSpec] = {s.name: s for s in _SPECS}
QUERY_FUNCTIONS = frozenset(n for n, s in FUNCTIONS.items() if s.query)
ALL_KWARGS = sorted({k for s in _SPECS for k in s.kwargs} | {"text", "searched"})


def coercible(arg_type: str, accepted: frozenset[str]) -> bool:
    """Can a value of ``arg_type`` be passed where ``accepted`` types are expected?"""
    if arg_type == ANY or arg_type in accepted:
        return True
    if arg_type == SPANSET and SPAN in accepted:  # a one-element set stands for its span
        return True
    if arg_type in (SPAN, SPANSET) and TEXT in accepted:  # spans read as their text
        return True
    return False
