"""Strict number parsing for SQL-side comparisons (``spanql_num``)."""

from __future__ import annotations

import re
from decimal import Decimal, InvalidOperation

_NUM = re.compile(r"^[+-]?(?:\d{1,3}(?:,\d{3})+|\d+)(?:\.\d+)?$")


def sql_number(v) -> float | None:
    """Return ``v`` as a float if it is cleanly numeric, else None (never guesses)."""
    if v is None or isinstance(v, bool):
        return None if v is None else float(v)
    if isinstance(v, (int, float)):
        return float(v)
    if isinstance(v, (bytes, bytearray)):
        return None
    s = str(v).strip()
    if not _NUM.match(s):
        return None
    try:
        return float(Decimal(s.replace(",", "")))
    except InvalidOperation:
        return None
