"""Source globs for ``src:"…"``: ``*`` stays within a path segment, ``**`` crosses segments.

A pattern matches a source URI ``<root>/<path>`` if it matches the whole URI, the path without
the root name, or (for patterns without ``/``) the file name.
"""

from __future__ import annotations

import re
from functools import lru_cache


@lru_cache(maxsize=512)
def glob_regex(pattern: str) -> re.Pattern[str]:
    i, n, out = 0, len(pattern), []
    while i < n:
        c = pattern[i]
        if c == "*":
            if pattern[i : i + 2] == "**":
                if pattern[i + 2 : i + 3] == "/":
                    out.append("(?:.*/)?")
                    i += 3
                else:
                    out.append(".*")
                    i += 2
                continue
            out.append("[^/]*")
        elif c == "?":
            out.append("[^/]")
        elif c == "[":
            j = pattern.find("]", i + 1)
            if j == -1:
                out.append(re.escape(c))
            else:
                body = pattern[i + 1 : j]
                if body.startswith("!"):
                    body = "^" + body[1:]
                out.append(f"[{body}]")
                i = j
        elif c == "{":
            j = pattern.find("}", i + 1)
            if j == -1:
                out.append(re.escape(c))
            else:
                alts = pattern[i + 1 : j].split(",")
                out.append("(?:" + "|".join(re.escape(a) for a in alts) + ")")
                i = j
        else:
            out.append(re.escape(c))
        i += 1
    return re.compile("".join(out) + r"\Z")


def glob_match(pattern: str | None, uri: str | None) -> int:
    if not pattern or uri is None:
        return 0 if pattern else 1
    rx = glob_regex(pattern.removeprefix("./"))
    if rx.match(uri):
        return 1
    if "/" in uri:
        rest = uri.split("/", 1)[1]
        if rx.match(rest):
            return 1
    if "/" not in pattern and rx.match(uri.rsplit("/", 1)[-1]):
        return 1
    return 0
