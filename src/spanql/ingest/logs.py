"""Log segmentation: one block per record (a line, or a line plus continuation lines).

Profiles: ``jsonl``, ``nginx`` (common/combined), ``iso`` (ISO-8601 timestamp first,
the shape of most application logs), ``syslog`` (needs a year), ``plain`` (no
timestamps), and ``re:<pattern>`` for a custom regex with named groups
(``ts``, ``level``, ``logger``, ``message``; other groups become fields).
"""

from __future__ import annotations

import json
import re
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
from typing import Callable

from ..blocks import Block
from .textseg import line_spans

LEVEL_ALIASES = {"WARNING": "WARN", "ERR": "ERROR", "CRIT": "CRITICAL", "SEVERE": "ERROR", "EMERG": "FATAL"}
_LEVEL_WORDS = r"TRACE|DEBUG|INFO|NOTICE|WARN(?:ING)?|ERROR|ERR|CRIT(?:ICAL)?|FATAL|SEVERE|ALERT|EMERG"
_NUMERIC_LEVELS = {10: "TRACE", 20: "DEBUG", 30: "INFO", 40: "WARN", 50: "ERROR", 60: "FATAL"}  # pino/bunyan

_ISO = re.compile(
    r"(\d{4})-(\d{2})-(\d{2})[T ](\d{2}):(\d{2}):(\d{2})(?:[.,](\d{1,9}))?\s?(Z|[+-]\d{2}:?\d{2})?$"
)
ISO_TS = r"\d{4}-\d{2}-\d{2}[T ]\d{2}:\d{2}:\d{2}(?:[.,]\d{1,9})?(?:\s?(?:Z|[+-]\d{2}:?\d{2}))?"
_MONTHS = {m: i for i, m in enumerate("Jan Feb Mar Apr May Jun Jul Aug Sep Oct Nov Dec".split(), 1)}


@dataclass(frozen=True, slots=True)
class Timestamp:
    ts: str  # ISO-8601 as written (offset kept)
    utc: str  # fixed-width UTC, sortable
    day: str  # calendar date as written


def _make_ts(dt: datetime) -> Timestamp:
    ts = dt.isoformat(timespec="microseconds" if dt.microsecond else "seconds")
    aware = dt.astimezone(timezone.utc) if dt.tzinfo else dt.replace(tzinfo=timezone.utc)
    return Timestamp(ts, aware.strftime("%Y-%m-%dT%H:%M:%S.%fZ"), dt.date().isoformat())


def parse_iso_ts(s: str) -> datetime | None:
    m = _ISO.match(s.strip())
    if not m:
        return None
    y, mo, d, h, mi, sec, frac, tz = m.groups()
    micro = int(((frac or "") + "000000")[:6])
    tzinfo = None
    if tz:
        if tz == "Z":
            tzinfo = timezone.utc
        else:
            t = tz.replace(":", "")
            sign = -1 if t[0] == "-" else 1
            tzinfo = timezone(sign * timedelta(hours=int(t[1:3]), minutes=int(t[3:5])))
    try:
        return datetime(int(y), int(mo), int(d), int(h), int(mi), int(sec), micro, tzinfo)
    except ValueError:
        return None


def parse_timestamp(value, *, year: int | None = None) -> Timestamp | None:
    """Parse ISO, nginx (``10/Nov/2026:10:15:03 +0000``), syslog (needs ``year``) or epoch values."""
    if isinstance(value, bool) or value is None:
        return None
    if isinstance(value, (int, float)):
        v = float(value)
        if v > 1e11:  # milliseconds
            v /= 1000.0
        try:
            return _make_ts(datetime.fromtimestamp(v, timezone.utc))
        except (OverflowError, OSError, ValueError):
            return None
    s = str(value).strip()
    dt = parse_iso_ts(s)
    if dt is None:
        m = re.match(r"^(\d{1,2})/([A-Za-z]{3})/(\d{4}):(\d{2}):(\d{2}):(\d{2})\s?([+-]\d{4})?$", s)
        if m and m.group(2).title() in _MONTHS:
            tz = m.group(7) or "+0000"
            sign = -1 if tz[0] == "-" else 1
            tzinfo = timezone(sign * timedelta(hours=int(tz[1:3]), minutes=int(tz[3:5])))
            try:
                dt = datetime(int(m.group(3)), _MONTHS[m.group(2).title()], int(m.group(1)),
                              int(m.group(4)), int(m.group(5)), int(m.group(6)), 0, tzinfo)
            except ValueError:
                dt = None
    if dt is None and year is not None:
        m = re.match(r"^([A-Za-z]{3})\s+(\d{1,2})\s+(\d{2}):(\d{2}):(\d{2})$", s)
        if m and m.group(1).title() in _MONTHS:
            try:
                dt = datetime(year, _MONTHS[m.group(1).title()], int(m.group(2)), int(m.group(3)),
                              int(m.group(4)), int(m.group(5)))
            except ValueError:
                dt = None
    if dt is None and re.fullmatch(r"\d{9,13}(\.\d+)?", s):
        return parse_timestamp(float(s))
    return _make_ts(dt) if dt else None


# ------------------------------------------------------------------ fields

_LOGFMT = re.compile(r'(?<![\w.])([A-Za-z_][\w.\-]*)=("(?:[^"\\]|\\.)*"|[^\s,;]+)')


def _typed(v: str):
    if re.fullmatch(r"-?(?:0|[1-9]\d{0,14})", v):
        return int(v)
    if re.fullmatch(r"-?(?:0|[1-9]\d{0,14})\.\d{1,12}", v):
        return float(v)
    return v


def logfmt_fields(text: str) -> dict:
    out: dict = {}
    for m in _LOGFMT.finditer(text):
        k, v = m.group(1), m.group(2)
        if v.startswith('"') and v.endswith('"') and len(v) >= 2:
            v = v[1:-1].replace('\\"', '"')
            out.setdefault(k, v)
        else:
            out.setdefault(k, _typed(v))
    return out


def normalize_level(v) -> str | None:
    if v is None or isinstance(v, bool):
        return None
    if isinstance(v, (int, float)):
        return _NUMERIC_LEVELS.get(int(v))
    s = str(v).strip().upper()
    if not s:
        return None
    return LEVEL_ALIASES.get(s, s)


# ---------------------------------------------------------------- profiles


@dataclass(slots=True)
class Parsed:
    ts: Timestamp | None
    level: str | None
    logger: str | None
    fields: dict
    message_start: int | None = None  # char offsets into the first line
    message_end: int | None = None


Extractor = Callable[[str], "Parsed | None"]  # first line of a record -> parsed, or None if not a record start


def _iso_profile(year=None) -> Extractor:
    start = re.compile(rf"^\[?(?P<ts>{ISO_TS})\]?")
    level_rx = re.compile(rf"(?<![A-Za-z])(?P<level>{_LEVEL_WORDS})(?![A-Za-z])")
    logger_rx = re.compile(r"^\s*(?P<logger>[A-Za-z_][\w.$\-/]*)\s*(?::|-)\s")

    def parse(line: str) -> Parsed | None:
        m = start.match(line)
        if not m:
            return None
        ts = parse_timestamp(m.group("ts").replace(",", "."))
        rest_at = m.end()
        rest = line[rest_at:]
        level = logger = None
        msg_at = rest_at + (len(rest) - len(rest.lstrip()))
        lm = level_rx.search(rest[:60])
        if lm:
            level = normalize_level(lm.group("level"))
            after = rest_at + lm.end()
            tail = line[after:]
            gm = logger_rx.match(tail)
            if gm and gm.group("logger").upper() not in LEVEL_ALIASES and not gm.group("logger").isdigit():
                logger = gm.group("logger")
                msg_at = after + gm.end()
            else:
                msg_at = after + (len(tail) - len(tail.lstrip(" :-]")))
        fields = logfmt_fields(line[msg_at:])
        return Parsed(ts, level, logger, fields, msg_at, len(line))

    return parse


_NGINX = re.compile(
    r'^(?P<ip>\S+) \S+ (?P<user>\S+) \[(?P<ts>[^\]]+)\] "(?P<method>[A-Z]+) (?P<path>\S+)(?: (?P<proto>[^"]*))?" '
    r'(?P<status>\d{3}) (?P<bytes>\d+|-)(?: "(?P<referer>[^"]*)" "(?P<ua>[^"]*)")?'
)


def _nginx_profile(year=None) -> Extractor:
    def parse(line: str) -> Parsed | None:
        m = _NGINX.match(line)
        if not m:
            return None
        g = m.groupdict()
        fields: dict = {"ip": g["ip"], "method": g["method"], "path": g["path"], "status": int(g["status"])}
        if g["user"] != "-":
            fields["user"] = g["user"]
        if g["proto"]:
            fields["protocol"] = g["proto"]
        if g["bytes"] != "-":
            fields["bytes"] = int(g["bytes"])
        for k in ("referer", "ua"):
            if g.get(k) and g[k] != "-":
                fields[k] = g[k]
        return Parsed(parse_timestamp(g["ts"]), None, None, fields, m.start("method") - 1, len(line))

    return parse


_SYSLOG = re.compile(
    r"^(?P<ts>[A-Z][a-z]{2}\s+\d{1,2}\s\d{2}:\d{2}:\d{2})\s(?P<host>\S+)\s(?P<proc>[\w./\-]+)(?:\[(?P<pid>\d+)\])?:\s?"
)


def _syslog_profile(year=None) -> Extractor:
    def parse(line: str) -> Parsed | None:
        m = _SYSLOG.match(line)
        if not m:
            return None
        fields: dict = {"host": m.group("host")}
        if m.group("pid"):
            fields["pid"] = int(m.group("pid"))
        fields.update(logfmt_fields(line[m.end():]))
        ts = parse_timestamp(re.sub(r"\s+", " ", m.group("ts")), year=year)
        return Parsed(ts, None, m.group("proc"), fields, m.end(), len(line))

    return parse


_JSON_TS = ("ts", "time", "timestamp", "@timestamp", "datetime", "t")
_JSON_LEVEL = ("level", "severity", "lvl", "levelname", "log.level")
_JSON_LOGGER = ("logger", "name", "component", "module", "logger_name")


def _jsonl_profile(year=None) -> Extractor:
    def parse(line: str) -> Parsed | None:
        if not line.startswith("{"):
            return None
        try:
            obj = json.loads(line)
        except ValueError:
            return None
        if not isinstance(obj, dict):
            return None
        ts = next((parse_timestamp(obj[k]) for k in _JSON_TS if k in obj and parse_timestamp(obj[k])), None)
        level = next((normalize_level(obj[k]) for k in _JSON_LEVEL if k in obj), None)
        logger = next((str(obj[k]) for k in _JSON_LOGGER if k in obj and isinstance(obj[k], str)), None)
        return Parsed(ts, level, logger, obj, 0, len(line))

    return parse


def _custom_profile(pattern: str, year=None) -> Extractor:
    rx = re.compile(pattern)

    def parse(line: str) -> Parsed | None:
        m = rx.match(line)
        if not m:
            return None
        g = {k: v for k, v in m.groupdict().items() if v is not None}
        ts = parse_timestamp(g.pop("ts", None), year=year) if "ts" in g else None
        level = normalize_level(g.pop("level", None))
        logger = g.pop("logger", None)
        msg_start = m.start("message") if "message" in m.groupdict() and m.group("message") is not None else m.end()
        g.pop("message", None)
        return Parsed(ts, level, logger, {k: _typed(v) for k, v in g.items()}, msg_start, len(line))

    return parse


PROFILES: dict[str, Callable[..., Extractor]] = {
    "jsonl": _jsonl_profile,
    "nginx": _nginx_profile,
    "iso": _iso_profile,
    "syslog": _syslog_profile,
}


def detect_profile(lines: list[str], year: int | None = None) -> str:
    """Pick the profile that recognises most of the first non-blank lines (>= 60%), else ``plain``."""
    sample = [ln for ln in lines if ln.strip()][:60]
    if not sample:
        return "plain"
    best, best_ratio = "plain", 0.0
    for name in ("jsonl", "nginx", "iso", "syslog"):
        ex = PROFILES[name](year)
        ratio = sum(1 for ln in sample if ex(ln) is not None) / len(sample)
        if ratio > best_ratio:
            best, best_ratio = name, ratio
    return best if best_ratio >= 0.6 else "plain"


def segment_log(data: bytes, parser: str = "auto", *, year: int | None = None) -> tuple[list[Block], str]:
    """Return (blocks, profile name actually used)."""
    spans = line_spans(data)
    texts = [data[s:e].decode("utf-8", "replace") for s, e in spans]
    name = parser
    if parser == "auto":
        name = detect_profile(texts[:200], year)
    if name.startswith("re:"):
        extract = _custom_profile(name[3:], year)
    elif name == "plain":
        extract = None
    elif name in PROFILES:
        extract = PROFILES[name](year)
    else:
        raise ValueError(f"unknown log parser {parser!r}")

    blocks: list[Block] = []
    cur_first: int | None = None
    cur_parsed: Parsed | None = None

    def flush(last: int) -> None:
        nonlocal cur_first, cur_parsed
        if cur_first is None:
            return
        start, end = spans[cur_first][0], spans[last][1]
        while end > start and data[end - 1] in b" \t\r":
            end -= 1
        p = cur_parsed
        rec: dict = {"fields": p.fields if p else {}}
        if p:
            if p.ts:
                rec.update(ts=p.ts.ts, ts_utc=p.ts.utc, day=p.ts.day)
            rec["level"], rec["logger"] = p.level, p.logger
            first_text = texts[cur_first]
            if p.message_start is not None:
                rec["message_start"] = start + len(first_text[: p.message_start].encode("utf-8"))
                rec["message_end"] = end
        text = data[start:end].decode("utf-8", "replace")
        blocks.append(Block("log_record", start, end, is_unit=True, fts=text + "\n" + split_identifiers(text), record=rec))
        cur_first = cur_parsed = None

    for i, line in enumerate(texts):
        if not line.strip():
            if cur_first is not None:
                flush(i - 1)
            continue
        parsed = extract(line) if extract else None
        starts_record = extract is None or parsed is not None
        if starts_record:
            if cur_first is not None:
                flush(i - 1)
            cur_first, cur_parsed = i, parsed
        elif cur_first is None:  # continuation with nothing to continue: its own record
            cur_first, cur_parsed = i, None
    if cur_first is not None:
        flush(len(spans) - 1)
    return blocks, name


_IDENT_PART = re.compile(r"[A-Z]+(?![a-z])|[A-Z]?[a-z]+|\d+")


def split_identifiers(text: str, *, max_extra: int = 400) -> str:
    """Extra FTS terms: ``PaymentTimeout`` -> ``Payment Timeout``; ``retry_with_backoff`` -> ``retry with backoff``."""
    parts: list[str] = []
    for tok in re.findall(r"[A-Za-z_][A-Za-z0-9_]{3,}", text):
        if "_" in tok or any(c.isupper() for c in tok[1:]):
            parts.extend(p.lower() for p in _IDENT_PART.findall(tok.replace("_", " ")))
        if len(parts) > max_extra:
            break
    return " ".join(parts)
