"""Verifier check V6 (optional, model-assisted): is each cited span actually about what was asked?

V1-V5 guarantee a value was computed from real text; they cannot tell "Q3 revenue"
from "Q3 forecast". A short yes/no classification per cited span catches the most
common wrong-but-real citation. It reduces that risk; it does not remove it.
"""

from __future__ import annotations

from ..engine.engine import Evaluation
from ..engine.resolve import HandleTable
from ..errors import RelevanceError
from ..store import Store

MAX_CHECKED = 6
MAX_CHARS = 1500
YES_NO_GRAMMAR = 'root ::= "yes" | "no"\n'

PROMPT = (
    "Question: {question}\n\nText:\n\"\"\"\n{text}\n\"\"\"\n\n"
    "Does this text contain information that directly answers the question or is the exact figure it asks about "
    "(not a forecast, a different period, a different entity, or an unrelated topic)? Answer only yes or no."
)


class RelevanceChecker:
    def __init__(self, client, store: Store):
        self.client, self.store = client, store

    def check(self, question: str, ev: Evaluation, handles: HandleTable) -> None:
        """Raise :class:`RelevanceError` if a cited candidate looks off-topic."""
        cited: dict[str, str] = {}  # handle -> span id
        for c in ev.cells:
            cited.update(c.answer_handles)
        off_topic = []
        for handle, span_id in list(sorted(cited.items()))[:MAX_CHECKED]:
            span = self.store.span_from_id(span_id)
            # judge the whole candidate the model saw, not just the quoted fragment
            text = self.store.span_text(handles.spans.get(int(handle[1:]), span))[:MAX_CHARS]
            reply = self.client.complete(
                [{"role": "user", "content": PROMPT.format(question=question, text=text)}],
                grammar=YES_NO_GRAMMAR, max_tokens=3)
            if reply.strip().lower().startswith("no"):
                off_topic.append(handle)
        if off_topic:
            raise RelevanceError(
                f"the cited text {', '.join(off_topic)} does not appear to be about what the question asks",
                hint="choose a different candidate that states the requested figure for the requested "
                     "period/entity, or answer NOT_FOUND if none does")
