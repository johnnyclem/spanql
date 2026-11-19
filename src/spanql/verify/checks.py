"""Static verifier checks that don't need evaluation (V4 literals, template digits)."""

from __future__ import annotations

import re

from spanql.engine.errors import Problem
from spanql.formula.ast import (
    Answer,
    BinOp,
    Call,
    Compare,
    Formula,
    Neg,
    Node,
    NumberLit,
)
from spanql.formula.spec import OBVIOUS_CONSTANTS, QUERY_FUNCTIONS

_PLACEHOLDER = re.compile(r"\{(\d+)\}")
_COUNT_Q = re.compile(r"(?i)\b(how many|how often|count|number of|times|occurrences|instances)\b")


def is_count_question(question: str | None) -> bool:
    return question is None or bool(_COUNT_Q.search(question))


def check_literals(f: Formula) -> tuple[list[Problem], list[Problem]]:
    """V4: numeric literals may appear only as formatting/config parameters (keyword args,
    ROUND's places), inside query filters, or as obvious constants (which are flagged)."""
    errors: list[Problem] = []
    warnings: list[Problem] = []
    if not isinstance(f, Answer):
        return errors, warnings

    def visit(node: Node, allowed: bool) -> None:
        match node:
            case NumberLit(text=t):
                if allowed:
                    return
                if t.lstrip("-") in OBVIOUS_CONSTANTS:
                    warnings.append(
                        Problem(
                            "V4",
                            f"constant {t} is not from your sources",
                            "flagged as an obvious constant",
                            node.pos,
                        )
                    )
                else:
                    errors.append(
                        Problem(
                            "V4",
                            f"numeric literal {t} is not from your sources",
                            "every value must come from a $n quote or a FIND(...) query",
                            node.pos,
                        )
                    )
            case Call(name=name, args=args, kwargs=kwargs):
                in_query = name in QUERY_FUNCTIONS or name == "FIELD"
                for i, a in enumerate(args):
                    visit(a, allowed or in_query or (name == "ROUND" and i == 1))
                for _, v in kwargs:
                    visit(v, True)
            case BinOp(left=left, right=right) | Compare(left=left, right=right):
                visit(left, allowed)
                visit(right, allowed)
            case Neg(operand=o):
                visit(o, allowed)

    for e in f.exprs:
        if isinstance(e, NumberLit):
            errors.append(
                Problem(
                    "V4",
                    f"answer {e.text} is a bare literal",
                    "answers must be computed from your sources",
                    e.pos,
                )
            )
            continue
        visit(e, False)
    return errors, warnings


# values are rendered with their own unit/currency/scale; prose must not add another one
_UNIT_AFTER = re.compile(
    r"\{\d+\}\s*(million|billion|thousand|trillion|percent|per cent|%|dollars?|euros?|pounds?"
    r"|USD|EUR|GBP|k|K|M|MM|bn|B)(?![A-Za-z])"
)
_CURRENCY_BEFORE = re.compile(r"[$€£¥₹]\s*\{\d+\}")
_QNUM = re.compile(r"\d[\d,.:/\-]*\d|\d")


def check_template(f: Formula, question: str | None = None) -> list[Problem]:
    """Template may only contain prose and {n} placeholders; digits outside placeholders are
    ungrounded values (V4). Numbers copied verbatim from the user's question (a date, an ID,
    "top 5") are the user's own words, not values, so they are allowed."""
    if not isinstance(f, Answer) or f.template is None:
        return []
    problems: list[Problem] = []
    for m in _PLACEHOLDER.finditer(f.template):
        if int(m.group(1)) >= len(f.exprs):
            problems.append(
                Problem(
                    "V1",
                    f"template placeholder {{{m.group(1)}}} has no value",
                    f"use {{0}}..{{{len(f.exprs) - 1}}}",
                    f.pos,
                )
            )
    for rx, what in (
        (_UNIT_AFTER, "a unit or scale word after"),
        (_CURRENCY_BEFORE, "a currency symbol before"),
    ):
        if um := rx.search(f.template):
            problems.append(
                Problem(
                    "V4",
                    f"template puts {what} a placeholder ({um.group(0)!r})",
                    "values are displayed with their own unit and scale; write just {n}",
                    f.pos,
                )
            )
    rest = _PLACEHOLDER.sub("", f.template)
    if question:
        for tok in sorted(set(_QNUM.findall(question)), key=len, reverse=True):
            rest = re.sub(rf"(?<![\d.,]){re.escape(tok)}(?![\d])", " ", rest)
    # identifiers like Q3, H1, v2, 5xx, S3 are prose; free-standing numbers are values
    digits = [d.rstrip(".,") for d in re.findall(r"(?<![A-Za-z\d_])\d[\d,.]*(?![A-Za-z_\d])", rest)]
    if digits:
        problems.append(
            Problem(
                "V4",
                f"template contains numbers outside placeholders: {', '.join(digits[:3])}",
                "put every value in a {n} placeholder computed by the formula",
                f.pos,
            )
        )
    return problems
