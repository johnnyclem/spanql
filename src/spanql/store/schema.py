"""SQLite schema for the span store (see docs/ARCHITECTURE.md §1.4)."""

SCHEMA_VERSION = 1

SCHEMA = """
CREATE TABLE IF NOT EXISTS meta (key TEXT PRIMARY KEY, value TEXT);

CREATE TABLE IF NOT EXISTS source (
  id INTEGER PRIMARY KEY,
  uri TEXT UNIQUE NOT NULL,          -- "<root name>/<relative path>"
  path TEXT NOT NULL,                -- absolute filesystem path
  kind TEXT NOT NULL,                -- doc | log | file | code
  root TEXT NOT NULL,
  current_snapshot TEXT,
  removed INTEGER NOT NULL DEFAULT 0
);

CREATE TABLE IF NOT EXISTS snapshot (
  id TEXT PRIMARY KEY,               -- sha256(source uri || NUL || text_sha256)
  text_sha256 TEXT NOT NULL,         -- sha256 of canonical text (corruption guard)
  source_id INTEGER NOT NULL REFERENCES source(id),
  ingested_at TEXT NOT NULL,
  raw_sha256 TEXT NOT NULL,
  converter TEXT NOT NULL,           -- converter name + version (re-ingest key)
  git_commit TEXT,
  text BLOB NOT NULL,                -- canonical UTF-8 text
  offset_map TEXT,                   -- JSON: canonical line start -> raw byte offset (null = identity)
  meta TEXT                          -- JSON: converter metadata (pages, log parser, columns)
);
CREATE INDEX IF NOT EXISTS snapshot_source ON snapshot(source_id);

CREATE TABLE IF NOT EXISTS block (
  id INTEGER PRIMARY KEY,
  snapshot_id TEXT NOT NULL REFERENCES snapshot(id),
  kind TEXT NOT NULL,                -- paragraph | heading | code_fence | table_row | table_cell
                                     -- | log_record | symbol | chunk | page
  start INTEGER NOT NULL,
  end INTEGER NOT NULL,
  text_sha256 TEXT NOT NULL,
  path TEXT,                         -- heading path / table+row+col / symbol path
  line_start INTEGER,
  line_end INTEGER,
  page INTEGER
);
CREATE INDEX IF NOT EXISTS block_snapshot ON block(snapshot_id);

CREATE VIRTUAL TABLE IF NOT EXISTS block_fts USING fts5(text, tokenize='unicode61');

CREATE TABLE IF NOT EXISTS log_record (
  block_id INTEGER PRIMARY KEY REFERENCES block(id),
  ts TEXT, level TEXT, logger TEXT,
  message_start INTEGER, message_end INTEGER,
  fields TEXT                        -- JSON
);
CREATE INDEX IF NOT EXISTS log_record_ts ON log_record(ts);

CREATE TABLE IF NOT EXISTS table_row (
  block_id INTEGER PRIMARY KEY REFERENCES block(id),
  table_id TEXT NOT NULL, row INTEGER NOT NULL,
  fields TEXT                        -- JSON {header: raw cell text}
);

CREATE TABLE IF NOT EXISTS table_cell (
  block_id INTEGER PRIMARY KEY REFERENCES block(id),
  table_id TEXT NOT NULL, row INTEGER NOT NULL, col INTEGER NOT NULL,
  header TEXT, row_block INTEGER
);
CREATE INDEX IF NOT EXISTS table_cell_row ON table_cell(row_block);

CREATE TABLE IF NOT EXISTS code_symbol (
  block_id INTEGER PRIMARY KEY REFERENCES block(id),
  lang TEXT, kind TEXT, name TEXT, qualified_name TEXT,
  parent_block INTEGER,
  calls TEXT                         -- JSON list of called names
);
CREATE INDEX IF NOT EXISTS code_symbol_name ON code_symbol(name);

CREATE TABLE IF NOT EXISTS cell (
  id TEXT PRIMARY KEY,
  question_id TEXT,
  formula TEXT NOT NULL,
  value TEXT NOT NULL,               -- JSON
  type TEXT NOT NULL,
  status TEXT NOT NULL,
  created_at TEXT NOT NULL,
  trace TEXT NOT NULL                -- JSON trace (inputs, steps, plan)
);
CREATE TABLE IF NOT EXISTS cell_dep (
  cell_id TEXT NOT NULL, dep_kind TEXT NOT NULL, ref TEXT NOT NULL
);
CREATE INDEX IF NOT EXISTS cell_dep_cell ON cell_dep(cell_id);
CREATE INDEX IF NOT EXISTS cell_dep_ref ON cell_dep(ref);

CREATE TABLE IF NOT EXISTS run (
  id TEXT PRIMARY KEY,
  created_at TEXT NOT NULL,
  question TEXT NOT NULL,
  status TEXT NOT NULL,
  record TEXT NOT NULL               -- JSON run record
);
"""
