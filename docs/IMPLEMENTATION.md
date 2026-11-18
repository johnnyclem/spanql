# Implementation status and decisions

This is the first code against the design docs. It implements the deterministic core end to end (roadmap **M1**, **M2**, and the model-independent half of **M3**) plus a CLI and a small HTTP API. It does **not** include anything that needs a real model, GPU, UI work, packaging, or your answers to the open questions in the [roadmap](ROADMAP.md).

The other docs remain the design. Where the code differs from them, or where they were inconsistent or silent, the decision is recorded here.

## Status by milestone

| # | Milestone | Status | Notes |
| --- | --- | --- | --- |
| M0 | Design sign-off | **open** | Needs your decision. ADRs are still *Proposed*; license not chosen (no `LICENSE` file, no `license` in `pyproject.toml`). |
| M1 | Span store + ingestion | **done**, except tree-sitter | Text, Markdown, CSV/TSV, JSON tables, JSONL/nginx/ISO/syslog/custom-regex logs, git repos, Python symbols (stdlib `ast`). Snapshots, stable spans, FTS5, re-anchoring, GC. Exit criteria are tests: unchanged re-ingest is a no-op, every block round-trips byte-exact, re-anchoring survives edits. |
| M2 | Formula language + engine | **done** | Grammar, parser, type checker, extractors, planner → parameterized SQL, cells with traces, `evaluate`, `trace`, recompute. Determinism is checked by a randomized test (same results across runs, row ids and ingestion order). |
| M3 | Verifier + harness | **partly done** | Done: V1–V5, optional V6, retrieval, candidate packing, per-request GBNF, OpenAI-compatible client, retry loop, `NOT_FOUND`/`AMBIGUOUS`, run records, CLI `ask`. **Not done:** Qwen3.5-4B baseline numbers and `eval/RESULTS.md`; that needs a model and an eval set (M4). |
| M4 | Eval set + bake-off | not started | |
| M5 | Chat UI + API + provenance panel | **API only** | Sources, ingest jobs, ask, evaluate, cells, trace, recompute, runs, spans. No UI, SSE, OpenAI shim, `/metrics`. |
| M6 | Packaging | not started | Docker/Compose/Homebrew files are untested here, so none were written. The core has zero third-party dependencies, which makes the Homebrew formula resource-free. |
| M7 | Fine-tune v1 | not started | |
| M8 | Rich documents + integrations | not started | PDF/DOCX/PPTX/HTML are skipped with a clear reason; no MCP server; no concussion-protocol gate. |

## Run it

```sh
pip install -e .            # Python 3.12+, no third-party dependencies
spanql selftest             # ingests a tiny corpus and evaluates formulas: prints "ok"

spanql ingest ./docs ./logs ./repo
spanql sources
spanql evaluate 'COUNT(FIND(src:"logs/*.log", level:"ERROR", day:"2026-11-10"))'
spanql trace c_AbC123       # formula, steps, exact source characters consumed, staleness

# asking questions needs a model endpoint (see "Model" below)
spanql ask "How many payment timeouts did we log on 2026-11-10?"
spanql serve                # HTTP API on 127.0.0.1:7717
```

Tests: `pip install pytest && pytest` (278 tests, ~12 s).

### Model

`spanql ask` talks to any OpenAI-compatible chat endpoint. The supported, designed-for setup is a llama.cpp server, which receives the per-request GBNF grammar in the `grammar` field:

```sh
llama-server -m qwen3.5-4b-instruct-q4_k_m.gguf -c 32768 --port 7718
SPANQL_MODEL__ENDPOINT=http://127.0.0.1:7718/v1 spanql ask "..."
```

Other backends set `backend = "openai-compatible"` and work without grammar constraints, at the cost of more verifier retries. **No model has been run against this code.** Prompts, retrieval and the retry loop are tested with a scripted stand-in model (`ScriptedClient`); whether a 4B model can actually write these formulas is exactly what M3/M4 measure. See `config.example.toml` for all options.

## Decisions where the docs were inconsistent or silent

**Formula language**

- **Comparison operators.** The EBNF has only `+ - * /`, but the spec's own example is `FIND(..., FIELD("status") >= 500)`. Infix `== != > >= < <=` are supported (non-associative, lowest precedence), plus `EQ NE GT GTE LT LTE` functions (`NE`, `GTE`, `LTE` are additions).
- **Keyword arguments.** `re:"..."` and `src:"..."` are ordinary `name: expression` keywords, not special grammar productions (the EBNF's `regex`/`glob` rules would otherwise parse `re:re:"..."`). Duplicate keywords are a parse error.
- **Cell references.** `[c_7Hq2]` references a stored cell. `[n]` (0-based) references an earlier argument of the same `ANSWER`, which is what lets `PCT([0], [1], places:1)` work without repeating long `COUNT(FIND(...))` expressions. The README's `PCT([41], [312])` is a display abbreviation of that.
- **`DISTINCT`** returns a set. The spec's `ANSWER(DISTINCT(SYMBOLS(...)), text:"{0} functions call it.")` would render the list where it says "count"; use `COUNT(DISTINCT(...))` for the number.
- **Aggregates over sets** (`SUM(COLUMN(...))`) parse every element strictly with an extractor chosen by `as:"num|money|pct|date|datetime|duration"` (default `num`). A dirty cell fails with its source and line instead of being skipped. Sums run in `Decimal` in Python, not SQL floats; only `COUNT` is pushed down to one SQL statement.
- **Types.** `Span` is added (a single span; a one-element set coerces to it). `Table` is reserved, not implemented.
- **`PCT_OF(span)`** is interpreted as "read a percentage from this span"; the spec lists it under extraction without defining it.
- **`DAYS_BETWEEN(a, b)`** is symmetric (always ≥ 0, whole days) and returns a `Number` with unit `days`.
- **`LINES(src:, from:, to:)`** is defined here as a line-range span of one source.
- **Query keywords added:** `contains`, `since`, `until`, `level`, `logger` on `FIND`; `name`, `calls`, `re`, `kind` on `SYMBOLS`; `COLUMN("header", src:, filters...)`. `kind:"function"` includes methods.
- **Filters** inside `FIND`/`COLUMN` must be `FIELD("name")` compared with literals under `AND/OR/NOT`, so they compile to SQL. A filter only matches records that *have* the field.

**Verifier**

- **V4 ("data comes only from references")** needed a precise policy, because the spec's own examples contain a literal `500` and the digits in `"from Q2 to Q3"`. Numeric literals are allowed in *query filters*, in formatting keywords (`places:`), and when the number **appears in the user's question**; everywhere else they are rejected, as are string literals in value positions. Digits in the prose template or in a `NOT_FOUND`/`AMBIGUOUS` message are allowed only when glued to a letter (`Q3`, `FY2026`) or echoed from the question. Every `ANSWER` value must trace to a source.
- **V5.** A count of 0 is a legitimate answer, but only if `src:` matched at least one source; a typo'd glob is a V5 error with "did you mean" hints. `SUM/AVG/MIN/MAX` over nothing, an empty `ANSWER(FIND(...))`, and `NUM(FIND(...))` matching 0 rows are V5; matching more than one row is V3.
- **`NOT_FOUND`'s `searched:`** is metadata, so unknown handles are dropped rather than failing a refusal. `AMBIGUOUS` handles must exist (they are shown to the user).
- **Regex safety.** Python's `re` has no timeout and runs inside a SQL callback, so patterns with nested unbounded repetition (`(a+)+$`) are rejected up front with a hint. Other long-running queries are stopped by a deadline.

**Store**

- **Shared snapshots.** Identical canonical text is stored once. The spec put `source_id`, `raw_sha256`, `git_commit` and the offset map on `snapshot`, which would collide for two identical files (think empty `__init__.py`). They live on a `source_snapshot` history table instead; `snapshot.id` is still `sha256(canonical text)`.
- **Added columns/tables:** `log_record.ts_utc` and `.day` (calendar day *as written*, so `day:"2026-11-10"` means what the log says, while `ts_utc` is sortable; naive timestamps are read as UTC), `table_row`, `block.is_unit` (the default counting unit for `FIND`, so a CSV row is not counted once per cell), `run`.
- **Globs follow SQLite `GLOB`** (what the spec compiles to): `*` crosses `/`, so `logs/*.log` finds `logs/api/access.log` and a too-narrow glob cannot silently undercount. `**/` also matches zero directories; a bare name matches the basename.
- **Regex and number comparison in SQL** use registered functions (`spanql_re`, `spanql_text`, `spanql_num`) that read from an in-process snapshot cache, so SQLite never loads a multi-MB blob per row.
- **Re-anchoring** reports where text moved or was edited (exact context → moved text → edited-in-place between anchors → fuzzy lines). It never rewrites a cell; recompute creates a new one.
- **Traces** keep the first 50 contributing spans of a query and the exact count, plus the query and the pinned snapshot set. Expanding every contributing span lazily is not implemented.
- **Retrieval** is BM25 (porter stemming) over blocks with each block's heading path indexed alongside its text, plus adjacent-term phrase matches and direct symbol lookup. Vectors are not implemented.

## Not implemented (and why)

| Item | Why |
| --- | --- |
| tree-sitter (TS/Go/…) | Python uses stdlib `ast`; other languages get paragraph chunks and grep-style lines (no symbol queries). tree-sitter is the planned upgrade and would be an optional extra. |
| Docling (PDF/DOCX/PPTX/HTML) | M8. |
| Managed `llama-server`, MLX, Ollama JSON-schema envelope, vLLM grammar field | Only llama.cpp (`grammar`) and generic OpenAI-compatible are implemented. `runtime = "managed"` is rejected with a clear error. |
| Question rewriting for follow-ups, the model-based search-planning call | Heuristic retrieval only; `[c_id]` references work in hand-written or model-written formulas. |
| Watch mode, SSE streaming, web UI, OpenAI shim, `/metrics`, MCP server | M5/M8. |
| Eval sets, bake-off, fine-tune | Need models, GPU and data. |
| Docker/Compose, Homebrew formula, `spanql model pull` | Not testable in this environment. |

## Known limits

- **Storage cost.** Structured logs cost about 7× their size on disk (blocks, FTS, parsed fields). A 200k-line, 18.6 MB log became a 136 MB store. ADR-0003 anticipated this; compaction is future work.
- **Speed** (single thread): ingesting that log took ~11 s (~19k lines/s); re-ingesting it unchanged ~20 ms; `COUNT(FIND(...))` with a field filter or regex over 200k records takes 0.1–0.7 s.
- **Retrieval quality is unmeasured.** It surfaced the right passage first for the one question I checked by hand against these design docs, and was noisier on others. Real numbers need the M4 eval set.
- **`max_seconds`** is checked between attempts; a single model call is bounded by the client timeout (default 60 s).
- Source text is sent to whichever model endpoint you configure. Nothing else leaves the machine, and the client identifies itself only as `spanql`.
