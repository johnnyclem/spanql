"""Log converter: groups lines into records and extracts ts/level/logger/message/fields.

Timestamps are stored as written (ISO-normalised, *not* converted to UTC), so ``day:`` filters
match the calendar day printed in the log. Syslog lines without a year keep ``ts`` NULL rather
than guessing one.
"""

from __future__ import annotations

import json
import re
from collections.abc import Callable
from datetime import datetime
from typing import Any

from spanql.blocks import BlockSpec, Converted
from spanql.ingest.text import ByteIndex, line_spans, normalize

LEVELS = {
    "TRACE": "TRACE",
    "DEBUG": "DEBUG",
    "INFO": "INFO",
    "NOTICE": "INFO",
    "WARN": "WARN",
    "WARNING": "WARN",
    "ERROR": "ERROR",
    "ERR": "ERROR",
    "CRITICAL": "CRITICAL",
    "CRIT": "CRITICAL",
    "FATAL": "FATAL",
    "SEVERE": "ERROR",
    "PANIC": "FATAL",
}
_TS = r"\d{4}-\d{2}-\d{2}[T ]\d{2}:\d{2}:\d{2}(?:[.,]\d{1,9})?(?:Z|[+-]\d{2}:?\d{2})?"
GENERIC_RE = re.compile(
    rf"^\[?(?P<ts>{_TS})\]?\s*(?:\[?(?P<level>{'|'.join(LEVELS)})\]?\b)?\s*"
    r"(?:(?:\[(?P<logger1>[\w.\-/:]+)\])|(?P<logger2>[\w.\-]+(?:/[\w.\-]+)*):(?=\s))?\s*"
    r"(?P<msg>.*)$",
    re.IGNORECASE,
)
ACCESS_RE = re.compile(
    r'^(?P<ip>\S+) \S+ (?P<user>\S+) \[(?P<ts>[^\]]+)\] "(?P<method>[A-Z]+) (?P<path>\S+)'
    r' ?(?P<protocol>[^"]*)" (?P<status>\d{3}) (?P<bytes>\d+|-)'
    r'(?: "(?P<referer>[^"]*)" "(?P<agent>[^"]*)")?(?P<rest>.*)$'
)
SYSLOG_RE = re.compile(
    r"^(?P<mon>[A-Z][a-z]{2}) +(?P<day>\d{1,2}) (?P<time>\d\d:\d\d:\d\d) (?P<host>\S+) "
    r"(?P<prog>[\w\-/.@]+)(?:\[(?P<pid>\d+)\])?: (?P<msg>.*)$"
)
SYSLOG5424_RE = re.compile(
    rf"^<(?P<pri>\d+)>1 (?P<ts>{_TS}) (?P<host>\S+) (?P<prog>\S+) (?P<pid>\S+) (?P<msgid>\S+)"
    r" (?:-|\[.*?\]) ?(?P<msg>.*)$"
)
KV_RE = re.compile(r'(?<![\w.])([A-Za-z_][\w.\-]*)=("(?:[^"\\]|\\.)*"|[^\s,;]+)')
_INT_RE = re.compile(r"^-?\d+$")
_FLOAT_RE = re.compile(r"^-?\d+\.\d+$")


def _scalar(v: str) -> Any:
    if _INT_RE.match(v):
        try:
            return int(v)
        except ValueError:
            return v
    if _FLOAT_RE.match(v):
        return float(v)
    return v


def _kv(msg: str) -> dict[str, Any]:
    out: dict[str, Any] = {}
    for k, v in KV_RE.findall(msg):
        if v.startswith('"') and v.endswith('"'):
            v = v[1:-1]
        out[k] = _scalar(v)
    return out


def norm_ts(ts: str) -> str:
    """Normalise an ISO-ish timestamp: 'T' separator, '.' fraction, ±HH:MM offset."""
    ts = ts.strip()
    if len(ts) > 10 and ts[10] == " ":
        ts = ts[:10] + "T" + ts[11:]
    ts = ts.replace(",", ".")
    m = re.search(r"([+-])(\d{2})(\d{2})$", ts)
    if m and ":" not in ts[-6:]:
        ts = ts[: m.start()] + f"{m.group(1)}{m.group(2)}:{m.group(3)}"
    return ts


Parsed = dict[str, Any]


def parse_generic(line: str) -> Parsed | None:
    m = GENERIC_RE.match(line)
    if not m:
        return None
    msg = m.group("msg") or ""
    lvl = m.group("level")
    return {
        "ts": norm_ts(m.group("ts")),
        "level": LEVELS.get(lvl.upper()) if lvl else None,
        "logger": m.group("logger1") or m.group("logger2"),
        "msg_offset": m.start("msg"),
        "msg": msg,
        "fields": _kv(msg),
    }


def parse_jsonl(line: str) -> Parsed | None:
    s = line.strip()
    if not (s.startswith("{") and s.endswith("}")):
        return None
    try:
        obj = json.loads(s)
    except ValueError:
        return None
    if not isinstance(obj, dict):
        return None
    flat = {k: v for k, v in obj.items() if isinstance(v, str | int | float | bool) or v is None}
    ts = next((obj[k] for k in ("ts", "time", "timestamp", "@timestamp", "date") if k in obj), None)
    lvl = next((obj[k] for k in ("level", "lvl", "severity", "levelname") if k in obj), None)
    logger = next((obj[k] for k in ("logger", "name", "module", "component") if k in obj), None)
    msg = next((obj[k] for k in ("msg", "message", "event") if k in obj), "")
    return {
        "ts": norm_ts(ts) if isinstance(ts, str) and re.match(_TS, ts) else None,
        "level": LEVELS.get(str(lvl).upper()) if lvl is not None else None,
        "logger": str(logger) if logger is not None else None,
        "msg_offset": 0,
        "msg": str(msg),
        "fields": flat,
    }


def parse_access(line: str) -> Parsed | None:
    m = ACCESS_RE.match(line)
    if not m:
        return None
    try:
        ts = datetime.strptime(m.group("ts"), "%d/%b/%Y:%H:%M:%S %z").isoformat()
    except ValueError:
        ts = None
    status = int(m.group("status"))
    fields: dict[str, Any] = {
        "ip": m.group("ip"),
        "user": m.group("user"),
        "method": m.group("method"),
        "path": m.group("path"),
        "protocol": m.group("protocol").strip(),
        "status": status,
        "bytes": 0 if m.group("bytes") == "-" else int(m.group("bytes")),
    }
    if m.group("referer") is not None:
        fields["referer"] = m.group("referer")
        fields["agent"] = m.group("agent")
    fields.update(_kv(m.group("rest") or ""))
    level = "ERROR" if status >= 500 else "WARN" if status >= 400 else "INFO"
    return {
        "ts": ts,
        "level": level,
        "logger": None,
        "msg_offset": m.start("method"),
        "msg": line[m.start("method") :],
        "fields": fields,
    }


def parse_syslog(line: str) -> Parsed | None:
    m = SYSLOG5424_RE.match(line)
    if m:
        msg = m.group("msg")
        f = {"host": m.group("host"), "program": m.group("prog"), **_kv(msg)}
        return {
            "ts": norm_ts(m.group("ts")),
            "level": None,
            "logger": m.group("prog"),
            "msg_offset": m.start("msg"),
            "msg": msg,
            "fields": f,
        }
    m = SYSLOG_RE.match(line)
    if not m:
        return None
    msg = m.group("msg")
    fields: dict[str, Any] = {
        "host": m.group("host"),
        "program": m.group("prog"),
        "syslog_time": f"{m.group('mon')} {m.group('day')} {m.group('time')}",
    }
    if m.group("pid"):
        fields["pid"] = int(m.group("pid"))
    fields.update(_kv(msg))
    lvl = next((LEVELS[w] for w in re.findall(r"\b([A-Z]{3,8})\b", msg[:40]) if w in LEVELS), None)
    return {
        "ts": None,
        "level": lvl,
        "logger": m.group("prog"),
        "msg_offset": m.start("msg"),
        "msg": msg,
        "fields": fields,
    }


def parse_plain(line: str) -> Parsed | None:
    lvl = next(
        (
            LEVELS[w.upper()]
            for w in re.findall(r"\b([A-Za-z]{3,8})\b", line[:60])
            if w.upper() in LEVELS and w.isupper()
        ),
        None,
    )
    return {
        "ts": None,
        "level": lvl,
        "logger": None,
        "msg_offset": 0,
        "msg": line,
        "fields": _kv(line),
    }


PARSERS: dict[str, Callable[[str], Parsed | None]] = {
    "jsonl": parse_jsonl,
    "access": parse_access,
    "syslog": parse_syslog,
    "generic": parse_generic,
}


def detect_parser(lines: list[str]) -> str:
    sample = [ln for ln in lines if ln.strip()][:50]
    if not sample:
        return "plain"
    best, best_n = "plain", 0
    for name, fn in PARSERS.items():
        n = sum(1 for ln in sample if fn(ln) is not None)
        if n > best_n:
            best, best_n = name, n
    return best if best_n >= max(1, len(sample) // 3) else "plain"


def convert_log(raw: bytes, parser: str = "auto") -> Converted:
    text, omap = normalize(raw)
    bi = ByteIndex(text)
    spans = line_spans(text)
    lines = [text[s:e] for s, e in spans]
    name = detect_parser(lines) if parser == "auto" else parser
    if name != "plain" and name not in PARSERS:
        raise ValueError(f"unknown log parser {parser!r}")
    fn = PARSERS.get(name, parse_plain)

    records: list[tuple[int, int, Parsed]] = []  # (first line idx, last line idx, parsed)
    for i, line in enumerate(lines):
        if not line.strip():
            continue
        parsed = fn(line)
        if parsed is None and records and (line[:1].isspace() or name != "plain"):
            # continuation (stack trace, wrapped message)
            first, _, p = records[-1]
            records[-1] = (first, i, p)
            continue
        records.append((i, i, parsed or parse_plain(line) or {}))

    blocks: list[BlockSpec] = []
    for first, last, p in records:
        s, e = spans[first][0], spans[last][1]
        ms = s + p["msg_offset"]
        blocks.append(
            BlockSpec(
                "log_record",
                bi(s),
                bi(e),
                path=None,
                log={
                    "ts": p["ts"],
                    "level": p["level"],
                    "logger": p["logger"],
                    "message_start": bi(ms),
                    "message_end": bi(e),
                    "fields": p["fields"],
                },
            )
        )
    meta = {"parser": name, "records": len(blocks)}
    return Converted(text, blocks, f"log-{name}/1", omap, meta)
