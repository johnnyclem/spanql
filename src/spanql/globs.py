"""Glob matching for source URIs.

Follows the SQLite ``GLOB`` semantics the spec compiles to (ARCHITECTURE.md section 3): ``*`` and
``?`` match across ``/``, so ``logs/*.log`` finds ``logs/api/access.log`` and a too-narrow glob cannot
silently undercount. ``**/`` additionally matches zero directories, so ``repo/**/*.py`` includes
``repo/main.py``.
"""

from __future__ import annotations

import re
from functools import lru_cache


@lru_cache(maxsize=512)
def glob_to_regex(pattern: str) -> re.Pattern[str]:
    p = pattern.strip()
    if p.startswith("./"):
        p = p[2:]
    out: list[str] = []
    i, n = 0, len(p)
    while i < n:
        c = p[i]
        if c == "*":
            if p[i : i + 2] == "**":
                i += 2
                if p[i : i + 1] == "/":
                    out.append("(?:.*/)?")
                    i += 1
                else:
                    out.append(".*")
            else:
                out.append(".*")
                i += 1
        elif c == "?":
            out.append(".")
            i += 1
        elif c == "[":
            j = p.find("]", i + 1)
            if j == -1:
                out.append(re.escape(c))
                i += 1
            else:
                body = p[i + 1 : j]
                if body.startswith("!"):
                    body = "^" + body[1:]
                out.append("[" + body.replace("\\", "\\\\") + "]")
                i = j + 1
        else:
            out.append(re.escape(c))
            i += 1
    return re.compile("".join(out) + r"\Z", re.DOTALL)


def match_uri(pattern: str, uri: str) -> bool:
    """True if ``uri`` matches ``pattern``.

    A pattern with no ``/`` also matches against the basename, like gitignore,
    so ``src:"payments.log"`` finds ``logs/payments.log``.
    """
    rx = glob_to_regex(pattern)
    if rx.match(uri):
        return True
    if "/" not in pattern.strip():
        return bool(rx.match(uri.rsplit("/", 1)[-1]))
    return False
