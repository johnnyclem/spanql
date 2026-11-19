# Informal live smoke runs (not the M4 eval)

These are hand-checked runs on the demo corpus (`examples/demo`). They show the pipeline working against real models; they say nothing statistically meaningful about accuracy. The M4 bake-off (MODEL.md §5) is still to do.

Setup: llama.cpp `llama-server` b12528 (CPU, `-c 8192 --jinja -t 6`), Linux x86_64 box. GGUFs are from the pinned manifest revisions, with sha256 verified. Backend `llama.cpp` with per-request GBNF. The verifier as of commit `2124350`. All times are wall clock.

## Qwen3.5-4B-Instruct Q4_K_M (the proposed default)

| Question | Result | Correct? | Attempts | Time |
| --- | --- | --- | --- | --- |
| What was total revenue for the quarter in Q3? | $4,820,000 = `MONEY($2."$4.82 million")` | yes | 1 | 6 s |
| How much did revenue grow from Q2 to Q3? | $510,000 (11.8%) via DIFF/PCT | yes | 1 | 11 s |
| When was the Q3 report published? | 2026-11-03 | yes | 1 | 5 s |
| How many PaymentTimeout errors were logged on 2026-11-10? | 2 = `COUNT(FIND(..., day:"2026-11-10", match:"PaymentTimeout", level:"ERROR"))` | yes | 1 | 19 s |
| How many requests returned a 5xx status on 2026-11-10? | 3 = `COUNT(FIND(..., FIELD("status") >= 500))` | yes | 1 | 13 s |
| What is the total invoice amount for the West region? | 2,075.75 = `SUM(COLUMN(..., "amount", FIELD("region") == "West"))` | yes | 1 | 7 s |
| Which functions call retry_with_backoff? | PaymentsClient.charge, PaymentsClient.refund | yes | 1 | 8 s |
| What was our Q3 churn rate? | NOT_FOUND (model refusal) | yes | 1 | 2 s |
| Who is the CEO of the company? | NOT_FOUND (model refusal) | yes | 1 | 2 s |

The first round, before the prompt and verifier changes in `2124350`, got 4 of 7. The failures were all query-syntax mistakes: `CONTAINS(...)` used as a FIND filter, a literal `"header"` passed as the column name, and a `kind:"function|method"` alternation that wasn't supported. The verifier rejected each of them, so none produced a wrong answer; the questions were refused instead. Better feedback messages, a worked example per query type, and accepting `|` alternatives fixed all three.

## Qwen3.5-0.8B-Instruct Q4_K_M

The model follows the grammar and refuses invented quotes. It produced **wrong but grounded** answers, though:
- It counted `status >= 500` (3, which includes a DB error) for "PaymentTimeout errors".
- It used `COUNT` where `SUM` was needed ("2 dollars").
- In the Docker sidecar run, it answered a Q3 revenue question with the West-invoices sum and wrote "million" into the template. The template unit/scale check (V4) now rejects that kind of template. Picking the wrong source is V6's job (relevance check, off by default).

So 0.8B isn't a usable default. These runs are the reason the provenance UI and V6 exist.
