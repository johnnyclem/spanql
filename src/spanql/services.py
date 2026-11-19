"""Wiring: config → store → backend → harness, shared by the CLI, HTTP API and MCP server."""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any

from spanql import __version__
from spanql.config import Config
from spanql.engine.handles import HandleTable, handles_from_ids
from spanql.engine.run import Outcome, evaluate_formula
from spanql.harness.backends import Backend, make_backend
from spanql.harness.loop import AskResult, Harness
from spanql.ingest.pipeline import IngestReport, ingest_all
from spanql.models.manifest import installed
from spanql.retrieve.retriever import Retrieval
from spanql.store.db import Store
from spanql.store.reanchor import reanchor


@dataclass
class Services:
    config: Config
    store: Store
    backend: Backend
    harness: Harness
    conversations: dict[str, dict[str, Any]] = field(default_factory=dict)

    @classmethod
    def create(
        cls, config: Config, *, backend: Backend | None = None, store: Store | None = None
    ) -> Services:
        st = store or Store(config.store_path)
        be = backend or make_backend(config.model)
        return cls(config, st, be, Harness(st, be, config.harness))

    def close(self) -> None:
        self.store.close()

    # ---------------------------------------------------------------- ingest
    def ingest(self, sources: list[Any] | None = None) -> IngestReport:
        return ingest_all(self.store, sources if sources is not None else self.config.sources)

    # -------------------------------------------------------------------- ask
    def ask(
        self,
        question: str,
        *,
        scope: list[str] | None = None,
        conversation_id: str | None = None,
        on_event: Any = None,
    ) -> AskResult:
        conv = (
            self.conversations.setdefault(conversation_id, {"history": [], "cells": []})
            if conversation_id
            else None
        )
        res = self.harness.ask(
            question,
            scope=scope,
            on_event=on_event,
            history=conv["history"] if conv else None,
            prior_cells=conv["cells"][-8:] if conv else None,
        )
        if conv is not None:
            conv["history"] += [
                {"role": "user", "content": question},
                {"role": "assistant", "content": res.formula or ""},
            ]
            conv["cells"] += res.cells
        return res

    def search(self, query: str, *, scope: list[str] | None = None, k: int = 10) -> Retrieval:
        return self.harness.retriever.search(query, scope=scope, k=k)

    def evaluate(
        self,
        formula: str,
        handles: dict[str, str] | HandleTable | None = None,
        question: str | None = None,
        save: bool = True,
    ) -> Outcome:
        table: HandleTable
        if handles and all(isinstance(v, str) for v in handles.values()):
            table = handles_from_ids(self.store, handles)  # type: ignore[arg-type]
        else:
            table = handles or {}  # type: ignore[assignment]
        return evaluate_formula(self.store, formula, table, question=question, save=save)

    def recompute(self, cell_id: str) -> dict[str, Any]:
        cell = self.store.get_cell(cell_id)
        if cell is None:
            raise KeyError(cell_id)
        anchors = {h: reanchor(self.store, sid) for h, sid in cell.get("handles", {}).items()}
        stale = [h for h, a in anchors.items() if a.status in ("missing", "changed")]
        if stale:
            return {
                "status": "stale",
                "cell": cell_id,
                "reanchor": {h: a.to_dict() for h, a in anchors.items()},
                "message": f"handles {', '.join(stale)} no longer match the current sources",
            }
        new_handles = {h.lstrip("$"): a.new or cell["handles"][h] for h, a in anchors.items()}
        out = self.evaluate(
            cell["formula"]
            if cell["formula"].startswith(("ANSWER", "NOT_FOUND"))
            else f"ANSWER({cell['formula']})",
            new_handles,
            question=cell.get("question"),
        )
        return {
            "status": out.status,
            "previous": cell["value"],
            "cells": out.cells,
            "problems": [p.to_dict() for p in out.problems],
            "reanchor": {h: a.to_dict() for h, a in anchors.items()},
        }

    def staleness(self, cell: dict[str, Any]) -> dict[str, Any]:
        stale = []
        for inp in cell.get("inputs", []):
            if inp.get("kind") == "span":
                try:
                    rs = self.store.resolve(inp.get("consumed_span") or inp["id"])
                except Exception:
                    stale.append(inp["id"])
                    continue
                if not rs.is_current:
                    stale.append(inp["id"])
        return {"stale": bool(stale), "stale_inputs": stale}

    # ---------------------------------------------------------------- status
    def status(self) -> dict[str, Any]:
        return {
            "version": __version__,
            "store": {"path": str(self.config.store_path), **self.store.stats()},
            "model": {
                "backend": self.backend.name,
                "model": self.backend.model,
                "runtime": self.config.model.runtime,
                **self.backend.health(),
            },
            "models_installed": [m["alias"] for m in installed(self.config.models_dir)],
            "config": self.config.path,
        }
