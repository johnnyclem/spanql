"""Prompts: a compact description of the DSL plus worked examples (MODEL.md section 4.1).

Constrained decoding limits what the model can *emit* but is not shown to it, so the
language still has to be described here.
"""

from __future__ import annotations

import json
from datetime import date

from ..retrieve.retriever import Retrieval

SYSTEM = """You write ONE formula in the spanql language that answers the user's question from their sources. \
You never write the final answer or any numbers yourself: an engine computes values from the text you point at.

OUTPUT: exactly one of
  ANSWER(expr, expr, ..., text:"prose with {0} {1} placeholders")
  NOT_FOUND("why the sources do not contain it", searched:[$1,$2])
  AMBIGUOUS("what conflicts", [$3,$5])

REFERENCES (the only way data enters a formula):
  $1                  a whole candidate passage
  $1."exact words"    exact characters copied from candidate $1 (add #2 for the 2nd occurrence)
  FIND(src:"glob", kind:"log|doc|file|code", re:"regex", contains:"text", day:"2026-11-10",
       since:"..", until:"..", level:"ERROR", logger:"x", FIELD("status") >= 500)   matching records
  COLUMN("header", src:"glob", FIELD("other") == "x")   cells of a table column   SYMBOLS(src:"glob", kind:"function|method|class|module", name:"f", calls:"g")
  LINES(src:"file", from:10, to:20)    [0] = an earlier value of this ANSWER, [c_ab12] = a stored cell

FUNCTIONS: extract NUM MONEY PCT_OF DATE DATETIME DURATION TEXT BOOL (read a span; they refuse to guess) | \
COUNT SUM AVG MIN MAX DISTINCT (SUM/AVG/MIN/MAX accept as:"num|money|pct|date|datetime|duration" over FIND/COLUMN) | \
ROUND(x, places:2) PCT(a,b,places:1) DIFF(a,b) RATIO(a,b) ABS | DAYS_BETWEEN BEFORE AFTER | \
IF AND OR NOT EQ NE GT GTE LT LTE CONTAINS. Infix + - * / and == != > >= < <= also work.

RULES
- Copy quotes exactly from the candidate text; never retype or round a number you read.
- Pick the candidate that states the requested figure for the requested period/entity.
- Numbers you type must be in the question, a query filter, or places:. Put no digits in text: except labels like Q3.
- Use COUNT(FIND(...)) / SUM(COLUMN(...)) for totals over many records instead of listing them.
- If no candidate or query can answer it, write NOT_FOUND. Do not guess. If sources conflict, write AMBIGUOUS.

EXAMPLES
Q: How much did revenue grow from Q2 to Q3?   (candidates: $1 Q3 report text, $2 Q2 report text)
ANSWER(DIFF(MONEY($1."$4.82 million"), MONEY($2."$4.31M")), PCT(DIFF(MONEY($1."$4.82 million"), MONEY($2."$4.31M")), MONEY($2."$4.31M"), places:1), text:"Revenue grew by {0} ({1}) from Q2 to Q3.")

Q: What was Q3 revenue?
ANSWER(MONEY($1."$4.82 million"), text:"Q3 revenue was {0}.")

Q: How many payment timeouts were logged on 2026-11-10, and what share of all errors is that?
ANSWER(COUNT(FIND(src:"logs/payments-*.log", re:"PaymentTimeout", day:"2026-11-10")), COUNT(FIND(src:"logs/payments-*.log", level:"ERROR", day:"2026-11-10")), PCT([0], [1], places:1), text:"There were {0} payment timeouts out of {1} errors, which is {2}.")

Q: How many 5xx responses did the API return on 2026-11-16?
ANSWER(COUNT(FIND(src:"logs/api/*.log", day:"2026-11-16", FIELD("status") >= 500)), text:"The API returned {0} 5xx responses.")

Q: Which functions call retry_with_backoff?
ANSWER(COUNT(DISTINCT(SYMBOLS(src:"repo/**/*.py", kind:"function", calls:"retry_with_backoff"))), DISTINCT(SYMBOLS(src:"repo/**/*.py", kind:"function", calls:"retry_with_backoff")), text:"{0} functions call it: {1}.")

Q: What is the total number of units sold in EMEA?
ANSWER(SUM(COLUMN("units", src:"data/sales.csv", FIELD("region") == "EMEA")), text:"EMEA sold {0} units.")

Q: How many days passed between the invoice and the payment?   ($3 = "issued on 2026-03-01 and paid on March 15, 2026")
ANSWER(DAYS_BETWEEN(DATE($3."2026-03-01"), DATE($3."March 15, 2026")), text:"{0} days passed.")

Q: When was the last error logged?
ANSWER(MAX(FIND(src:"logs/payments-*.log", level:"ERROR"), as:"datetime"), text:"The last error was logged at {0}.")

Q: What was Q3 churn?   (no candidate mentions churn or retention)
NOT_FOUND("No churn or retention figure appears in the candidates.", searched:[$1,$2,$3])

Q: What is the timeout?   ($4 says 30 seconds, $7 says 45 seconds, for different services)
AMBIGUOUS("Two different timeouts are configured.", [$4, $7])
"""

# Worked examples above that need no corpus to be valid; tests parse and type-check every one.
EXAMPLE_FORMULAS = [
    'ANSWER(DIFF(MONEY($1."$4.82 million"), MONEY($2."$4.31M")), PCT(DIFF(MONEY($1."$4.82 million"), MONEY($2."$4.31M")), MONEY($2."$4.31M"), places:1), text:"Revenue grew by {0} ({1}) from Q2 to Q3.")',
    'ANSWER(MONEY($1."$4.82 million"), text:"Q3 revenue was {0}.")',
    'ANSWER(COUNT(FIND(src:"logs/payments-*.log", re:"PaymentTimeout", day:"2026-11-10")), COUNT(FIND(src:"logs/payments-*.log", level:"ERROR", day:"2026-11-10")), PCT([0], [1], places:1), text:"There were {0} payment timeouts out of {1} errors, which is {2}.")',
    'ANSWER(COUNT(FIND(src:"logs/api/*.log", day:"2026-11-16", FIELD("status") >= 500)), text:"The API returned {0} 5xx responses.")',
    'ANSWER(COUNT(DISTINCT(SYMBOLS(src:"repo/**/*.py", kind:"function", calls:"retry_with_backoff"))), DISTINCT(SYMBOLS(src:"repo/**/*.py", kind:"function", calls:"retry_with_backoff")), text:"{0} functions call it: {1}.")',
    'ANSWER(SUM(COLUMN("units", src:"data/sales.csv", FIELD("region") == "EMEA")), text:"EMEA sold {0} units.")',
    'ANSWER(DAYS_BETWEEN(DATE($3."2026-03-01"), DATE($3."March 15, 2026")), text:"{0} days passed.")',
    'ANSWER(MAX(FIND(src:"logs/payments-*.log", level:"ERROR"), as:"datetime"), text:"The last error was logged at {0}.")',
    'NOT_FOUND("No churn or retention figure appears in the candidates.", searched:[$1,$2,$3])',
    'AMBIGUOUS("Two different timeouts are configured.", [$4, $7])',
]


def _fmt_schema(d: dict) -> str:
    bits = [f"{d['uri']} [{d['kind']}]"]
    if "records" in d:
        bits.append(f"{d['records']} records")
    if "rows" in d:
        bits.append(f"{d['rows']} rows")
    if d.get("time_range"):
        bits.append(f"days {d['time_range'][0]}..{d['time_range'][1]}")
    if d.get("levels"):
        bits.append("levels " + " ".join(f"{k}:{v}" for k, v in d["levels"].items()))
    for key, label in (("fields", "fields"), ("columns", "columns")):
        if d.get(key):
            bits.append(label + ": " + ", ".join(f"{k}={json.dumps(v)}" for k, v in list(d[key].items())[:12]))
    if d.get("symbols"):
        bits.append("symbols " + " ".join(f"{k}:{v}" for k, v in d["symbols"].items()))
    return " · ".join(bits)


def build_user_prompt(question: str, retrieval: Retrieval, today: date) -> str:
    lines = [f"Today is {today.isoformat()} ({today.strftime('%A')})."]
    if retrieval.date_hints:
        lines.append("Date hints: " + "; ".join(f'"{k}" = {v}' for k, v in retrieval.date_hints.items()))
    if retrieval.catalog:
        lines.append("\nSources (use these paths in src: globs):")
        schema_uris = {d["uri"] for d in retrieval.schemas}
        for d in retrieval.catalog:
            if d["uri"] not in schema_uris:
                lines.append(f"- {d['uri']} [{d['kind']}]")
        for d in retrieval.schemas:
            lines.append("- " + _fmt_schema(d))
    lines.append("\nCandidates:" if retrieval.candidates else "\nCandidates: (none retrieved)")
    for c in retrieval.candidates:
        where = c.location() + (f" · {c.path}" if c.path else "")
        lines.append(f'[${c.handle}] {c.uri} · {where} · "{c.excerpt}"')
    lines.append(f"\nQuestion: {question}\n\nWrite the formula.")
    return "\n".join(lines)


def retry_message(feedback: str) -> str:
    return ("That formula failed verification:\n" + feedback +
            "\n\nWrite a corrected formula (only the formula). If the sources cannot support an answer, write NOT_FOUND.")
