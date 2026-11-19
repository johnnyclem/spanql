# ADR-0003: SQLite as the span store, with immutable snapshots

- Status: Proposed (implemented on `feat/implementation`; deviations in [ADR-0007](0007-implementation-deviations.md))
- Date: 2026-11-17

## Context
We need byte-exact, stable span addressing, full-text search, typed side tables (logs, table cells, code symbols), SQL execution for aggregates, and a store that lives happily in a Docker volume or Homebrew's `var/`.

## Options
1. **SQLite + FTS5 (+ sqlite-vec optional).** Single file, embedded, SQL, BM25.
2. DuckDB + `fts` extension. Excellent analytics; FTS index must be rebuilt after changes ([DuckDB docs](https://duckdb.org/docs/stable/guides/sql_features/full_text_search)), and concurrent writer story is weaker for a long-running service.
3. Postgres + pgvector. Strong, but a second service to run, which is at odds with the Homebrew single-service goal.
4. A search engine (Tantivy/Elastic) + separate metadata DB. More moving parts.

## Decision
SQLite (WAL mode) with FTS5, typed side tables, and optional sqlite-vec. Snapshots are content-addressed by SHA-256 of canonical text and immutable; spans are `(snapshot, start, end)` plus a hash of the covered bytes. DuckDB remains an option later as an attached analytical engine for large CSV/log aggregates.

## Consequences
- + Zero-ops, portable, easy backup.
- + The planner can lower `FIND`/aggregate formulas straight to SQL.
- − Very large log corpora (tens of GB) may need partitioning or DuckDB/Parquet offload; to be measured.
- − Storing every snapshot's text costs disk; mitigated by retention and GC of snapshots not referenced by any cell.
