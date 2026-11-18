"""Deterministic extractors: NUM, MONEY, PCT_OF, DATE, DATETIME, DURATION, TEXT, BOOL.

Each parses text from a span with fixed, locale-aware rules and records *which
characters it consumed* so the trace can highlight exactly them. Failure is an
:class:`ExtractError` (verifier check V3); nothing is ever guessed: no assumed
year, no assumed currency, no choosing between several numbers.
"""

from __future__ import annotations

import re
from dataclasses import dataclass, field
from datetime import date, datetime, timedelta
from decimal import Decimal, InvalidOperation

from ..errors import ExtractError
from ..ingest.logs import parse_iso_ts
from .format import plain
from .values import BoolV, CTX, DateTimeV, DateV, DurationV, Number, Text, Value

MONTHS = {m: i for i, m in enumerate(
    "january february march april may june july august september october november december".split(), 1)}
_MONTH_RX = (r"(?P<mon>Jan(?:uary)?|Feb(?:ruary)?|Mar(?:ch)?|Apr(?:il)?|May|June?|July?|Aug(?:ust)?|"
             r"Sep(?:t(?:ember)?)?|Oct(?:ober)?|Nov(?:ember)?|Dec(?:ember)?)\.?")

# (thousands separator class, decimal separator)
LOCALES = {"en": (",", "."), "de": (".", ","), "fr": ("[   ]", ",")}

CURRENCY_SYMBOLS = {"US$": "USD", "C$": "CAD", "A$": "AUD", "NZ$": "NZD", "HK$": "HKD", "S$": "SGD", "$": "USD",
                    "€": "EUR", "£": "GBP", "¥": "JPY", "₹": "INR", "₩": "KRW", "₽": "RUB"}
CURRENCY_CODES = set(
    "USD EUR GBP JPY CNY CAD AUD NZD CHF SEK NOK DKK PLN CZK HUF INR KRW BRL MXN ZAR SGD HKD TWD THB IDR MYR PHP "
    "TRY ILS AED SAR RUB UAH ARS CLP COP PEN EGP NGN KES".split())
CURRENCY_WORDS = {"dollar": "USD", "dollars": "USD", "euro": "EUR", "euros": "EUR", "pound": "GBP", "pounds": "GBP",
                  "yen": "JPY"}
_SYMS = "|".join(re.escape(s) for s in sorted(CURRENCY_SYMBOLS, key=len, reverse=True))
_MAG = {"thousand": 3, "k": 3, "million": 6, "m": 6, "mn": 6, "mm": 6, "billion": 9, "b": 9, "bn": 9, "trillion": 12}


@dataclass(slots=True)
class Parsed:
    value: Value
    consumed: tuple[int, int]  # char offsets into the span text
    info: dict = field(default_factory=dict)


def _clip(text: str, n: int = 80) -> str:
    t = " ".join(text.split())
    return t if len(t) <= n else t[: n - 1] + "…"


def _locale(name: str | None) -> tuple[str, str]:
    key = (name or "en").lower()
    if key not in LOCALES:
        raise ExtractError(f"unknown locale {name!r}; supported: {', '.join(LOCALES)}")
    return LOCALES[key]


# ---------------------------------------------------------------- numbers


def _number_rx(locale: str | None, *, money: bool) -> re.Pattern[str]:
    thou, dec = _locale(locale)
    thou_rx = thou if thou.startswith("[") else re.escape(thou)
    dec_rx = re.escape(dec)
    lo = "kmb" if money else "k"  # lowercase single-letter magnitudes are ambiguous outside money
    return re.compile(
        rf"""(?<![\w.,])
            (?P<sign>[-−])?
            (?P<int>\d{{1,3}}(?:{thou_rx}\d{{3}})+|\d+)
            (?P<frac>{dec_rx}\d+)?
            (?!\d)(?!{dec_rx}\d)(?!{thou_rx}\d)
            (?:\s?(?P<word>thousand|million|billion|trillion)(?![A-Za-z])
              |\s?(?P<abbr>[KMB]|bn|Bn|BN|mn|MM)(?![A-Za-z])
              |(?P<lo>[{lo}])(?![A-Za-z]))?""",
        re.VERBOSE,
    )


def _magnitude(m: re.Match[str]) -> int:
    tok = m.group("word") or m.group("abbr") or m.group("lo")
    return _MAG[tok.lower()] if tok else 0


def _to_decimal(m: re.Match[str], locale: str | None) -> Decimal:
    thou, dec = _locale(locale)
    ip = re.sub(thou, "", m.group("int")) if thou.startswith("[") else m.group("int").replace(thou, "")
    s = ip + ("." + m.group("frac")[1:] if m.group("frac") else "")
    try:
        v = Decimal(s)
    except InvalidOperation as e:  # pragma: no cover - regex guarantees digits
        raise ExtractError(f"cannot read {m.group(0)!r} as a number") from e
    v = CTX.scaleb(v, _magnitude(m))
    return -v if m.group("sign") else v


def _paren_negative(text: str, start: int, end: int) -> bool:
    return text[:start].rstrip().endswith("(") and text[end:].lstrip().startswith(")")


def _no_or_many(kind: str, text: str, found: list[str], hint: str) -> ExtractError:
    if not found:
        return ExtractError(f"no {kind} found in {_clip(text)!r}", hint=hint)
    return ExtractError(
        f"found {len(found)} {kind}s ({', '.join(_clip(f, 30) for f in found[:5])}) in {_clip(text)!r}; "
        f"I will not guess which one you mean",
        hint="quote only the one you want, e.g. $1.\"4.82 million\"",
    )


def _currency_adjacent(before: str, after: str) -> bool:
    """Is a currency symbol/code/word directly attached to a number?"""
    if re.search(rf"(?:{_SYMS})\s?$", before):
        return True
    bm = re.search(r"\b([A-Z]{3})\s?$", before)
    if bm and bm.group(1) in CURRENCY_CODES:
        return True
    am = re.match(r"\s?([A-Z]{3})\b", after)
    if am and am.group(1) in CURRENCY_CODES:
        return True
    return bool(re.match(r"\s?(?:dollars?|euros?|pounds?|yen)\b", after, re.I))


def extract_num(text: str, locale: str | None = None) -> Parsed:
    rx = _number_rx(locale, money=False)
    toks = list(rx.finditer(text))
    if len(toks) != 1:
        raise _no_or_many("number", text, [m.group(0) for m in toks], "the span must contain a number")
    m = toks[0]
    before, after = text[max(0, m.start() - 6) : m.start()], text[m.end() : m.end() + 12]
    if _currency_adjacent(before, after):
        raise ExtractError(f"{_clip(text)!r} has a currency; use MONEY(...) instead of NUM(...)")
    if re.match(r"\s?(?:%|percent\b|per cent\b|pct\b)", after):
        raise ExtractError(f"{_clip(text)!r} is a percentage; use PCT_OF(...) instead of NUM(...)")
    v = _to_decimal(m, locale)
    if _paren_negative(text, m.start(), m.end()):
        v = -abs(v)
    return Parsed(Number(v), (m.start(), m.end()), {"decimal": plain(v)})


def extract_money(text: str, locale: str | None = None) -> Parsed:
    rx = _number_rx(locale, money=True)
    toks = list(rx.finditer(text))
    if len(toks) != 1:
        raise _no_or_many("amount", text, [m.group(0) for m in toks], "the span must contain an amount with a currency")
    m = toks[0]
    start, end = m.start(), m.end()
    currency = None
    before = text[:start]
    bm = re.search(rf"(?P<neg>[-−])?(?P<cur>{_SYMS}|\b[A-Z]{{3}})\s?$", before)
    if bm and (bm.group("cur") in CURRENCY_SYMBOLS or bm.group("cur") in CURRENCY_CODES):
        currency = CURRENCY_SYMBOLS.get(bm.group("cur"), bm.group("cur"))
        neg_prefix = bool(bm.group("neg"))
        start = bm.start("neg") if neg_prefix else bm.start("cur")
    else:
        neg_prefix = False
    if currency is None:
        am = re.match(r"\s?(?P<cur>[A-Z]{3}\b|dollars?\b|euros?\b|pounds?\b|yen\b)", text[end:])
        if am:
            cur = am.group("cur")
            code = CURRENCY_WORDS.get(cur.lower()) if cur.islower() else cur
            if code in CURRENCY_CODES:
                currency, end = code, end + am.end()
    if currency is None:
        raise ExtractError(f"no currency symbol or code next to the amount in {_clip(text)!r}",
                           hint="use NUM(...) for plain numbers; MONEY needs e.g. $, EUR or 'USD'")
    v = _to_decimal(m, locale)
    if neg_prefix or _paren_negative(text, start, end):
        v = -abs(v)
    return Parsed(Number(v, currency), (start, end), {"amount": plain(v), "currency": currency})


def extract_pct(text: str, locale: str | None = None) -> Parsed:
    rx = _number_rx(locale, money=False)
    hits = []
    for m in rx.finditer(text):
        am = re.match(r"\s?(?:%|percent\b|per cent\b|pct\b)", text[m.end() :])
        if am:
            hits.append((m, m.end() + am.end()))
    if len(hits) != 1:
        raise _no_or_many("percentage", text, [text[m.start() : e] for m, e in hits],
                          "the span must contain a number followed by % or 'percent'")
    m, end = hits[0]
    return Parsed(Number(_to_decimal(m, locale), "%"), (m.start(), end), {"decimal": plain(_to_decimal(m, locale))})


# ------------------------------------------------------------------- dates


def _mk_date(y: int, mo: int, d: int, text: str) -> date:
    try:
        return date(y, mo, d)
    except ValueError as e:
        raise ExtractError(f"{y:04d}-{mo:02d}-{d:02d} in {_clip(text)!r} is not a real calendar date") from e


def _mon(name: str) -> int:
    key = name.lower().rstrip(".")
    for full, i in MONTHS.items():
        if full.startswith(key[:3]) and (full.startswith(key) or key == "sept"):
            return i
    raise ExtractError(f"unknown month {name!r}")


def extract_date(text: str, fmt: str | None = None, year: int | None = None) -> Parsed:
    fmt = (fmt or "").upper() or None
    if fmt not in (None, "MDY", "DMY", "YMD"):
        raise ExtractError(f"unknown date fmt {fmt!r}; use 'MDY', 'DMY' or 'YMD'")
    found: list[tuple[int, int, date]] = []
    taken: list[tuple[int, int]] = []

    def add(m: re.Match[str], d: date) -> None:
        if not any(m.start() < e and s < m.end() for s, e in taken):
            taken.append((m.start(), m.end()))
            found.append((m.start(), m.end(), d))

    for m in re.finditer(r"(?<!\d)(\d{4})[-/.](\d{1,2})[-/.](\d{1,2})(?!\d)", text):
        add(m, _mk_date(int(m.group(1)), int(m.group(2)), int(m.group(3)), text))
    for m in re.finditer(rf"\b{_MONTH_RX}\s+(\d{{1,2}})(?:st|nd|rd|th)?,?\s+(\d{{4}})\b", text, re.I):
        add(m, _mk_date(int(m.group(3)), _mon(m.group("mon")), int(m.group(2)), text))
    for m in re.finditer(rf"\b(\d{{1,2}})(?:st|nd|rd|th)?[\s\-]+(?:of\s+)?{_MONTH_RX},?[\s\-]+(\d{{4}})\b", text, re.I):
        add(m, _mk_date(int(m.group(3)), _mon(m.group("mon")), int(m.group(1)), text))
    for m in re.finditer(r"(?<![\d/.\-])(\d{1,2})[/.\-](\d{1,2})[/.\-](\d{4})(?![\d/.\-])", text):
        a, b, y = int(m.group(1)), int(m.group(2)), int(m.group(3))
        order = fmt
        if order is None:
            if a > 12 and b <= 12:
                order = "DMY"
            elif b > 12 and a <= 12:
                order = "MDY"
            elif a == b:
                order = "MDY"
            else:
                raise ExtractError(f"{m.group(0)!r} is ambiguous (month-first or day-first)",
                                   hint="pass fmt:\"MDY\" or fmt:\"DMY\", or quote a date written with a month name")
        mo, d = (a, b) if order == "MDY" else (b, a)
        add(m, _mk_date(y, mo, d, text))
    if not found:
        for m in list(re.finditer(rf"\b{_MONTH_RX}\s+(\d{{1,2}})(?:st|nd|rd|th)?\b", text, re.I)):
            d = int(m.group(2))
            if year is None:
                raise ExtractError(f"{m.group(0)!r} has no year", hint="quote a span that includes the year, "
                                   "or pass year: with the year from the question")
            add(m, _mk_date(year, _mon(m.group("mon")), d, text))
        for m in list(re.finditer(rf"\b(\d{{1,2}})(?:st|nd|rd|th)?\s+(?:of\s+)?{_MONTH_RX}\b", text, re.I)):
            if year is None:
                raise ExtractError(f"{m.group(0)!r} has no year", hint="quote a span that includes the year, "
                                   "or pass year: with the year from the question")
            add(m, _mk_date(year, _mon(m.group("mon")), int(m.group(1)), text))
    if not found:
        if re.search(rf"\b{_MONTH_RX}\s+\d{{4}}\b", text, re.I):
            raise ExtractError(f"{_clip(text)!r} names a month but no day")
        raise ExtractError(f"no date found in {_clip(text)!r}")
    found.sort()
    distinct = {d for _, _, d in found}
    if len(distinct) > 1:
        raise ExtractError(f"found {len(distinct)} different dates ({', '.join(sorted(map(str, distinct)))}) in "
                           f"{_clip(text)!r}; quote only the one you want")
    s, e, d = found[0]
    return Parsed(DateV(d), (s, e), {"iso": d.isoformat()})


_DT_ISO = re.compile(r"\d{4}-\d{2}-\d{2}[T ]\d{2}:\d{2}(?::\d{2}(?:[.,]\d{1,9})?)?(?:\s?(?:Z|[+-]\d{2}:?\d{2}))?")
_DT_NGINX = re.compile(r"\d{1,2}/[A-Za-z]{3}/\d{4}:\d{2}:\d{2}:\d{2}(?:\s[+-]\d{4})?")


def extract_datetime(text: str) -> Parsed:
    hits: list[tuple[int, int, datetime]] = []
    for m in _DT_ISO.finditer(text):
        s = m.group(0)
        core = re.sub(r"^(\d{4}-\d{2}-\d{2}[T ]\d{2}:\d{2})(?!:)", r"\1:00", s)
        dt = parse_iso_ts(core)
        if dt is None:
            raise ExtractError(f"{s!r} is not a valid date and time")
        hits.append((m.start(), m.end(), dt))
    for m in _DT_NGINX.finditer(text):
        from ..ingest.logs import parse_timestamp

        ts = parse_timestamp(m.group(0))
        dt = parse_iso_ts(ts.ts) if ts else None
        if dt is not None:
            hits.append((m.start(), m.end(), dt))
    if not hits:
        if re.search(r"\d{4}-\d{2}-\d{2}", text):
            raise ExtractError(f"{_clip(text)!r} has a date but no time of day; use DATE(...)")
        raise ExtractError(f"no date and time found in {_clip(text)!r}")
    if len({h[2] for h in hits}) > 1:
        raise ExtractError(f"found {len(hits)} timestamps in {_clip(text)!r}; quote only the one you want")
    s, e, dt = hits[0]
    return Parsed(DateTimeV(dt), (s, e), {"iso": dt.isoformat()})


_UNIT_SECONDS = {"ms": Decimal("0.001"), "s": Decimal(1), "m": Decimal(60), "h": Decimal(3600),
                 "d": Decimal(86400), "w": Decimal(604800)}
_UNIT_ALIASES = {"millisecond": "ms", "milliseconds": "ms", "second": "s", "seconds": "s", "sec": "s", "secs": "s",
                 "minute": "m", "minutes": "m", "min": "m", "mins": "m", "hour": "h", "hours": "h", "hr": "h",
                 "hrs": "h", "day": "d", "days": "d", "week": "w", "weeks": "w"}
_DUR_TOKEN = re.compile(
    r"(?<![\w.])(?P<n>\d+(?:\.\d+)?)\s*(?P<u>milliseconds?|ms|seconds?|secs?|minutes?|mins?|hours?|hrs?|days?|weeks?"
    r"|s|m|h|d|w)(?![A-Za-z])")


def _td(seconds: Decimal) -> timedelta:
    return timedelta(microseconds=int((seconds * 1_000_000).to_integral_value()))


def extract_duration(text: str) -> Parsed:
    m = re.search(r"\bP(?:(\d+)D)?(?:T(?:(\d+)H)?(?:(\d+)M)?(?:(\d+(?:\.\d+)?)S)?)?\b", text)
    if m and m.group(0) != "P" and any(m.groups()):
        d, h, mi, s = (Decimal(g or 0) for g in m.groups())
        return Parsed(DurationV(_td(d * 86400 + h * 3600 + mi * 60 + s)), (m.start(), m.end()), {"iso8601": m.group(0)})
    m = re.search(r"(?<![\d:])(\d+):([0-5]\d):([0-5]\d(?:\.\d+)?)(?![\d:])", text)
    if m:
        secs = Decimal(m.group(1)) * 3600 + Decimal(m.group(2)) * 60 + Decimal(m.group(3))
        return Parsed(DurationV(_td(secs)), (m.start(), m.end()), {"clock": m.group(0)})
    toks = list(_DUR_TOKEN.finditer(text))
    if not toks:
        raise ExtractError(f"no duration found in {_clip(text)!r}", hint="expected e.g. '250 ms', '3h 20m', '1.5 hours'")
    runs: list[list[re.Match[str]]] = [[toks[0]]]
    for prev, cur in zip(toks, toks[1:]):
        if re.fullmatch(r"[\s,]*(?:and\s+)?", text[prev.end() : cur.start()]):
            runs[-1].append(cur)
        else:
            runs.append([cur])
    if len(runs) > 1:
        raise ExtractError(f"found {len(runs)} separate durations in {_clip(text)!r}; quote only the one you want")
    total = Decimal(0)
    for t in runs[0]:
        unit = _UNIT_ALIASES.get(t.group("u").lower(), t.group("u").lower())
        total += Decimal(t.group("n")) * _UNIT_SECONDS[unit]
    return Parsed(DurationV(_td(total)), (runs[0][0].start(), runs[0][-1].end()), {"seconds": plain(total)})


_TRUE = {"yes", "true", "enabled", "on", "y", "active", "allowed"}
_FALSE = {"no", "false", "disabled", "off", "n", "inactive", "denied"}


def extract_bool(text: str) -> Parsed:
    s = text.strip().strip(".,;:!").strip().lower()
    if s in _TRUE:
        v = True
    elif s in _FALSE:
        v = False
    else:
        raise ExtractError(f"cannot read {_clip(text)!r} as yes/no",
                           hint="the span must be just yes/no, true/false, enabled/disabled or on/off")
    lead = len(text) - len(text.lstrip())
    return Parsed(BoolV(v), (lead, lead + len(text.strip())), {})


def extract_text(text: str) -> Parsed:
    return Parsed(Text(text), (0, len(text)), {})


def extract(kind: str, text: str, **kw) -> Parsed:
    """Dispatch by extractor name (``num``, ``money``, ``pct``, ``date``, ``datetime``, ``duration``, ``text``, ``bool``)."""
    k = kind.lower()
    if k == "num":
        return extract_num(text, kw.get("locale"))
    if k == "money":
        return extract_money(text, kw.get("locale"))
    if k in ("pct", "pct_of"):
        return extract_pct(text, kw.get("locale"))
    if k == "date":
        return extract_date(text, kw.get("fmt"), kw.get("year"))
    if k == "datetime":
        return extract_datetime(text)
    if k == "duration":
        return extract_duration(text)
    if k == "bool":
        return extract_bool(text)
    if k == "text":
        return extract_text(text)
    raise ExtractError(f"unknown extractor {kind!r}")

