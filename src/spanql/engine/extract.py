"""Deterministic extraction parsers: NUM, MONEY, PCT_OF, DATE, DATETIME, DURATION, BOOL.

Each parser finds *exactly one* match in its input or fails (never guesses), and reports the
character range it consumed so the trace can highlight it.
"""

from __future__ import annotations

import re
from dataclasses import dataclass
from datetime import date, datetime
from decimal import Decimal, InvalidOperation
from typing import Any

from spanql.engine.errors import extraction

SCALES = {
    "thousand": Decimal(1_000),
    "k": Decimal(1_000),
    "K": Decimal(1_000),
    "million": Decimal(1_000_000),
    "mn": Decimal(1_000_000),
    "M": Decimal(1_000_000),
    "MM": Decimal(1_000_000),
    "mm": Decimal(1_000_000),
    "billion": Decimal(1_000_000_000),
    "bn": Decimal(1_000_000_000),
    "B": Decimal(1_000_000_000),
    "trillion": Decimal(10) ** 12,
    "tn": Decimal(10) ** 12,
    "T": Decimal(10) ** 12,
}
CURRENCY_SYMBOLS = {
    "$": "USD",
    "US$": "USD",
    "€": "EUR",
    "£": "GBP",
    "¥": "JPY",
    "₹": "INR",
    "C$": "CAD",
    "A$": "AUD",
}
CURRENCY_CODES = {"USD", "EUR", "GBP", "JPY", "CAD", "AUD", "CHF", "CNY", "INR", "MXN", "BRL"}
UNITS = {
    "%": "%",
    "percent": "%",
    "pct": "%",
    "ms": "ms",
    "s": "s",
    "sec": "s",
    "secs": "s",
    "seconds": "s",
    "second": "s",
    "min": "min",
    "mins": "min",
    "minutes": "min",
    "minute": "min",
    "h": "h",
    "hr": "h",
    "hrs": "h",
    "hours": "h",
    "hour": "h",
    "days": "days",
    "day": "days",
    "d": "days",
    "KB": "KB",
    "MB": "MB",
    "GB": "GB",
    "TB": "TB",
    "kb": "KB",
    "mb": "MB",
    "gb": "GB",
    "x": "x",
}

_SYM = r"(?:US\$|C\$|A\$|\$|€|£|¥|₹)"
_CODE = r"(?:" + "|".join(sorted(CURRENCY_CODES)) + r")"
_NUM = r"(?:\d{1,3}(?:,\d{3})+|\d+)(?:\.\d+)?|\.\d+"
_SCALE = r"(?:thousand|million|billion|trillion|mn|bn|tn|MM|mm|[kKMBT])"
_UNIT = r"(?:%|percent|pct|ms|seconds?|secs?|s|minutes?|mins?|min|hours?|hrs?|hr|h|days?|d|[KMGT]B|[kmg]b|x)"

QUANTITY_RE = re.compile(
    rf"(?<![\w.$€£¥₹])(?P<neg>-|−|\()?\s?(?P<sym>{_SYM})?\s?(?P<code1>{_CODE}\s)?"
    rf"(?P<neg2>-|−)?(?P<num>{_NUM})"
    rf"(?:\s?(?P<scale>{_SCALE})(?![A-Za-z]))?"
    rf"(?:\s?(?P<unit>{_UNIT})(?![A-Za-z]))?"
    rf"(?:\s(?P<code2>{_CODE}))?(?P<close>\))?"
    rf"(?![\w])"
)


@dataclass
class Extracted:
    value: Any
    unit: str | None
    start: int  # char offsets within the input text
    end: int
    parsed: dict[str, Any]


# the full run of digits and separators a number token sits in ("1.234,50")
_RUN_RE = re.compile(r"[\d.,]*\d")


def _quantities(text: str) -> list[re.Match[str]]:
    out = []
    covered = -1  # end of the last unreadable separator run; its pieces aren't separate numbers
    for m in QUANTITY_RE.finditer(text):
        if m.start("num") < covered:
            continue
        # skip things that look like parts of dates/times/versions/identifiers
        a, b = m.start("num"), m.end("num")
        before = text[max(0, a - 1) : a]
        after = text[b : b + 1]
        if before in ("-", "/", ":") and not m.group("neg") and not m.group("neg2"):
            continue
        if after in ("/", ":") or (after == "-" and text[b + 1 : b + 2].isdigit()):
            continue
        if after == "." and text[b + 1 : b + 2].isdigit():
            continue  # version like 1.2.3
        run = _RUN_RE.match(text, a)
        if run is not None and run.end() > b and _separator_run(run.group(0)):
            covered = run.end()
        out.append(m)
    return out


def _separator_run(run: str) -> bool:
    """True for digit runs whose separators spanql won't guess at: European ``1.234,50``,
    ``1.234.567``, decimal-comma ``12,5`` next to a decimal point, and similar."""
    return ("," in run and "." in run) or run.count(".") > 1 or bool(re.search(r",\d{1,2}$", run))


def _check_separators(m: re.Match[str], text: str) -> None:
    a = m.start("num")
    run = _RUN_RE.match(text, a)
    if run is not None and run.end() > m.end("num") and _separator_run(run.group(0)):
        raise extraction(
            f"can't read {run.group(0)!r} unambiguously: its separators could mean thousands "
            "or decimals (e.g. 1.234,50 = 1234.50 in many locales)",
            hint="spanql reads numbers written like 1,234.50; quote a value written that way, "
            "or answer NOT_FOUND / AMBIGUOUS",
        )


def _one(matches: list[Any], what: str, text: str) -> Any:
    if not matches:
        raise extraction(
            f"no {what} found in {text[:80]!r}", hint=f"quote the exact text containing the {what}"
        )
    if len(matches) > 1:
        shown = ", ".join(repr(m.group(0).strip()) for m in matches[:4])
        raise extraction(
            f"{len(matches)} {what}s found in {text[:80]!r} ({shown})",
            hint=f'quote only the {what} you mean, e.g. $n."…"',
        )
    return matches[0]


def _to_decimal(num: str) -> Decimal:
    try:
        return Decimal(num.replace(",", ""))
    except InvalidOperation as exc:  # pragma: no cover - regex guarantees digits
        raise extraction(f"not a number: {num!r}") from exc


def parse_quantity(text: str, *, require: str | None = None) -> Extracted:
    """Parse one number with optional currency/scale/unit. ``require`` is 'money' or '%'."""
    ms = _quantities(text)
    if require == "money":
        ms = [m for m in ms if m.group("sym") or m.group("code1") or m.group("code2")]
        m = _one(ms, "money amount", text)
    elif require == "%":
        ms = [m for m in ms if m.group("unit") and UNITS.get(m.group("unit")) == "%"]
        m = _one(ms, "percentage", text)
    else:
        m = _one(ms, "number", text)
    _check_separators(m, text)
    value = _to_decimal(m.group("num"))
    scale = m.group("scale")
    # a lone uppercase letter like "M" after a number with no currency is still a scale;
    # "5 m" (lowercase) is ambiguous (metres/minutes) and is not treated as a scale.
    if scale:
        value *= SCALES[scale]
    has_currency = bool(m.group("sym") or m.group("code1") or m.group("code2"))
    # accounting negatives "(1,234)" only for money or a bare table cell; "(12) items" is 12
    paren_neg = (
        m.group("neg") == "("
        and m.group("close") == ")"
        and (has_currency or text.strip() == m.group(0).strip())
    )
    neg = bool(m.group("neg2")) or m.group("neg") in ("-", "−") or paren_neg
    if neg:
        value = -value
    currency = None
    if m.group("sym"):
        currency = CURRENCY_SYMBOLS[m.group("sym")]
    elif m.group("code1") or m.group("code2"):
        currency = (m.group("code1") or m.group("code2")).strip()
    unit_tok = m.group("unit")
    unit = UNITS.get(unit_tok) if unit_tok else None
    if currency and unit and unit != "%":
        unit = None
    start = m.start()
    if m.group("neg") == "(" and not paren_neg:
        start = m.end("neg")
    while start < m.end() and text[start].isspace():
        start += 1
    end = m.end()
    if m.group("close") and not paren_neg:
        end -= 1
    return Extracted(
        value,
        currency or unit,
        start,
        end,
        {
            "amount": str(value),
            "currency": currency,
            "unit": unit,
            "scale": scale,
            "matched": text[start:end],
        },
    )


MONTHS = {
    m: i
    for i, m in enumerate(
        ["jan", "feb", "mar", "apr", "may", "jun", "jul", "aug", "sep", "oct", "nov", "dec"],
        start=1,
    )
}
MONTHS.update(
    {
        "sept": 9,
        "january": 1,
        "february": 2,
        "march": 3,
        "april": 4,
        "june": 6,
        "july": 7,
        "august": 8,
        "september": 9,
        "october": 10,
        "november": 11,
        "december": 12,
    }
)
_MON = r"(?P<mon>" + "|".join(sorted(MONTHS, key=len, reverse=True)) + r")\.?"
DATE_PATTERNS = [
    ("iso", re.compile(r"(?<!\d)(?P<y>\d{4})-(?P<m>\d{1,2})-(?P<d>\d{1,2})(?!\d)")),
    ("ymd/", re.compile(r"(?<!\d)(?P<y>\d{4})/(?P<m>\d{1,2})/(?P<d>\d{1,2})(?!\d)")),
    (
        "mdy-name",
        re.compile(rf"(?i)\b{_MON}\s+(?P<d>\d{{1,2}})(?:st|nd|rd|th)?,?\s+(?P<y>\d{{4}})\b"),
    ),
    (
        "dmy-name",
        re.compile(rf"(?i)\b(?P<d>\d{{1,2}})(?:st|nd|rd|th)?\s+{_MON},?\s+(?P<y>\d{{4}})\b"),
    ),
    ("slash", re.compile(r"(?<![\d/])(?P<a>\d{1,2})[/.](?P<b>\d{1,2})[/.](?P<y>\d{4})(?![\d/])")),
    ("noyear", re.compile(rf"(?i)\b{_MON}\s+(?P<d>\d{{1,2}})(?:st|nd|rd|th)?\b(?!,?\s+\d{{4}})")),
]


def parse_date(text: str, fmt: str | None = None) -> Extracted:
    found: list[tuple[int, int, date, str]] = []
    taken: list[tuple[int, int]] = []

    def overlaps(a: int, b: int) -> bool:
        return any(a < y and x < b for x, y in taken)

    for name, pat in DATE_PATTERNS:
        for m in pat.finditer(text):
            if overlaps(m.start(), m.end()):
                continue
            if name == "noyear":
                raise extraction(
                    f"date {m.group(0)!r} has no year",
                    hint="quote text that includes the year; spanql never guesses years",
                )
            try:
                if name in ("iso", "ymd/"):
                    d = date(int(m["y"]), int(m["m"]), int(m["d"]))
                elif name in ("mdy-name", "dmy-name"):
                    d = date(int(m["y"]), MONTHS[m["mon"].lower().rstrip(".")], int(m["d"]))
                else:
                    a, b = int(m["a"]), int(m["b"])
                    order = (fmt or "").upper()
                    if order == "MDY" or (not order and b > 12 >= a):
                        d = date(int(m["y"]), a, b)
                    elif order == "DMY" or (not order and a > 12 >= b):
                        d = date(int(m["y"]), b, a)
                    else:
                        raise extraction(
                            f"date {m.group(0)!r} is ambiguous (month/day order)",
                            hint='add format:"MDY" or format:"DMY" only if the source states it',
                        )
            except ValueError as exc:
                raise extraction(f"invalid date {m.group(0)!r}: {exc}") from exc
            found.append((m.start(), m.end(), d, name))
            taken.append((m.start(), m.end()))
    found.sort()
    if not found:
        raise extraction(f"no date found in {text[:80]!r}", hint="quote the exact date text")
    if len({f[2] for f in found}) > 1:
        raise extraction(
            f"{len(found)} dates found in {text[:80]!r}", hint="quote only the date you mean"
        )
    s, e, d, name = found[0]
    return Extracted(d, None, s, e, {"date": d.isoformat(), "pattern": name})


DATETIME_RE = re.compile(
    r"(?<!\d)(?P<date>\d{4}-\d{2}-\d{2})[T ](?P<time>\d{2}:\d{2}(?::\d{2}(?:[.,]\d{1,6})?)?)"
    r"(?P<tz>Z|[+-]\d{2}:?\d{2})?(?!\d)"
)


def parse_datetime(text: str) -> Extracted:
    ms = list(DATETIME_RE.finditer(text))
    m = _one(ms, "date-time", text)
    raw = m.group(0).replace(" ", "T", 1).replace(",", ".")
    tz = m.group("tz")
    if tz == "Z":
        raw = raw[:-1] + "+00:00"
    elif tz and ":" not in tz:
        raw = raw[: -len(tz)] + tz[:3] + ":" + tz[3:]
    try:
        dt = datetime.fromisoformat(raw)
    except ValueError as exc:
        raise extraction(f"invalid date-time {m.group(0)!r}") from exc
    return Extracted(dt, None, m.start(), m.end(), {"datetime": dt.isoformat()})


_DUR_UNITS = {
    "ms": Decimal("0.001"),
    "millisecond": Decimal("0.001"),
    "milliseconds": Decimal("0.001"),
    "s": Decimal(1),
    "sec": Decimal(1),
    "secs": Decimal(1),
    "second": Decimal(1),
    "seconds": Decimal(1),
    "m": Decimal(60),
    "min": Decimal(60),
    "mins": Decimal(60),
    "minute": Decimal(60),
    "minutes": Decimal(60),
    "h": Decimal(3600),
    "hr": Decimal(3600),
    "hrs": Decimal(3600),
    "hour": Decimal(3600),
    "hours": Decimal(3600),
    "d": Decimal(86400),
    "day": Decimal(86400),
    "days": Decimal(86400),
    "w": Decimal(604800),
    "week": Decimal(604800),
    "weeks": Decimal(604800),
}
_DUR_PART = r"(\d+(?:\.\d+)?)\s?(milliseconds?|ms|seconds?|secs?|s|minutes?|mins?|min|m|hours?|hrs?|hr|h|days?|d|weeks?|w)"
DURATION_RE = re.compile(rf"(?<![\w.])(?:{_DUR_PART})(?:\s?{_DUR_PART})*(?![A-Za-z])")
_ISO_DUR_RE = re.compile(r"\bP(?:(\d+)D)?(?:T(?:(\d+)H)?(?:(\d+)M)?(?:(\d+(?:\.\d+)?)S)?)?\b")


def parse_duration(text: str) -> Extracted:
    ms = [m for m in DURATION_RE.finditer(text)]
    iso = [m for m in _ISO_DUR_RE.finditer(text) if m.group(0) != "P"]
    if iso and not ms:
        m = _one(iso, "duration", text)
        d, h, mi, s = (Decimal(x) if x else Decimal(0) for x in m.groups())
        secs = d * 86400 + h * 3600 + mi * 60 + s
        return Extracted(secs, "s", m.start(), m.end(), {"seconds": str(secs)})
    m = _one(ms, "duration", text)
    total = Decimal(0)
    for num, unit in re.findall(_DUR_PART, m.group(0)):
        total += Decimal(num) * _DUR_UNITS[unit]
    return Extracted(total, "s", m.start(), m.end(), {"seconds": str(total)})


_TRUE = {"true", "yes", "on", "enabled", "enable", "y"}
_FALSE = {"false", "no", "off", "disabled", "disable", "n"}
BOOL_RE = re.compile(r"(?i)\b(true|false|yes|no|on|off|enabled|disabled|enable|disable)\b")


def parse_bool(text: str) -> Extracted:
    m = _one(list(BOOL_RE.finditer(text)), "boolean", text)
    w = m.group(1).lower()
    return Extracted(w in _TRUE, None, m.start(), m.end(), {"token": m.group(1)})
