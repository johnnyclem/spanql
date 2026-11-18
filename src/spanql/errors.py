"""Error types. Every formula-level error maps to a verifier check (V1-V6) and
knows how to describe itself as retry feedback for the model."""

from __future__ import annotations


class SpanqlError(Exception):
    """Base class for formula/verification failures (not infrastructure errors)."""

    check = "V1"
    code = "error"

    def __init__(
        self,
        message: str,
        *,
        hint: str | None = None,
        pos: int | None = None,
        problems: list[str] | None = None,
    ):
        super().__init__(message)
        self.message = message
        self.hint = hint
        self.pos = pos
        self.problems = problems or [message]

    def feedback(self) -> str:
        """Text fed back to the model on a retry."""
        lines = [f"[{self.check}] {p}" for p in self.problems]
        if self.hint:
            lines.append(f"hint: {self.hint}")
        return "\n".join(lines)

    def to_json(self) -> dict:
        return {
            "check": self.check,
            "code": self.code,
            "message": self.message,
            "problems": self.problems,
            "hint": self.hint,
            "pos": self.pos,
        }


class ParseError(SpanqlError):
    check, code = "V1", "parse"


class FormulaTypeError(SpanqlError):
    check, code = "V1", "type"


class EvalError(SpanqlError):
    """Arithmetic/unit/domain errors raised while evaluating."""

    check, code = "V1", "eval"


class ResolveError(SpanqlError):
    check, code = "V2", "resolve"


class SpanIntegrityError(ResolveError):
    code = "integrity"


class ExtractError(SpanqlError):
    check, code = "V3", "extract"


class LiteralError(SpanqlError):
    check, code = "V4", "literal"


class EmptySupportError(SpanqlError):
    check, code = "V5", "empty_support"


class RelevanceError(SpanqlError):
    check, code = "V6", "relevance"


class StoreError(Exception):
    """Infrastructure problem (bad span id, unknown source, corrupt store)."""
