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
from spanql.formula.spec import QUERY_FUNCTIONS

_PLACEHOLDER = re.compile(r"\{(\d+)\}")
_COUNT_Q = re.compile(r"(?i)\b(how many|how often|count|number of|times|occurrences|instances)\b")


def is_count_question(question: str | None) -> bool:
    return question is None or bool(_COUNT_Q.search(question))


# a number standing on its own: "3 times", "1,000", "2.5"; not the 3 in "Q3" or "v3"
_QTOKEN = re.compile(r"(?<![A-Za-z_\d.,])\d[\d,]*(?:\.\d+)?(?![A-Za-z_\d])")


def question_numbers(question: str | None) -> frozenset[str]:
    """Numbers the user typed in the question, normalized (``1,000`` → ``1000``, ``3.0`` → ``3``).

    A number the user wrote is the user's own input ("what is 3 times …", "top 5"), not a value
    the model made up, so V4 lets the formula use it."""
    if not question:
        return frozenset()
    return frozenset(_norm_num(m.group(0)) for m in _QTOKEN.finditer(question))


def _norm_num(text: str) -> str:
    t = text.replace(",", "").lstrip("-")
    if "." in t:
        t = t.rstrip("0").rstrip(".")
    return t.lstrip("0") or "0"


def check_literals(f: Formula, question: str | None = None) -> tuple[list[Problem], list[Problem]]:
    """V4: a numeric literal is allowed only as a formatting/config parameter (keyword args,
    ROUND's places), inside a query filter, or when the user typed that number in the question.
    Anything else is an ungrounded value and is rejected; there is no "obvious constant"
    allowance (``MONEY($1) * 2`` would otherwise produce a grounded-looking value)."""
    errors: list[Problem] = []
    warnings: list[Problem] = []
    if not isinstance(f, Answer):
        return errors, warnings
    from_question = question_numbers(question)

    def visit(node: Node, allowed: bool) -> None:
        match node:
            case NumberLit(text=t):
                if allowed:
                    return
                if _norm_num(t) in from_question:
                    warnings.append(
                        Problem(
                            "V4",
                            f"number {t} is taken from the question",
                            "allowed because the user typed it",
                            node.pos,
                        )
                    )
                    return
                errors.append(
                    Problem(
                        "V4",
                        f"numeric literal {t} is not from your sources or the question",
                        "every value must come from a $n quote or a FIND(...) query; "
                        "only formatting (places:), query filters and numbers typed in the "
                        "question may be written as literals",
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
