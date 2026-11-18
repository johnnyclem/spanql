from __future__ import annotations

from datetime import date, datetime, timedelta, timezone
from decimal import Decimal

import pytest

from spanql.engine.extract import (extract, extract_bool, extract_date, extract_datetime, extract_duration,
                                   extract_money, extract_num, extract_pct)
from spanql.engine.format import render
from spanql.engine.values import Number
from spanql.errors import ExtractError


def consumed(text, p):
    return text[p.consumed[0] : p.consumed[1]]


# ------------------------------------------------------------------ NUM


@pytest.mark.parametrize(
    "text, expected, cons",
    [
        ("41", "41", "41"),
        ("Headcount was 120 at the end", "120", "120"),
        ("1,234,567", "1234567", "1,234,567"),
        ("-3.5", "-3.5", "-3.5"),
        ("−3", "-3", "−3"),
        ("(1,200)", "-1200", "1,200"),
        ("4.82 million", "4820000", "4.82 million"),
        ("4.2k", "4200", "4.2k"),
        ("3.5B", "3500000000", "3.5B"),
        ("took 12ms", "12", "12"),
        ("The 5. Item", "5", "5"),
        ("Q3 revenue was 7", "7", "7"),
    ],
)
def test_num_ok(text, expected, cons):
    p = extract_num(text)
    assert p.value == Number(Decimal(expected)) and consumed(text, p) == cons


@pytest.mark.parametrize(
    "text, fragment",
    [
        ("no digits here", "no number"),
        ("up 4.82 from 4.31", "found 2 numbers"),
        ("2026-11-10", "found 3 numbers"),
        ("$4.82 million", "use MONEY"),
        ("USD 1,200", "use MONEY"),
        ("13.1%", "use PCT_OF"),
        ("12 percent", "use PCT_OF"),
        ("1,23", "no number"),  # not a valid grouping: refuse rather than read it as 1
        ("v2.1", "no number"),
    ],
)
def test_num_refuses_to_guess(text, fragment):
    with pytest.raises(ExtractError) as e:
        extract_num(text)
    assert fragment in e.value.message


def test_num_locales():
    assert extract_num("1.234,56", "de").value.value == Decimal("1234.56")
    assert extract_num("1 234,5", "fr").value.value == Decimal("1234.5")
    assert extract_num("1,234.56").value.value == Decimal("1234.56")
    with pytest.raises(ExtractError):
        extract_num("1", "xx")


# ---------------------------------------------------------------- MONEY


@pytest.mark.parametrize(
    "text, amount, cur, cons",
    [
        ("$4.82 million", "4820000", "USD", "$4.82 million"),
        ("$4.31M", "4310000", "USD", "$4.31M"),
        ("Q2 revenue: $4.31M", "4310000", "USD", "$4.31M"),
        ("$4.31 M", "4310000", "USD", "$4.31 M"),
        ("$1,200.50", "1200.50", "USD", "$1,200.50"),
        ("EUR 1,200", "1200", "EUR", "EUR 1,200"),
        ("€1.5bn", "1500000000", "EUR", "€1.5bn"),
        ("£300", "300", "GBP", "£300"),
        ("4.2 million dollars", "4200000", "USD", "4.2 million dollars"),
        ("1,200 CHF", "1200", "CHF", "1,200 CHF"),
        ("-$5", "-5", "USD", "-$5"),
        ("($1,234)", "-1234", "USD", "$1,234"),
        ("C$20", "20", "CAD", "C$20"),
        ("$4.3m", "4300000", "USD", "$4.3m"),
    ],
)
def test_money_ok(text, amount, cur, cons):
    p = extract_money(text)
    assert p.value == Number(Decimal(amount), cur) and consumed(text, p) == cons


@pytest.mark.parametrize(
    "text, fragment",
    [
        ("4.82 million", "no currency"),
        ("revenue was 100", "no currency"),
        ("$4.82 million and $4.31 million", "found 2 amounts"),
        ("ABC 12", "no currency"),
    ],
)
def test_money_refuses(text, fragment):
    with pytest.raises(ExtractError) as e:
        extract_money(text)
    assert fragment in e.value.message


def test_pct():
    p = extract_pct("up 13.1% on the year")
    assert p.value == Number(Decimal("13.1"), "%") and consumed("up 13.1% on the year", p) == "13.1%"
    assert extract_pct("12 percent").value.value == 12
    with pytest.raises(ExtractError):
        extract_pct("12")
    with pytest.raises(ExtractError):
        extract_pct("5% and 6%")
    assert extract_pct("12.5% of 4,000").value.value == Decimal("12.5")  # only one percent token


# ---------------------------------------------------------------- dates


@pytest.mark.parametrize(
    "text, expected",
    [
        ("2026-11-10", date(2026, 11, 10)),
        ("2026/11/10", date(2026, 11, 10)),
        ("Nov 10, 2026", date(2026, 11, 10)),
        ("November 10, 2026", date(2026, 11, 10)),
        ("10 Nov 2026", date(2026, 11, 10)),
        ("10th of November, 2026", date(2026, 11, 10)),
        ("10-Nov-2026", date(2026, 11, 10)),
        ("Sept 3, 2026", date(2026, 9, 3)),
        ("paid on March 15, 2026.", date(2026, 3, 15)),
        ("13/02/2026", date(2026, 2, 13)),  # day > 12 disambiguates
        ("02/13/2026", date(2026, 2, 13)),
        ("03/03/2026", date(2026, 3, 3)),
        ("2026-11-10T10:15:03Z", date(2026, 11, 10)),
        ("issued 2026-03-01 and ...", date(2026, 3, 1)),
    ],
)
def test_date_ok(text, expected):
    assert extract_date(text).value.value == expected


def test_date_consumed_range():
    t = "The invoice was paid on March 15, 2026 by wire."
    assert consumed(t, extract_date(t)) == "March 15, 2026"


@pytest.mark.parametrize(
    "text, fragment",
    [
        ("03/04/2026", "ambiguous"),
        ("Nov 10", "no year"),
        ("10 November", "no year"),
        ("November 2026", "no day"),
        ("2026-02-30", "not a real calendar date"),
        ("issued 2026-03-01 and paid 2026-03-15", "2 different dates"),
        ("no date", "no date found"),
    ],
)
def test_date_refuses(text, fragment):
    with pytest.raises(ExtractError) as e:
        extract_date(text)
    assert fragment in e.value.message


def test_date_fmt_and_year_hints():
    assert extract_date("03/04/2026", fmt="DMY").value.value == date(2026, 4, 3)
    assert extract_date("03/04/2026", fmt="MDY").value.value == date(2026, 3, 4)
    assert extract_date("Nov 10", year=2026).value.value == date(2026, 11, 10)
    with pytest.raises(ExtractError):
        extract_date("03/04/2026", fmt="XYZ")


def test_datetime():
    p = extract_datetime("at 2026-11-10 10:15:03,250 UTC? no: 2026-11-10 10:15:03,250")
    assert p.value.value == datetime(2026, 11, 10, 10, 15, 3, 250000)
    p = extract_datetime("2026-11-10T10:15:03-06:00")
    assert p.value.value == datetime(2026, 11, 10, 10, 15, 3, tzinfo=timezone(timedelta(hours=-6)))
    assert extract_datetime("2026-11-10 10:15").value.value == datetime(2026, 11, 10, 10, 15)
    assert extract_datetime("16/Nov/2026:10:15:03 +0000").value.value == datetime(2026, 11, 16, 10, 15, 3, tzinfo=timezone.utc)
    for bad, frag in (("2026-11-10", "no time of day"), ("nothing", "no date and time"),
                      ("2026-11-10 10:15 and 2026-11-11 10:15", "2 timestamps"), ("2026-13-10 10:15:00", "not a valid")):
        with pytest.raises(ExtractError) as e:
            extract_datetime(bad)
        assert frag in e.value.message


@pytest.mark.parametrize(
    "text, seconds",
    [
        ("250 ms", "0.25"),
        ("3h 20m", "12000"),
        ("3 hours and 20 minutes", "12000"),
        ("1.5 hours", "5400"),
        ("2 days", "172800"),
        ("PT1H30M", "5400"),
        ("P1DT2H", "93600"),
        ("00:03:20", "200"),
        ("took 45 seconds to run", "45"),
    ],
)
def test_duration_ok(text, seconds):
    assert extract_duration(text).value.value == timedelta(seconds=float(seconds))


def test_duration_refuses():
    for bad, frag in (("soon", "no duration"), ("5 minutes then 3 hours", "2 separate durations")):
        with pytest.raises(ExtractError) as e:
            extract_duration(bad)
        assert frag in e.value.message


def test_bool_and_dispatch():
    assert extract_bool(" Enabled. ").value.value is True
    assert extract_bool("off").value.value is False
    with pytest.raises(ExtractError):
        extract_bool("maybe")
    assert extract("num", "7").value.value == 7
    with pytest.raises(ExtractError):
        extract("nope", "x")


def test_rendering_of_extracted_values():
    assert render(extract_money("$4.82 million").value) == "$4,820,000"
    assert render(extract_money("$1,200.5").value) == "$1,200.50"
    assert render(extract_pct("13.1%").value) == "13.1%"
    assert render(extract_num("2026").value) == "2026"
    assert render(extract_num("12,345,678").value) == "12,345,678"
    assert render(extract_duration("3h 20m 5s").value) == "3h 20m 5s"
    assert render(extract_duration("250 ms").value) == "250ms"
    assert render(extract_datetime("2026-11-10T10:15:03Z").value) == "2026-11-10 10:15:03Z"
