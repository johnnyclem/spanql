"""Typed values with provenance, and display formatting."""

from __future__ import annotations

from dataclasses import dataclass, field
from datetime import date, datetime
from decimal import ROUND_HALF_UP, Decimal
from typing import Any

CURRENCY_PREFIX = {"USD": "$", "EUR": "€", "GBP": "£", "JPY": "¥", "INR": "₹"}


@dataclass
class Value:
    type: str  # Number | Text | Date | DateTime | Bool | Span | SpanSet | List
    v: Any
    unit: str | None = None
    inputs: list[dict[str, Any]] = field(default_factory=list)
    steps: list[dict[str, Any]] = field(default_factory=list)
    places: int | None = None  # display precision requested by the formula
    meta: dict[str, Any] = field(default_factory=dict)

    @property
    def grounded(self) -> bool:
        return bool(self.inputs)

    def text(self) -> str:
        """Text content for Text/Span values."""
        if self.type == "Span":
            return str(self.v["text"])
        if self.type == "Text":
            return str(self.v)
        raise TypeError(self.type)

    def to_json(self) -> dict[str, Any]:
        d: dict[str, Any] = {"type": self.type, "display": display(self)}
        if self.type == "Number":
            d["decimal"] = dec_str(self.v)
            d["unit"] = self.unit
            if self.places is not None:
                d["places"] = self.places
        elif self.type == "Text":
            d["text"] = self.v
        elif self.type == "Span":
            d["text"] = self.v["text"]
            d["span"] = self.v["id"]
        elif self.type in ("Date", "DateTime"):
            d["value"] = self.v.isoformat()
        elif self.type == "Bool":
            d["value"] = bool(self.v)
        elif self.type == "List":
            d["items"] = [x.to_json() for x in self.v[:200]]
            d["count"] = len(self.v)
        elif self.type == "SpanSet":
            d["count"] = len(self.v)
        return d


def merge_inputs(*vals: Value) -> list[dict[str, Any]]:
    seen: dict[str, dict[str, Any]] = {}
    for val in vals:
        for inp in val.inputs:
            key = (
                f"{inp.get('kind')}:{inp.get('consumed_span') or inp.get('id') or inp.get('query')}"
            )
            if key not in seen:
                seen[key] = inp
    return list(seen.values())


def merge_steps(*vals: Value) -> list[dict[str, Any]]:
    out: list[dict[str, Any]] = []
    seen: set[str] = set()
    for val in vals:
        for st in val.steps:
            key = repr(sorted(st.items()))
            if key not in seen:
                seen.add(key)
                out.append(st)
    return out


def dec_str(d: Decimal) -> str:
    """Plain (non-exponent) decimal string with trailing zeros removed."""
    if d == d.to_integral_value():
        return str(d.quantize(Decimal(1)))
    s = format(d.normalize(), "f")
    return s


def quantize(d: Decimal, places: int) -> Decimal:
    return d.quantize(Decimal(1).scaleb(-places), rounding=ROUND_HALF_UP)


def _group(d: Decimal, places: int | None) -> str:
    if places is not None:
        d = quantize(d, places)
        s = f"{abs(d):,.{places}f}"
    elif d == d.to_integral_value():
        s = f"{abs(d.quantize(Decimal(1))):,}"
    else:
        q = quantize(d, 4).normalize()
        frac = max(0, -q.as_tuple().exponent)  # type: ignore[operator]
        s = f"{abs(q):,.{frac}f}"
    return ("-" if d < 0 and s.strip("0.,") else "") + s


def format_number(d: Decimal, unit: str | None, places: int | None = None) -> str:
    if unit == "%":
        return _group(d, 1 if places is None else places) + "%"
    if unit in CURRENCY_PREFIX or (unit and len(unit) == 3 and unit.isupper()):
        if places is None:
            places = 0 if d == d.to_integral_value() else 2
        body = _group(d, places)
        neg = body.startswith("-")
        body = body.lstrip("-")
        sym = CURRENCY_PREFIX.get(unit)
        s = f"{sym}{body}" if sym else f"{body} {unit}"
        return f"-{s}" if neg else s
    s = _group(d, places)
    return f"{s} {unit}" if unit else s


def display(val: Value) -> str:
    t = val.type
    if t == "Number":
        return format_number(val.v, val.unit, val.places)
    if t == "Text":
        return str(val.v)
    if t == "Span":
        return str(val.v["text"])
    if t == "Date":
        d: date = val.v
        return d.isoformat()
    if t == "DateTime":
        dt: datetime = val.v
        return dt.isoformat()
    if t == "Bool":
        return "yes" if val.v else "no"
    if t == "List":
        items = [display(x) for x in val.v]
        if len(items) > 25:
            return ", ".join(items[:25]) + f", … ({len(items)} total)"
        return ", ".join(items)
    if t == "SpanSet":
        return f"{len(val.v)} matches"
    return str(val.v)


def from_json(d: dict[str, Any]) -> Value:
    """Rebuild a value from a stored cell (for [c_id] references)."""
    t = d["type"]
    if t == "Number":
        return Value("Number", Decimal(d["decimal"]), d.get("unit"), places=d.get("places"))
    if t in ("Text", "Span"):
        return Value("Text", d.get("text", ""))
    if t == "Date":
        return Value("Date", date.fromisoformat(d["value"]))
    if t == "DateTime":
        return Value("DateTime", datetime.fromisoformat(d["value"]))
    if t == "Bool":
        return Value("Bool", bool(d["value"]))
    if t == "List":
        return Value("List", [from_json(x) for x in d.get("items", [])])
    raise ValueError(f"cannot reference a cell of type {t}")
