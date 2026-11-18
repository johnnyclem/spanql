from __future__ import annotations

import pytest

from spanql.globs import match_uri


@pytest.mark.parametrize(
    "pattern, uri, expected",
    [
        ("logs/api/*.log", "logs/api/access.log", True),
        ("logs/*", "logs/api/access.log", True),  # * crosses directories, like SQLite GLOB
        ("logs/*.log", "logs/api/deep/x.log", True),
        ("logs/*.log", "logs/x.jsonl", False),
        ("repo/**/*.py", "repo/main.py", True),  # **/ matches zero directories
        ("repo/**/*.py", "repo/a/b/c.py", True),
        ("repo/**/*.py", "repo/a/b/c.js", False),
        ("repo/**", "repo/a/b", True),
        ("payments.log", "logs/payments.log", True),  # a bare name matches the basename
        ("logs/payments.log", "other/logs/payments.log", False),
        ("./logs/*", "logs/a.log", True),
        ("data/sale?.csv", "data/sales.csv", True),
        ("data/[st]ales.csv", "data/sales.csv", True),
        ("data/[!s]ales.csv", "data/sales.csv", False),
        ("a.b", "aXb", False),  # regex metacharacters are literal
        ("a+b", "a+b", True),
    ],
)
def test_glob_semantics(pattern, uri, expected):
    assert match_uri(pattern, uri) is expected
