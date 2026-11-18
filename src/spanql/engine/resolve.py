"""Resolve handles and quotes to concrete spans (verifier check V2).

A formula cannot point at text that does not exist: ``$n`` must be an offered
candidate, and ``$n."quote"`` must match bytes inside that candidate exactly.
"""

from __future__ import annotations

import re
import unicodedata
from dataclasses import dataclass
from difflib import SequenceMatcher

from ..errors import ResolveError, SpanIntegrityError, StoreError
from ..formula.ast import Ambiguous, Answer, Formula, Ref, walk
from ..spans import Span
from ..store import Store


@dataclass(frozen=True, slots=True)
class HandleTable:
    """The candidate spans offered to the model: ``$1..$k``."""

    spans: dict[int, Span]

    @classmethod
    def from_ids(cls, store: Store, ids: dict[str | int, str]) -> "HandleTable":
        out: dict[int, Span] = {}
        for k, sid in ids.items():
            n = int(str(k).lstrip("$"))
            try:
                out[n] = store.span_from_id(sid)
            except (ValueError, StoreError) as e:
                raise ResolveError(f"handle ${n}: {e}") from e
        return cls(out)

    def to_ids(self) -> dict[str, str]:
        return {f"${n}": s.id for n, s in sorted(self.spans.items())}

    def describe_range(self) -> str:
        if not self.spans:
            return "no handles are available"
        ks = sorted(self.spans)
        return f"${ks[0]}..${ks[-1]}" if len(ks) > 1 else f"${ks[0]}"


def find_all(hay: bytes, needle: bytes) -> list[int]:
    """All occurrences, including overlapping ones (a quote must be unambiguous)."""
    out, pos = [], hay.find(needle)
    while pos != -1:
        out.append(pos)
        if len(out) > 1000:
            break
        pos = hay.find(needle, pos + 1)
    return out


def closest_text(text: str, quote: str, limit_tokens: int = 3000) -> str | None:
    """Best token-aligned window of ``text`` resembling ``quote`` (for retry hints)."""
    toks = [(m.start(), m.end()) for m in re.finditer(r"\S+", text)][:limit_tokens]
    if not toks or not quote.strip():
        return None
    k = max(1, len(quote.split()))
    best, best_ratio = None, 0.0
    q = quote.lower()
    for i in range(len(toks)):
        for kk in {max(1, k - 1), k, k + 1}:
            j = i + kk - 1
            if j >= len(toks):
                continue
            cand = text[toks[i][0] : toks[j][1]].strip(",.;:!?()[]\"'")  # sentence punctuation is not part of a value
            if not cand:
                continue
            r = SequenceMatcher(None, q, cand.lower()).ratio()
            if r > best_ratio:
                best, best_ratio = cand, r
    return best if best_ratio >= 0.6 else None


def resolve_ref(store: Store, ref: Ref, handles: HandleTable) -> Span:
    """Resolve one reference or raise :class:`ResolveError`."""
    span = handles.spans.get(ref.handle)
    if span is None:
        raise ResolveError(f"handle ${ref.handle} does not exist; available handles: {handles.describe_range()}",
                           pos=ref.pos)
    try:
        data = store.span_bytes(span)
    except SpanIntegrityError as e:
        raise ResolveError(str(e), pos=ref.pos) from e
    if ref.quote is None:
        return span
    quote = unicodedata.normalize("NFC", ref.quote)
    if not quote:
        raise ResolveError(f'empty quote in ${ref.handle}."" ', pos=ref.pos)
    needle = quote.encode("utf-8")
    hits = find_all(data, needle)
    shown = quote if len(quote) <= 60 else quote[:57] + "..."
    if not hits:
        close = closest_text(data.decode("utf-8", "replace"), quote)
        raise ResolveError(
            f'quote "{shown}" was not found in ${ref.handle}',
            hint=(f'closest text in ${ref.handle}: "{close}"' if close
                  else f"quote exactly as written in ${ref.handle}; copy the characters, do not retype numbers"),
            pos=ref.pos,
        )
    if ref.occurrence is None:
        if len(hits) > 1:
            raise ResolveError(
                f'quote "{shown}" occurs {len(hits)} times in ${ref.handle}',
                hint=f'add #1..#{len(hits)} to choose one (e.g. ${ref.handle}."{shown}"#1) or quote a longer phrase',
                pos=ref.pos,
            )
        idx = 0
    else:
        if ref.occurrence > len(hits):
            raise ResolveError(
                f'quote "{shown}" occurs only {len(hits)} time(s) in ${ref.handle}, not {ref.occurrence}', pos=ref.pos)
        idx = ref.occurrence - 1
    start = span.start + hits[idx]
    return store.make_span(span.snapshot_id, start, start + len(needle))


def resolve_refs(store: Store, formula: Formula, handles: HandleTable) -> dict[int, Span]:
    """Resolve every reference in the formula. Returns ``id(node) -> Span``.

    All problems are collected so a single retry can fix them all.
    """
    resolved: dict[int, Span] = {}
    problems: list[str] = []
    hints: list[str] = []
    first_pos = None

    def add(e: ResolveError) -> None:
        nonlocal first_pos
        problems.append(e.message)
        if e.hint:
            hints.append(e.hint)
        if first_pos is None:
            first_pos = e.pos

    if isinstance(formula, Answer):
        nodes = [n for a in formula.args for n in walk(a)]
    else:
        nodes = []
    for n in nodes:
        if isinstance(n, Ref):
            try:
                resolved[id(n)] = resolve_ref(store, n, handles)
            except ResolveError as e:
                add(e)
    if isinstance(formula, Ambiguous):  # shown to the user, so every handle must be real
        for h in formula.handles:
            if h not in handles.spans:
                problems.append(f"handle ${h} does not exist; available handles: {handles.describe_range()}")
    # NOT_FOUND's searched: list is metadata only: refusing is always safe, so unknown handles are dropped
    if problems:
        embedded = len(problems) == len(hints)  # hints folded into the problem lines; do not repeat them
        raise ResolveError(problems[0], hint=None if embedded else ("; ".join(hints) or None), pos=first_pos,
                           problems=_with_hints(problems, hints))
    return resolved


def _with_hints(problems: list[str], hints: list[str]) -> list[str]:
    # keep one line per problem; attach the matching hint when counts line up
    if len(problems) == len(hints):
        return [f"{p} (hint: {h})" for p, h in zip(problems, hints)]
    return problems
