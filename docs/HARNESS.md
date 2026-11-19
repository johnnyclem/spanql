# Agent harness

> **Implementation note (2026-11-19):** the first implementation differs from this document in a few places (snapshot ids, `match:` keyword, comparisons in the grammar, Starlette/argparse, runtime defaults, Docker/Homebrew details). See [ADR-0007](adr/0007-implementation-deviations.md).

> Design phase.

The harness is the "agent app": it turns a chat message into retrieval, one or more formula attempts, evaluation, verification, and a rendered answer. It is deliberately a **bounded loop with a fixed toolset**, not an open-ended agent: the model never runs shell commands, never reads files directly, and never sees more than the candidates the harness gives it.

## 1. Agent loop

```mermaid
sequenceDiagram
    participant U as User
    participant H as Harness
    participant R as Retriever
    participant M as Model
    participant E as Engine
    participant V as Verifier

    U->>H: question (+ chat history, scope)
    H->>H: rewrite follow-ups into standalone question
    H->>R: plan searches (keywords, source kinds, time window)
    R-->>H: candidates $1..$k + structural hints (log fields, tables, symbols)
    loop up to max_attempts
        H->>M: prompt(question, candidates, DSL spec, examples, last error)
        M-->>H: formula (grammar-constrained)
        H->>E: parse, resolve, plan, execute
        E-->>H: cells or error
        H->>V: verify(cells, formula, candidates)
        alt grounded
            V-->>H: ok
        else fixable
            V-->>H: error + hint; optionally widen retrieval
        end
    end
    H-->>U: answer with cell chips, or NOT_FOUND with search report
```

Steps in detail:

1. **Normalize the question.** For follow-ups ("and in Q2?"), a cheap rewrite into a standalone question; prior cells are available as `[c_id]` references.
2. **Search planning.** A small structured call (JSON schema-constrained) produces search intents: keywords/synonyms, source kinds (`doc|log|file|code`), globs, time windows, symbol names. Heuristics cover most cases without the model (dates in the question → log time window; identifiers in backticks → symbol search).
3. **Retrieve.** Hybrid: FTS5 BM25 + optional vectors + structural lookups (log side table, table cells, symbols). Re-rank, de-duplicate, pack into the context budget as handles `$1..$k`, each with source path, location and a trimmed excerpt. Log/file *schemas* (field names, column headers, value samples) are included so the model can write `FIND(...)` queries for aggregates it can't see line-by-line.
4. **Write formula.** Grammar-constrained call (see [MODEL.md](MODEL.md)).
5. **Evaluate + verify** ([ARCHITECTURE.md §3–5](ARCHITECTURE.md)).
6. **Retry** with the verifier's specific error (and possibly widened retrieval) up to `max_attempts` (default 3).
7. **Render.** The `text:` template is filled with formatted cell values. If there is no template, a deterministic default ("{0}") is used. The model never writes the final numbers in prose.

Budgets per question (defaults, configurable): 3 formula attempts, 1 search-planning call, 1 optional relevance check, 8K-token candidate budget, 30 s wall clock.

## 2. Tools

Internally the loop is implemented as tool calls so the same tools can be exposed to *external* agents (see §5):

| Tool | Input | Output |
| --- | --- | --- |
| `search` | query, kinds, globs, time window, k | candidate spans with handles |
| `describe_source` | source glob | schema: log fields, CSV columns, table headers, symbol kinds, time range |
| `read_span` | span id, context lines | exact text + location |
| `evaluate` | formula, handle table | cells or structured error |
| `explain_cell` | cell id | trace (formula, steps, input spans) |
| `ingest` | path/glob, kind override | job id (async) |
| `status` | — | sources, snapshot counts, index freshness, model status |

## 3. Chat interface

- **Web UI** served by the API process at `http://localhost:7717` (port TBD): chat on the left, provenance panel on the right. Answers stream; cell chips appear when evaluation completes. Clicking a chip opens the trace and highlights spans in source context.
- **Scope selector**: limit a conversation to certain sources ("only the payments repo and its logs").
- **Refusals** are shown plainly, with an expandable "what I searched" section and near-miss spans, plus a button to add a source and retry.
- **CLI**: `spanql ask "question"` prints the answer with numbered footnotes, `spanql trace <cell>` prints the trace, `spanql chat` for a REPL.

## 4. HTTP API (v1 sketch)

All JSON. Streaming via Server-Sent Events.

```http
POST /v1/sources            {"path": "~/src/payments", "kind": "code", "watch": true}
GET  /v1/sources
POST /v1/ingest/{source_id} → {"job_id": "..."}
GET  /v1/jobs/{job_id}

POST /v1/ask                {"question": "...", "conversation_id": "...", "scope": ["src:payments/**"]}
     → {"answer": {"text": "...", "cells": ["c_7Hq2"]}, "status": "grounded|not_found|ambiguous",
        "attempts": 1, "search_report": {...}}
POST /v1/ask?stream=1       → SSE events: retrieval, attempt, verifier, cell, answer

GET  /v1/cells/{id}
GET  /v1/cells/{id}/trace
POST /v1/cells/{id}/recompute
POST /v1/evaluate           {"formula": "...", "handles": {...}}   # power users / tests

GET  /v1/health  GET /v1/status
```

**OpenAI-compatible shim** (`POST /v1/chat/completions`, model `spanql`) so existing chat clients can talk to it; cell traces are returned as links in the message and in a `spanql` extension field.

**Auth:** bound to `127.0.0.1` by default with no auth. Binding to other interfaces requires an API token (`SPANQL_API_TOKEN`).

## 5. Use from other agents (MCP)

An MCP server mode (`spanql mcp`) exposes `search`, `ask`, `evaluate` and `explain_cell` as tools, so coding agents and assistants can ask grounded questions about a repo or its logs and get back values with traces. This is the natural point to pair with an outer grounding gate such as concussion-protocol (see [ARCHITECTURE.md §5.1](ARCHITECTURE.md#51-optional-concussion-protocol-as-an-outer-gate)).

## 6. Observability

- Every question writes a run record (retrieval results, prompts, formulas, verifier outcomes, timings) to the store; `spanql runs` lists them. This is also the source of real-world eval and fine-tune data (opt-in, local only).
- Prometheus-style `/metrics` (optional): latency, attempts, refusal rate, verifier failure reasons.
