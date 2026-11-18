"""Typed runtime values. All immutable; arithmetic uses Decimal, never float."""

from __future__ import annotations

from dataclasses import dataclass
from datetime import date, datetime, timedelta
from decimal import ROUND_HALF_UP, Context, Decimal
from typing import ClassVar

from ..spans import Span

# One shared context: 34 significant digits, Excel-style half-up rounding.
CTX = Context(prec=34, rounding=ROUND_HALF_UP)


@dataclass(frozen=True, slots=True)
class Number:
    type: ClassVar[str] = "Number"
    value: Decimal
    unit: str | None = None  # ISO currency code, "%", or "days"
    places: int | None = None  # display precision after ROUND / PCT(places:)


@dataclass(frozen=True, slots=True)
class Text:
    type: ClassVar[str] = "Text"
    value: str


@dataclass(frozen=True, slots=True)
class DateV:
    type: ClassVar[str] = "Date"
    value: date


@dataclass(frozen=True, slots=True)
class DateTimeV:
    type: ClassVar[str] = "DateTime"
    value: datetime  # naive or timezone-aware; the two are never mixed implicitly


@dataclass(frozen=True, slots=True)
class DurationV:
    type: ClassVar[str] = "Duration"
    value: timedelta


@dataclass(frozen=True, slots=True)
class BoolV:
    type: ClassVar[str] = "Bool"
    value: bool


@dataclass(frozen=True, slots=True)
class SpanV:
    type: ClassVar[str] = "Span"
    span: Span


@dataclass(frozen=True, slots=True)
class Item:
    """One member of a SpanSet."""

    span: Span
    source: str  # URI
    kind: str  # block kind
    label: str | None = None  # e.g. a symbol's qualified name
    line: int | None = None
    path: str | None = None


@dataclass(frozen=True, slots=True)
class SpanSetV:
    type: ClassVar[str] = "SpanSet"
    items: tuple[Item, ...]
    total: int  # may exceed len(items) when only a prefix was materialized
    scan: dict | None = None  # query description + snapshot set (also stored in the trace)
    spec: object | None = None  # the ScanSpec, so large sets can be re-expanded

    @property
    def complete(self) -> bool:
        return len(self.items) == self.total


Value = Number | Text | DateV | DateTimeV | DurationV | BoolV | SpanV | SpanSetV
