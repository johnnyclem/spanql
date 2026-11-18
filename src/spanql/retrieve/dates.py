"""Resolve relative dates in a question ("yesterday", "last Tuesday") against a reference day.

The model is told these resolutions; it is far less error-prone than asking a small
model to do calendar arithmetic. ``today`` is injected so results are reproducible.
"""

from __future__ import annotations

import re
from datetime import date, timedelta

WEEKDAYS = ["monday", "tuesday", "wednesday", "thursday", "friday", "saturday", "sunday"]


def date_hints(question: str, today: date) -> dict[str, str]:
    """Phrase -> ISO date for the relative dates we can resolve unambiguously."""
    q = question.lower()
    hints: dict[str, str] = {}
    if re.search(r"\btoday\b", q):
        hints["today"] = today.isoformat()
    if re.search(r"\byesterday\b", q):
        hints["yesterday"] = (today - timedelta(days=1)).isoformat()
    for m in re.finditer(r"\blast (monday|tuesday|wednesday|thursday|friday|saturday|sunday)\b", q):
        target = WEEKDAYS.index(m.group(1))
        back = (today.weekday() - target) % 7 or 7  # "last X" is strictly before today
        hints[m.group(0)] = (today - timedelta(days=back)).isoformat()
    for m in re.finditer(r"\b(\d{4}-\d{2}-\d{2})\b", question):
        hints.setdefault(m.group(1), m.group(1))
    return hints
