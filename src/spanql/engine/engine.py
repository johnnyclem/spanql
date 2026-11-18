"""Engine entry point: formula text -> parsed, typed, resolved, executed -> cells."""

from __future__ import annotations

import re
import secrets
import string
from dataclasses import dataclass, field
from datetime import datetime

from .. import ENGINE_VERSION
from ..formula.ast import Ambiguous, Answer, NotFound, Ref, walk
from ..formula.parser import parse_formula
from ..formula.typecheck import typecheck
from ..spans import Span
from ..store import Store
from .exec import Executor
from .format import render, to_json
from .resolve import HandleTable, resolve_refs
from .values import SpanSetV, Value
from ..errors import EmptySupportError

_ALPHABET = string.ascii_letters + string.digits
_PLACEHOLDER = re.compile(r"\{(\d+)\}")


def new_cell_id() -> str:
    return "c_" + "".join(secrets.choice(_ALPHABET) for _ in range(6))


@dataclass(slots=True)
class Cell:
    """One evaluated ANSWER argument: a value with its formula and provenance."""

    id: str
    index: int
    formula: str
    answer_formula: str
    value: Value
    display: str
    inputs: list[dict]
    steps: list[dict]
    handles: dict[str, str]  # handles used by this cell's formula
    evaluated_at: str
    answer_handles: dict[str, str] = field(default_factory=dict)  # every handle in the whole answer
    question_id: str | None = None
    template: str | None = None
    model: dict | None = None
    status: str = "grounded"
    store: Store | None = field(default=None, repr=False)

    def to_json(self) -> dict:
        return {
            "id": self.id, "question_id": self.question_id, "index": self.index, "formula": self.formula,
            "answer_formula": self.answer_formula, "template": self.template,
            "value": to_json(self.value, self.store), "type": self.value.type, "display": self.display,
            "status": self.status, "inputs": self.inputs, "steps": self.steps, "handles": self.handles,
            "answer_handles": self.answer_handles,
            "evaluated_at": self.evaluated_at, "engine_version": ENGINE_VERSION, "model": self.model,
        }


@dataclass(slots=True)
class Evaluation:
    kind: str  # answer | not_found | ambiguous
    formula: str
    cells: list[Cell] = field(default_factory=list)
    template: str | None = None
    message: str | None = None
    spans: list[Span] = field(default_factory=list)  # searched (not_found) or conflicting (ambiguous)
    snapshots: dict[str, str] = field(default_factory=dict)  # pinned snapshot set that was scanned

    def render(self) -> str:
        """Prose for an ANSWER: the template filled with cell displays (the model never writes values)."""
        if self.kind != "answer":
            return self.message or ""
        displays = [c.display for c in self.cells]
        if self.template is None:
            return ", ".join(displays)
        return _PLACEHOLDER.sub(lambda m: displays[int(m.group(1))], self.template)


def _now() -> str:
    return datetime.now().astimezone().isoformat(timespec="seconds")


class Engine:
    def __init__(self, store: Store, *, id_factory=new_cell_id, clock=_now):
        self.store = store
        self.new_id = id_factory
        self.clock = clock

    def load_cell(self, key: str) -> dict | None:
        return self.store.get_cell(key)

    def _cell_type(self, key):
        c = self.load_cell(key) if isinstance(key, str) else None
        return c["type"] if c else None

    def check(self, text: str):
        """Parse + type-check only (verifier V1). Returns (formula, arg types)."""
        f = parse_formula(text)
        return f, typecheck(f, cell_type=self._cell_type)

    def evaluate(
        self,
        text: str,
        handles: HandleTable,
        *,
        pins: dict[str, str] | None = None,
        question_id: str | None = None,
        model: dict | None = None,
        formula=None,
    ) -> Evaluation:
        """Evaluate ``text``. ``formula`` may be a pre-parsed, pre-checked AST for this same text.

        Raises a :class:`~spanql.errors.SpanqlError` subclass on any V1-V5 failure.
        """
        if formula is None:
            formula, _ = self.check(text)
        resolved = resolve_refs(self.store, formula, handles)

        if isinstance(formula, NotFound):
            return Evaluation("not_found", text, message=formula.message,
                              spans=[handles.spans[h] for h in formula.searched if h in handles.spans])
        if isinstance(formula, Ambiguous):
            return Evaluation("ambiguous", text, message=formula.message,
                              spans=[handles.spans[h] for h in formula.handles])

        assert isinstance(formula, Answer)
        ex = Executor(self.store, text, resolved, pins=pins, cell_loader=self.load_cell)
        cells: list[Cell] = []
        all_used = {f"${n.handle}": handles.spans[n.handle].id
                    for a in formula.args for n in walk(a) if isinstance(n, Ref) and n.handle in handles.spans}
        for i, arg in enumerate(formula.args):
            r = ex.eval(arg)
            if isinstance(r.value, SpanSetV) and r.value.total == 0:
                raise EmptySupportError(f"ANSWER value {i} ({text[arg.pos:arg.end]}) is an empty result",
                                        hint="the query matched nothing; check src:, filters and spelling")
            used = sorted({n.handle for n in walk(arg) if isinstance(n, Ref)})
            cell = Cell(
                id=self.new_id(), index=i, formula=text[arg.pos : arg.end], answer_formula=text, value=r.value,
                display=render(r.value, self.store), inputs=list(r.inputs), steps=list(r.steps),
                handles={f"${h}": handles.spans[h].id for h in used if h in handles.spans},
                answer_handles=all_used,
                evaluated_at=self.clock(), question_id=question_id, template=formula.template, model=model,
                store=self.store,
            )
            ex.prior.append((cell.id, r.value))
            cells.append(cell)
        return Evaluation("answer", text, cells=cells, template=formula.template, snapshots=dict(ex.snapshots_used))
