"""Structured engine/verifier errors. Codes follow docs/ARCHITECTURE.md §5 (V1-V6)."""

from __future__ import annotations

from dataclasses import dataclass
from typing import Any


@dataclass
class Problem:
    code: str  # V1..V6
    message: str
    hint: str | None = None
    pos: int | None = None

    def to_dict(self) -> dict[str, Any]:
        return {"code": self.code, "message": self.message, "hint": self.hint, "pos": self.pos}

    def __str__(self) -> str:
        return f"[{self.code}] {self.message}" + (f" (hint: {self.hint})" if self.hint else "")


class EngineError(Exception):
    """Raised during type checking / evaluation; carries a verifier code."""

    def __init__(self, code: str, message: str, hint: str | None = None, pos: int | None = None):
        super().__init__(message)
        self.problem = Problem(code, message, hint, pos)


def syntax(msg: str, hint: str | None = None, pos: int | None = None) -> EngineError:
    return EngineError("V1", msg, hint, pos)


def citation(msg: str, hint: str | None = None, pos: int | None = None) -> EngineError:
    return EngineError("V2", msg, hint, pos)


def extraction(msg: str, hint: str | None = None, pos: int | None = None) -> EngineError:
    return EngineError("V3", msg, hint, pos)


def literal(msg: str, hint: str | None = None, pos: int | None = None) -> EngineError:
    return EngineError("V4", msg, hint, pos)


def support(msg: str, hint: str | None = None, pos: int | None = None) -> EngineError:
    return EngineError("V5", msg, hint, pos)
