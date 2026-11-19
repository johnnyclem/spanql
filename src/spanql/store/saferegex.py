"""Regex matching that cannot hang the engine.

Model-written ``re:`` patterns run inside a SQLite callback (``FIND``) or a line scan
(``LINES``). Python's ``re`` has no timeout, so one catastrophic pattern could pin a worker
forever. Three layers:

1. :func:`check_pattern` rejects invalid patterns and the classic exponential family, nested
   unbounded repetition such as ``(a+)+``, up front with a hint the model can act on.
2. Matching uses the third-party ``regex`` module (VERSION0, ``re``-compatible syntax), whose
   ``search`` accepts a ``timeout``.
3. Every query runs under a per-thread deadline (:func:`deadline`). Each match gets only the
   time left, and :class:`RegexTimeout` is raised once it is spent. The store's SQLite progress
   handler also aborts long SQL when the deadline passes.
"""

from __future__ import annotations

import re
import threading
import time
from collections.abc import Iterator
from contextlib import contextmanager
from functools import lru_cache
from typing import Any

import regex

DEFAULT_TIMEOUT_S = 10.0


class RegexTimeout(Exception):
    """A query's time budget ran out while matching a regular expression."""


class UnsafePattern(ValueError):
    def __init__(self, message: str, hint: str):
        super().__init__(message)
        self.hint = hint


_local = threading.local()


def _get_deadline() -> float | None:
    return getattr(_local, "deadline", None)


def expired() -> bool:
    d = _get_deadline()
    return d is not None and time.monotonic() > d


def timed_out() -> bool:
    """True if a regex timed out inside the current deadline block (even if swallowed)."""
    return bool(getattr(_local, "timed_out", False))


@contextmanager
def deadline(seconds: float | None) -> Iterator[None]:
    """Run the body under a time budget (nested blocks keep the earlier deadline)."""
    prev = _get_deadline()
    prev_flag = timed_out()
    new = None if seconds is None else time.monotonic() + seconds
    _local.deadline = new if prev is None else (prev if new is None else min(prev, new))
    _local.timed_out = False
    try:
        yield
    finally:
        _local.deadline = prev
        _local.timed_out = prev_flag


@lru_cache(maxsize=512)
def compile_pattern(pattern: str) -> Any:
    return regex.compile(pattern, flags=regex.VERSION0)


def search(pattern: str, text: str) -> bool:
    """``True`` if ``pattern`` matches somewhere in ``text``, within the current deadline."""
    d = _get_deadline()
    if d is None:
        d = time.monotonic() + DEFAULT_TIMEOUT_S
    left = d - time.monotonic()
    if left <= 0:
        _local.timed_out = True
        raise RegexTimeout(pattern)
    try:
        return compile_pattern(pattern).search(text, timeout=left) is not None
    except TimeoutError as exc:
        _local.timed_out = True
        raise RegexTimeout(pattern) from exc


def check_pattern(pattern: str) -> None:
    """Raise :class:`UnsafePattern` for an invalid or structurally exponential pattern."""
    try:
        compile_pattern(pattern)
    except regex.error as exc:
        raise UnsafePattern(
            f"invalid regex {pattern!r}: {exc}",
            "Python regex syntax; escape backslashes inside the quoted string",
        ) from exc
    try:
        from re import _constants as C  # type: ignore[attr-defined]
        from re import _parser as P  # type: ignore[attr-defined]
    except ImportError:  # pragma: no cover - stdlib internals moved; the timeout still applies
        return
    try:
        tree = P.parse(pattern)
    except (re.error, Exception):  # valid for `regex` but not `re`: rely on the timeout
        return

    def children(op: Any, av: Any) -> list[Any]:
        if op in (C.MAX_REPEAT, C.MIN_REPEAT, getattr(C, "POSSESSIVE_REPEAT", None)):
            return [av[2]]
        if op == C.SUBPATTERN:
            return [av[3]]
        if op == C.BRANCH:
            return list(av[1])
        if op in (C.ASSERT, C.ASSERT_NOT):
            return [av[1]]
        if op == getattr(C, "ATOMIC_GROUP", None):
            return [av]
        return []

    def unbounded(op: Any, av: Any) -> bool:
        return op in (C.MAX_REPEAT, C.MIN_REPEAT) and av[1] == C.MAXREPEAT

    def has_unbounded(sub: Any) -> bool:
        return any(
            unbounded(op, av) or any(has_unbounded(x) for x in children(op, av)) for op, av in sub
        )

    def risky(sub: Any) -> bool:
        for op, av in sub:
            if unbounded(op, av) and has_unbounded(av[2]):
                return True
            if any(risky(x) for x in children(op, av)):
                return True
        return False

    if risky(tree):
        raise UnsafePattern(
            f"regex {pattern!r} repeats a repeated group (like (a+)+), which can take "
            "exponential time",
            'simplify it: drop the outer repeat, bound it like {1,5}, or use match:"..."',
        )
