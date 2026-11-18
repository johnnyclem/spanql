"""Shared fixtures: a small multi-format corpus under a temp dir."""

from __future__ import annotations

import textwrap
from pathlib import Path

import pytest

from spanql.ingest import ingest_path
from spanql.store import Store

APP_LOG = """\
2026-11-10 09:00:01,100 INFO  payments.api - request ok user=alice latency_ms=120
2026-11-10 09:00:02,200 ERROR payments.api - PaymentTimeout while charging card user=bob latency_ms=3000
Traceback (most recent call last):
  File "charge.py", line 10, in charge
TimeoutError: upstream timed out
2026-11-10 09:00:03,300 ERROR payments.api - PaymentTimeout while charging card user=carol latency_ms=3100
2026-11-10 09:00:04,400 WARN  payments.api - retrying charge user=carol
2026-11-10 09:00:05,500 ERROR payments.db - connection reset user=dave
2026-11-11 10:00:00,000 INFO  payments.api - request ok user=erin latency_ms=90
2026-11-11 10:00:01,000 ERROR payments.api - PaymentTimeout while charging card user=frank latency_ms=2900
"""

NGINX_LOG = """\
10.0.0.1 - - [16/Nov/2026:10:15:03 +0000] "GET /v1/items HTTP/1.1" 200 512 "-" "curl/8"
10.0.0.2 - - [16/Nov/2026:10:15:04 +0000] "POST /v1/items HTTP/1.1" 500 87 "-" "curl/8"
10.0.0.3 - - [16/Nov/2026:10:15:05 +0000] "GET /v1/items/9 HTTP/1.1" 404 12 "-" "curl/8"
10.0.0.4 - - [16/Nov/2026:10:15:06 +0000] "GET /v1/items HTTP/1.1" 503 0 "-" "curl/8"
10.0.0.5 - - [17/Nov/2026:08:00:00 +0000] "GET /v1/items HTTP/1.1" 502 0 "-" "curl/8"
"""

JSONL_LOG = (
    '{"ts":"2026-11-12T10:00:00Z","level":"info","msg":"started","service":"jobs"}\n'
    '{"ts":"2026-11-12T10:00:05Z","level":"error","msg":"job failed","service":"jobs","attempts":3}\n'
    '{"ts":"2026-11-12T10:00:09Z","level":"error","msg":"job failed","service":"jobs","attempts":5}\n'
)

Q3 = """\
# Q3 2026 Report

## Revenue

Total revenue for the quarter was $4.82 million, up from $4.31 million in Q2.

## Notes

- Net revenue excludes refunds and chargebacks.
- Headcount was 120 at the end of the quarter.

```
code: not prose
```
"""

Q2 = """\
# Q2 2026 Report

## Revenue

Q2 revenue: $4.31M

The invoice was issued on 2026-03-01 and paid on March 15, 2026.
"""

SALES = """\
region,product,amount,units
EMEA,widget,"1,200.50",10
EMEA,gadget,300,3
APAC,widget,950,8
APAC,gadget,"a ""quoted"" one",5
"""

ITEMS = '[{"id": 1, "name": "alpha", "price": 9.5, "tags": ["x"]},\n {"id": 2, "name": "beta", "price": 12}]\n'

RETRY_PY = '''\
"""Retry helpers."""
import time


def retry_with_backoff(fn, attempts=3):
    for i in range(attempts):
        try:
            return fn()
        except Exception:
            time.sleep(2 ** i)


class Client:
    def get(self, url):
        return retry_with_backoff(lambda: self._get(url))

    def _get(self, url):
        return url

    def post(self, url):
        return self._post(url)

    def _post(self, url):
        return url


def standalone():
    return retry_with_backoff(print)
'''

CONFIG_YAML = "server:\n  timeout: 30\n  retries: 5\nlogging:\n  level: info\n"


def write_corpus(root: Path) -> Path:
    files = {
        "logs/payments-2026-11-10.log": APP_LOG,
        "logs/api/access.log": NGINX_LOG,
        "logs/jobs.jsonl": JSONL_LOG,
        "reports/q3-2026.md": Q3,
        "reports/q2-2026.md": Q2,
        "data/sales.csv": SALES,
        "data/items.json": ITEMS,
        "repo/payments/retry.py": RETRY_PY,
        "repo/config.yaml": CONFIG_YAML,
    }
    for rel, text in files.items():
        p = root / rel
        p.parent.mkdir(parents=True, exist_ok=True)
        p.write_text(text, encoding="utf-8")
    return root


@pytest.fixture
def corpus(tmp_path) -> Path:
    return write_corpus(tmp_path / "corpus")


@pytest.fixture
def store() -> Store:
    s = Store(":memory:")
    yield s
    s.close()


@pytest.fixture
def ingested(corpus, store) -> Store:
    """Store with the corpus ingested; sources are named logs/..., reports/..., data/..., repo/...."""
    for sub in ("logs", "reports", "data", "repo"):
        ingest_path(store, str(corpus / sub))
    return store


def block_span(store: Store, uri: str, needle: str, kind: str | None = None):
    """Span of the first block of ``uri`` whose text contains ``needle`` (a stand-in for the retriever)."""
    src = store.get_source(uri)
    q = 'SELECT start, "end" FROM block WHERE snapshot_id=?' + (" AND kind=?" if kind else "") + " ORDER BY start"
    data = store.snapshot_bytes(src.current_snapshot)
    for r in store.db.execute(q, (src.current_snapshot, kind) if kind else (src.current_snapshot,)):
        if needle.encode() in data[r["start"] : r["end"]]:
            return store.make_span(src.current_snapshot, r["start"], r["end"])
    raise AssertionError(f"{needle!r} not found in a block of {uri}")
