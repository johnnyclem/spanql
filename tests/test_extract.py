from __future__ import annotations

from datetime import date
from decimal import Decimal

import pytest

from spanql.engine.errors import EngineError
from spanql.engine.extract import (
    parse_bool,
    parse_date,
    parse_datetime,
    parse_duration,
    parse_quantity,
)
from spanql.engine.values import Value, display, format_number, quantize


@pytest.mark.parametrize(
    "text,value,unit",
    [
        ("$4.82 million", "4820000", "USD"),
        ("$4.31M", "4310000", "USD"),
        ("USD 3.5k", "3500", "USD"),
        ("€1.2bn", "1200000000", "EUR"),
        ("12%", "12", "%"),
        ("-7", "-7", None),
        ("(1,200)", "-1200", None),  # accounting negative for a bare cell
        ("revenue was ($3.1M)", "-3100000", "USD"),
        ("1,284 paying customers", "1284", None),
        ("latency 300ms", "300", "ms"),
    ],
)
def test_quantities(text: str, value: str, unit: str | None) -> None:
    ex = parse_quantity(text)
    assert ex.value == Decimal(value)
    assert ex.unit == unit


def test_parens_not_negative_for_counts() -> None:
    assert parse_quantity("(12) items").value == Decimal(12)


def test_consumed_range_includes_sign_and_symbol() -> None:
    text = "net (USD 3.5k) loss"
    ex = parse_quantity(text)
    assert text[ex.start : ex.end] == "(USD 3.5k)"


@pytest.mark.parametrize("text", ["no numbers here", "$1 and $2"])
def test_quantity_errors(text: str) -> None:
    with pytest.raises(EngineError):
        parse_quantity(text)


@pytest.mark.parametrize(
    "text,want",
    [
        ("November 3, 2026", date(2026, 11, 3)),
        ("2026-11-03", date(2026, 11, 3)),
        ("3 Nov 2026", date(2026, 11, 3)),
        ("2026/11/03", date(2026, 11, 3)),
    ],
)
def test_dates(text: str, want: date) -> None:
    assert parse_date(text).value == want


def test_ambiguous_and_yearless_dates() -> None:
    with pytest.raises(EngineError, match="ambiguous"):
        parse_date("3/4/2026")
    assert parse_date("3/4/2026", "MDY").value == date(2026, 3, 4)
    assert parse_date("3/4/2026", "DMY").value == date(2026, 4, 3)
    with pytest.raises(EngineError, match="no year"):
        parse_date("Nov 3")


def test_datetime_duration_bool() -> None:
    assert parse_datetime("2026-11-10T08:00:05Z").value.hour == 8
    assert parse_duration("1h 30m").value == Decimal(5400)
    assert parse_duration("250ms").value == Decimal("0.25")
    assert parse_bool("yes").value is True
    assert parse_bool("disabled").value is False


def test_formatting() -> None:
    assert format_number(Decimal("510000.00"), "USD") == "$510,000"
    assert format_number(Decimal("11.83"), "%", 1) == "11.8%"
    assert quantize(Decimal("2.345"), 2) == Decimal("2.35")  # HALF_UP
    assert display(Value("Number", Decimal("1284"), None)) == "1,284"


@pytest.mark.parametrize(
    "text", ["€1.234,50", "Revenue: €1.234,50 in the EU store.", "12,5 %", "$10,20"]
)
def test_ambiguous_separators_are_refused(text: str) -> None:
    with pytest.raises(EngineError) as exc:
        parse_quantity(text)
    assert exc.value.problem.code == "V3" and "unambiguously" in exc.value.problem.message


@pytest.mark.parametrize(
    ("text", "value"),
    [
        ("$1,234.50", "1234.50"),
        ("costs $5, and more", "5"),
        ("€1,234.5", "1234.5"),
        ("3.25", "3.25"),
    ],
)
def test_ordinary_separators_still_parse(text: str, value: str) -> None:
    assert parse_quantity(text).value == Decimal(value)


def test_dotted_thousands_are_not_read_as_a_number() -> None:
    with pytest.raises(EngineError) as exc:  # 1.234.567 looks like a version; never 1.234
        parse_quantity("EUR 1.234.567")
    assert exc.value.problem.code == "V3"
