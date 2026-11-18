"""SQLite schema (ADR-0003). Migrations are append-only entries in MIGRATIONS."""

SCHEMA_VERSION = 1

MIGRATIONS: dict[int, str] = {
    1: """
CREATE TABLE source (
    id               INTEGER PRIMARY KEY,
    uri              TEXT NOT NULL UNIQUE,
    kind             TEXT NOT NULL,                 -- doc|log|file|code
    root             TEXT,
    rel_path         TEXT,
    current_snapshot TEXT REFERENCES snapshot(id),  -- NULL once the file disappears
    deleted_at       TEXT
);

-- Content-addressed and shared: identical canonical text is stored once.
CREATE TABLE snapshot (
    id           TEXT PRIMARY KEY,                  -- sha256 of canonical text
    text         BLOB NOT NULL,
    n_bytes      INTEGER NOT NULL,
    segmentation TEXT NOT NULL,
    created_at   TEXT NOT NULL
);

-- Which source had which snapshot when (history; offset map is per raw file).
CREATE TABLE source_snapshot (
    id            INTEGER PRIMARY KEY,
    source_id     INTEGER NOT NULL REFERENCES source(id) ON DELETE CASCADE,
    snapshot_id   TEXT NOT NULL REFERENCES snapshot(id),
    ingested_at   TEXT NOT NULL,
    superseded_at TEXT,
    raw_sha256    TEXT NOT NULL,
    raw_size      INTEGER NOT NULL,
    git_commit    TEXT,
    offset_map    BLOB
);
CREATE INDEX source_snapshot_src ON source_snapshot(source_id, superseded_at);
CREATE INDEX source_snapshot_snap ON source_snapshot(snapshot_id);

CREATE TABLE block (
    id          INTEGER PRIMARY KEY,
    snapshot_id TEXT NOT NULL REFERENCES snapshot(id) ON DELETE CASCADE,
    kind        TEXT NOT NULL,
    start       INTEGER NOT NULL,
    "end"       INTEGER NOT NULL,
    text_sha256 TEXT NOT NULL,
    path        TEXT,
    line_start  INTEGER,
    line_end    INTEGER,
    page        INTEGER,
    is_unit     INTEGER NOT NULL DEFAULT 0
);
CREATE INDEX block_snapshot ON block(snapshot_id, kind);

CREATE VIRTUAL TABLE block_fts USING fts5(
    text, content='', contentless_delete=1,
    tokenize='porter unicode61 remove_diacritics 2'
);

-- typed side tables
CREATE TABLE log_record (
    block_id      INTEGER PRIMARY KEY REFERENCES block(id) ON DELETE CASCADE,
    ts            TEXT,   -- ISO-8601 as written (with offset if present)
    ts_utc        TEXT,   -- fixed-width UTC, sortable; naive timestamps are read as UTC
    day           TEXT,   -- calendar date as written
    level         TEXT,
    logger        TEXT,
    message_start INTEGER,
    message_end   INTEGER,
    fields        TEXT    -- JSON object
);
CREATE INDEX log_record_day ON log_record(day);
CREATE INDEX log_record_ts ON log_record(ts_utc);

CREATE TABLE table_row (
    block_id INTEGER PRIMARY KEY REFERENCES block(id) ON DELETE CASCADE,
    table_id INTEGER NOT NULL,
    row      INTEGER NOT NULL,
    fields   TEXT NOT NULL                          -- JSON object header -> value
);

CREATE TABLE table_cell (
    block_id  INTEGER PRIMARY KEY REFERENCES block(id) ON DELETE CASCADE,
    table_id  INTEGER NOT NULL,
    row       INTEGER NOT NULL,
    col       INTEGER NOT NULL,
    header    TEXT NOT NULL,
    row_block INTEGER NOT NULL REFERENCES block(id) ON DELETE CASCADE
);
CREATE INDEX table_cell_header ON table_cell(header COLLATE NOCASE);

CREATE TABLE code_symbol (
    block_id       INTEGER PRIMARY KEY REFERENCES block(id) ON DELETE CASCADE,
    lang           TEXT NOT NULL,
    kind           TEXT NOT NULL,                   -- module|class|function|method
    name           TEXT NOT NULL,
    qualified_name TEXT NOT NULL,
    parent_block   INTEGER,
    calls          TEXT NOT NULL DEFAULT '[]'       -- JSON array of dotted call names
);
CREATE INDEX code_symbol_name ON code_symbol(name);

-- answers
CREATE TABLE cell (
    id          TEXT PRIMARY KEY,
    question_id TEXT,
    formula     TEXT NOT NULL,
    value       TEXT NOT NULL,                      -- JSON
    type        TEXT NOT NULL,
    status      TEXT NOT NULL,
    created_at  TEXT NOT NULL,
    trace       TEXT NOT NULL                       -- JSON: inputs, steps, handles, model...
);
CREATE INDEX cell_question ON cell(question_id);

CREATE TABLE cell_dep (
    cell_id  TEXT NOT NULL REFERENCES cell(id) ON DELETE CASCADE,
    dep_kind TEXT NOT NULL,                         -- span|cell|query
    ref      TEXT NOT NULL
);
CREATE INDEX cell_dep_cell ON cell_dep(cell_id);
CREATE INDEX cell_dep_ref ON cell_dep(dep_kind, ref);

-- one row per question asked (HARNESS.md section 6)
CREATE TABLE run (
    id          TEXT PRIMARY KEY,
    created_at  TEXT NOT NULL,
    question    TEXT NOT NULL,
    status      TEXT NOT NULL,
    attempts    INTEGER NOT NULL,
    elapsed_ms  INTEGER NOT NULL,
    record      TEXT NOT NULL                       -- JSON: retrieval, prompts, formulas, verifier outcomes
);
""",
}
