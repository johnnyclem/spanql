from __future__ import annotations

import pytest

from spanql.ingest.logs import (
    convert_log,
    detect_parser,
    parse_access,
    parse_generic,
    parse_jsonl,
    parse_syslog,
)


def test_generic_with_logfmt_fields() -> None:
    p = parse_generic("2026-11-10T08:00:05Z ERROR payments.api: PaymentTimeout status=504 order=A1")
    assert p is not None
    assert p["level"] == "ERROR" and p["logger"] == "payments.api"
    assert p["fields"]["status"] == 504 and p["fields"]["order"] == "A1"


def test_jsonl() -> None:
    p = parse_jsonl('{"ts":"2026-11-10T08:00:00Z","level":"warn","msg":"slow","latency_ms":900}')
    assert p is not None
    assert p["level"] == "WARN"
    assert p["fields"]["latency_ms"] == 900
    assert parse_jsonl("not json") is None


def test_access_combined() -> None:
    line = '10.0.0.1 - - [10/Nov/2026:08:00:00 +0000] "GET /pay HTTP/1.1" 502 12 "-" "curl/8"'
    p = parse_access(line)
    assert p is not None
    assert p["fields"]["status"] == 502
    assert p["fields"]["path"] == "/pay"
    assert p["ts"].startswith("2026-11-10T08:00:00")


def test_syslog_3164_has_no_year() -> None:
    p = parse_syslog("Nov 10 08:00:00 host sshd[12]: Accepted key")
    assert p is not None and p["ts"] is None
    p2 = parse_syslog("<34>1 2026-11-10T08:00:00Z host app 1 ID47 - message here")
    assert p2 is not None and p2["ts"].startswith("2026-11-10")


def test_continuation_lines_join_previous_record() -> None:
    raw = (
        b"2026-11-10T08:02:00Z ERROR db: reset status=500\n"
        b'Traceback (most recent call last):\n  File "x", line 1\nError: boom\n'
        b"2026-11-10T08:03:00Z INFO db: ok\n"
    )
    c = convert_log(raw)
    recs = [b for b in c.blocks if b.kind == "log_record"]
    assert len(recs) == 2
    assert b"Error: boom" in c.text.encode()[recs[0].start : recs[0].end]
    assert c.meta["parser"] == "generic"


@pytest.mark.parametrize(
    "lines,want",
    [
        (['{"a":1}', '{"b":2}'], "jsonl"),
        (['1.2.3.4 - - [10/Nov/2026:08:00:00 +0000] "GET / HTTP/1.1" 200 1 "-" "x"'], "access"),
        (["2026-11-10T08:00:00Z INFO x: y"], "generic"),
    ],
)
def test_detect(lines: list[str], want: str) -> None:
    assert detect_parser(lines) == want


def test_message_span_inside_record() -> None:
    c = convert_log(b"2026-11-10T08:00:01Z INFO payments.api: request ok status=200\n")
    b = c.blocks[0]
    msg = c.text.encode()[b.log["message_start"] : b.log["message_end"]].decode()
    assert msg.startswith("request ok")
