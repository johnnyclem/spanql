"""Arithmetic, comparison and date operations on typed values, with unit checking.

Nothing here converts units or currencies: mismatches are errors, not guesses.
"""

from __future__ import annotations

from datetime import date, datetime, timedelta
from decimal import Decimal, DivisionByZero, InvalidOperation

from ..errors import EvalError
from .format import quantize, render
from .values import BoolV, CTX, DateTimeV, DateV, DurationV, Number, Text, Value


def _u(unit: str | None) -> str:
    return unit or "no unit"


def _ctx_op(fn, *args) -> Decimal:
    try:
        return fn(*args)
    except (InvalidOperation, DivisionByZero) as e:
        raise EvalError("division by zero" if isinstance(e, DivisionByZero) or "division" in str(e).lower()
                        else f"arithmetic error: {e}") from e


def add_numbers(a: Number, b: Number, sign: int) -> Number:
    if a.unit != b.unit:
        raise EvalError(f"cannot {'add' if sign > 0 else 'subtract'} {render(a)} ({_u(a.unit)}) and {render(b)} "
                        f"({_u(b.unit)}): units differ",
                        hint="extract both values with the same extractor (e.g. MONEY) from comparable text")
    fn = CTX.add if sign > 0 else CTX.subtract
    return Number(fn(a.value, b.value), a.unit)


def mul_numbers(a: Number, b: Number) -> Number:
    if a.unit and b.unit:
        raise EvalError(f"cannot multiply {render(a)} by {render(b)}: both have units",
                        hint="use PCT(...) or RATIO(...) to relate two amounts")
    return Number(CTX.multiply(a.value, b.value), a.unit or b.unit)


def div_numbers(a: Number, b: Number) -> Number:
    if b.value == 0:
        raise EvalError(f"division by zero ({render(a)} / {render(b)})")
    if a.unit and b.unit:
        if a.unit != b.unit:
            raise EvalError(f"cannot divide {render(a)} by {render(b)}: units differ")
        return Number(CTX.divide(a.value, b.value))
    if b.unit:
        raise EvalError(f"cannot divide a plain number by {render(b)}")
    return Number(CTX.divide(a.value, b.value), a.unit)


def _check_tz(a: datetime, b: datetime) -> None:
    if (a.tzinfo is None) != (b.tzinfo is None):
        raise EvalError("cannot combine a timestamp with a time zone and one without",
                        hint="use timestamps written the same way, or DATE(...) to compare calendar days")


def _micros(td: timedelta) -> int:
    return td.days * 86_400_000_000 + td.seconds * 1_000_000 + td.microseconds


def binop(op: str, a: Value, b: Value) -> Value:
    if isinstance(a, Number) and isinstance(b, Number):
        if op in "+-":
            return add_numbers(a, b, 1 if op == "+" else -1)
        return mul_numbers(a, b) if op == "*" else div_numbers(a, b)
    if isinstance(a, DateV) and isinstance(b, DateV) and op == "-":
        return DurationV(a.value - b.value)
    if isinstance(a, DateV) and isinstance(b, DurationV) and op in "+-":
        try:
            return DateV((a.value + b.value if op == "+" else a.value - b.value))
        except OverflowError as e:
            raise EvalError("date out of range") from e
    if isinstance(a, DurationV) and isinstance(b, DateV) and op == "+":
        return DateV(b.value + a.value)
    if isinstance(a, DateTimeV) and isinstance(b, DateTimeV) and op == "-":
        _check_tz(a.value, b.value)
        return DurationV(a.value - b.value)
    if isinstance(a, DateTimeV) and isinstance(b, DurationV) and op in "+-":
        return DateTimeV(a.value + b.value if op == "+" else a.value - b.value)
    if isinstance(a, DurationV) and isinstance(b, DateTimeV) and op == "+":
        return DateTimeV(b.value + a.value)
    if isinstance(a, DurationV) and isinstance(b, DurationV):
        if op in "+-":
            return DurationV(a.value + b.value if op == "+" else a.value - b.value)
        if op == "/":
            if b.value == timedelta(0):
                raise EvalError("division by zero (duration / duration)")
            return Number(CTX.divide(Decimal(_micros(a.value)), Decimal(_micros(b.value))))
    if isinstance(a, DurationV) and isinstance(b, Number) and op in "*/" and not b.unit:
        if op == "/" and b.value == 0:
            raise EvalError("division by zero")
        f = b.value if op == "*" else CTX.divide(Decimal(1), b.value)
        return DurationV(timedelta(microseconds=int(CTX.multiply(Decimal(_micros(a.value)), f).to_integral_value())))
    if isinstance(a, Number) and isinstance(b, DurationV) and op == "*" and not a.unit:
        return binop("*", b, a)
    raise EvalError(f"cannot compute {a.type} {op} {b.type}")


def negate(a: Value) -> Value:
    if isinstance(a, Number):
        return Number(-a.value, a.unit, a.places)
    if isinstance(a, DurationV):
        return DurationV(-a.value)
    raise EvalError(f"cannot negate a {a.type}")


def pct(a: Number, b: Number, places: int | None) -> Number:
    if a.unit != b.unit:
        raise EvalError(f"PCT needs values with the same unit, got {_u(a.unit)} and {_u(b.unit)}")
    if b.value == 0:
        raise EvalError(f"PCT: the base is zero ({render(a)} / {render(b)})")
    v = CTX.multiply(CTX.divide(a.value, b.value), Decimal(100))
    if places is not None:
        v = quantize(v, places)
    return Number(v, "%", places)


def ratio(a: Number, b: Number, places: int | None) -> Number:
    if a.unit != b.unit:
        raise EvalError(f"RATIO needs values with the same unit, got {_u(a.unit)} and {_u(b.unit)}")
    if b.value == 0:
        raise EvalError(f"RATIO: the denominator is zero ({render(a)} / {render(b)})")
    v = CTX.divide(a.value, b.value)
    if places is not None:
        v = quantize(v, places)
    return Number(v, None, places)


def round_(a: Number, places: int | None) -> Number:
    p = 0 if places is None else places
    return Number(quantize(a.value, p), a.unit, p)


def absolute(a: Value) -> Value:
    if isinstance(a, Number):
        return Number(abs(a.value), a.unit, a.places)
    if isinstance(a, DurationV):
        return DurationV(abs(a.value))
    raise EvalError(f"ABS needs a number or duration, got {a.type}")


def _as_dt(v: Value) -> datetime | date:
    if isinstance(v, (DateV, DateTimeV)):
        return v.value
    raise EvalError(f"expected a date or timestamp, got {v.type}")


def _comparable_dates(a: Value, b: Value) -> tuple[datetime | date, datetime | date]:
    x, y = _as_dt(a), _as_dt(b)
    if isinstance(x, datetime) != isinstance(y, datetime):
        # a calendar date against a timestamp: compare calendar days
        x = x.date() if isinstance(x, datetime) else x
        y = y.date() if isinstance(y, datetime) else y
    elif isinstance(x, datetime):
        _check_tz(x, y)  # type: ignore[arg-type]
    return x, y


def days_between(a: Value, b: Value) -> Number:
    x, y = _comparable_dates(a, b)
    if isinstance(x, datetime):
        days = abs(y - x) // timedelta(days=1)  # type: ignore[operator]
    else:
        days = abs((y - x).days)  # type: ignore[operator]
    return Number(Decimal(days), "days")


def before_after(op: str, a: Value, b: Value) -> BoolV:
    x, y = _comparable_dates(a, b)
    return BoolV(x < y if op == "BEFORE" else x > y)  # type: ignore[operator]


def compare(op: str, a: Value, b: Value) -> BoolV:
    """``== != > >= < <=`` on two values of the same type."""
    if type(a) is not type(b):
        if {type(a), type(b)} <= {DateV, DateTimeV}:
            x, y = _comparable_dates(a, b)
        else:
            raise EvalError(f"cannot compare {a.type} with {b.type}")
    elif isinstance(a, Number):
        if a.unit != b.unit:
            raise EvalError(f"cannot compare {render(a)} ({_u(a.unit)}) with {render(b)} ({_u(b.unit)}): units differ")
        x, y = a.value, b.value
    elif isinstance(a, (Text, BoolV, DateV, DateTimeV, DurationV)):
        if isinstance(a, DateTimeV):
            _check_tz(a.value, b.value)
        x, y = a.value, b.value
        if isinstance(a, BoolV) and op not in ("==", "!="):
            raise EvalError("true/false values can only be compared with == or !=")
    else:
        raise EvalError(f"cannot compare {a.type} values")
    return BoolV({"==": x == y, "!=": x != y, ">": x > y, ">=": x >= y, "<": x < y, "<=": x <= y}[op])  # type: ignore[operator]
