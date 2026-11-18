"""Display formatting and JSON (de)serialization of values."""

from __future__ import annotations

from datetime import date, datetime, timedelta
from decimal import ROUND_HALF_UP, Decimal

from ..errors import EvalError
from .values import (BoolV, DateTimeV, DateV, DurationV, Item, Number, SpanSetV, SpanV, Text, Value)

CURRENCY_SYMBOLS = {"USD": "$", "EUR": "€", "GBP": "£", "JPY": "¥", "INR": "₹", "KRW": "₩", "CAD": "CA$",
                    "AUD": "A$", "CHF": "CHF ", "CNY": "CN¥"}
MAX_DISPLAY_ITEMS = 20


def quantize(v: Decimal, places: int) -> Decimal:
    if not 0 <= places <= 12:
        raise EvalError(f"places must be between 0 and 12, got {places}")
    return v.quantize(Decimal(1).scaleb(-places), rounding=ROUND_HALF_UP)


def _group(int_part: str) -> str:
    out = ""
    while len(int_part) > 3:
        out = "," + int_part[-3:] + out
        int_part = int_part[:-3]
    return int_part + out


def format_decimal(v: Decimal, places: int | None, *, group: bool) -> str:
    if places is not None:
        v = quantize(v, places)
        s = format(v, "f")
    elif v == v.to_integral_value():
        s = format(v.quantize(Decimal(1)), "f")
    else:
        s = format(quantize(v, 6).normalize(), "f")
    neg = s.startswith("-")
    s = s.lstrip("-")
    ip, _, fp = s.partition(".")
    if group:
        ip = _group(ip)
    s = ip + ("." + fp if fp else "")
    return "-" + s if neg and any(c not in "0.," for c in s) else s


def format_number(n: Number) -> str:
    v, unit = n.value, n.unit
    if unit == "%":
        return format_decimal(v, n.places, group=False) + "%"
    if unit and unit in CURRENCY_SYMBOLS or (unit and len(unit) == 3 and unit.isupper()):
        sym = CURRENCY_SYMBOLS.get(unit, unit + " ")
        places = n.places
        if places is None and v != v.to_integral_value():
            places = max(2, -v.normalize().as_tuple().exponent)
            places = min(places, 6)
        body = format_decimal(abs(v), places, group=True)
        return ("-" if v < 0 else "") + sym + body
    body = format_decimal(v, n.places, group=abs(v) >= 10000)
    return f"{body} {unit}" if unit else body


def format_duration(td: timedelta) -> str:
    total = td.total_seconds()
    neg = total < 0
    micros = abs(td.days * 86_400_000_000 + td.seconds * 1_000_000 + td.microseconds)
    if micros == 0:
        return "0s"
    if micros < 1_000_000:
        ms = Decimal(micros) / 1000
        return ("-" if neg else "") + format_decimal(ms, None, group=False) + "ms"
    secs, frac = divmod(micros, 1_000_000)
    days, rem = divmod(secs, 86_400)
    hours, rem = divmod(rem, 3600)
    mins, s = divmod(rem, 60)
    parts = []
    if days:
        parts.append(f"{days}d")
    if hours:
        parts.append(f"{hours}h")
    if mins:
        parts.append(f"{mins}m")
    if s or frac:
        parts.append(f"{s}" + (f".{frac:06d}".rstrip("0") if frac else "") + "s")
    return ("-" if neg else "") + " ".join(parts)


def format_datetime(dt: datetime) -> str:
    s = dt.strftime("%Y-%m-%d %H:%M:%S") + (f".{dt.microsecond:06d}".rstrip("0") if dt.microsecond else "")
    if dt.tzinfo is not None:
        off = dt.utcoffset() or timedelta(0)
        if off == timedelta(0):
            return s + "Z"
        mins = int(off.total_seconds() // 60)
        sign = "+" if mins >= 0 else "-"
        return f"{s}{sign}{abs(mins) // 60:02d}:{abs(mins) % 60:02d}"
    return s


def render(value: Value, store=None) -> str:
    """Human display of a value (what appears in the answer prose)."""
    if isinstance(value, Number):
        return format_number(value)
    if isinstance(value, Text):
        return value.value
    if isinstance(value, DateV):
        return value.value.isoformat()
    if isinstance(value, DateTimeV):
        return format_datetime(value.value)
    if isinstance(value, DurationV):
        return format_duration(value.value)
    if isinstance(value, BoolV):
        return "yes" if value.value else "no"
    if isinstance(value, SpanV):
        return _clip(store.span_text(value.span, verify=False) if store else value.span.id, 300)
    if isinstance(value, SpanSetV):
        labels = [_item_text(i, store) for i in value.items[:MAX_DISPLAY_ITEMS]]
        s = ", ".join(labels)
        if value.total > len(labels):
            s += f" … and {value.total - len(labels)} more"
        return s or "(none)"
    raise TypeError(f"cannot render {value!r}")


def _item_text(item: Item, store) -> str:
    if item.label:
        return item.label
    return _clip(store.span_text(item.span, verify=False), 120) if store else item.span.id


def _clip(s: str, n: int) -> str:
    s = " ".join(s.split())
    return s if len(s) <= n else s[: n - 1] + "…"


def plain(v: Decimal) -> str:
    """Decimal as plain digits, never scientific notation (``4820000``, not ``4.82E+6``)."""
    return format(v.normalize(), "f") if v != 0 else "0"


_dec = plain


def to_json(value: Value, store=None, *, max_items: int = 20) -> dict:
    if isinstance(value, Number):
        d = {"type": "Number", "decimal": _dec(value.value)}
        if value.unit:
            d["unit"] = value.unit
        if value.places is not None:
            d["places"] = value.places
        return d
    if isinstance(value, Text):
        return {"type": "Text", "text": value.value}
    if isinstance(value, DateV):
        return {"type": "Date", "iso": value.value.isoformat()}
    if isinstance(value, DateTimeV):
        return {"type": "DateTime", "iso": value.value.isoformat()}
    if isinstance(value, DurationV):
        td = value.value
        micros = td.days * 86_400_000_000 + td.seconds * 1_000_000 + td.microseconds
        return {"type": "Duration", "seconds": _dec(Decimal(micros) / Decimal(1_000_000))}
    if isinstance(value, BoolV):
        return {"type": "Bool", "value": value.value}
    if isinstance(value, SpanV):
        return {"type": "Span", "span": value.span.id,
                "text": _clip(store.span_text(value.span, verify=False), 500) if store else None}
    if isinstance(value, SpanSetV):
        return {
            "type": "SpanSet", "count": value.total, "truncated": value.total > min(len(value.items), max_items),
            "items": [{"span": i.span.id, "source": i.source, "label": i.label, "line": i.line}
                      for i in value.items[:max_items]],
        }
    raise TypeError(f"cannot serialize {value!r}")


def from_json(d: dict) -> Value:
    """Rebuild a scalar value from a stored cell (used by ``[c_id]`` references)."""
    t = d.get("type")
    if t == "Number":
        return Number(Decimal(d["decimal"]), d.get("unit"), d.get("places"))
    if t == "Text":
        return Text(d["text"])
    if t == "Date":
        return DateV(date.fromisoformat(d["iso"]))
    if t == "DateTime":
        return DateTimeV(datetime.fromisoformat(d["iso"]))
    if t == "Duration":
        micros = int((Decimal(d["seconds"]) * 1_000_000).to_integral_value())
        return DurationV(timedelta(microseconds=micros))
    if t == "Bool":
        return BoolV(bool(d["value"]))
    raise EvalError(f"a stored {t} cell cannot be used as a value; reference scalar cells only")

