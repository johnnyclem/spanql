# ADR-0007: Deviations from the design made during the first implementation

- Status: Proposed
- Date: 2026-11-19

## Context
The first implementation (branch `feat/implementation`) followed ARCHITECTURE.md, HARNESS.md and DEPLOYMENT.md. In a few places the design was ambiguous, underspecified, or wrong once it met real data and real models. This ADR records each change so the docs and the code can be reconciled.

## Decisions

**Store and ingestion**
1. **Snapshot id = sha256(uri ‖ NUL ‖ text_sha256)** instead of sha256(text). Two identical files at different paths would otherwise share a snapshot, and a span couldn't say which file it came from. Content addressing within one source is unchanged.
2. **FTS5 stores its own text** (a regular `fts5` table, not external-content). This is simpler to keep consistent and to garbage-collect old snapshots, at the cost of roughly doubling indexed text size. Revisit if stores get large.
3. **`table_row` side table** next to `table_cell`, holding each row's `header → value` map, so filters like `FIELD("region") == "West"` compile to SQL.
4. **Python symbols come from the stdlib `ast` module**, not tree-sitter. It's exact for Python and needs no native grammar. TypeScript, JavaScript and Go use tree-sitter (the `code` extra) with a regex fallback.
5. **PDF via pypdf** (the `pdf` extra) for M1. Docling stays an M8 extra.
6. **Log timestamps are normalized to ISO-8601 but not converted to UTC.** A timestamp with no zone stays zone-less, and RFC 3164 syslog lines with no year get `ts = NULL`, so they can't silently match a `day:` filter.
7. **File watching (`watch = true`) is not implemented yet.** Ingestion runs at `serve` startup and on demand (`spanql ingest`, `POST /v1/ingest`).

**Formula language**
8. **Comparisons are in the grammar** (`==, !=, >=, <=, >, <`), because FIND/COLUMN predicates need them.
9. **FIND's full-text keyword is `match:`**, not `text:`, to avoid a clash with ANSWER's `text:` template.
10. **`kind:` accepts `|` alternatives** (`"function|method"`, `"log|doc"`). The prompt advertised that syntax and models used it.
11. **V4 template rules, refined.** A template must not contain free-standing numbers, except numbers copied verbatim from the user's question (dates, ids). It must not put units, scales or currency next to a placeholder (`{0} million`, `${0}`), because values render with their own. Identifiers like `Q3` and `5xx` are prose.
12. **V5 (empty support).** COUNT over zero matches is an answer only for count questions. SUM/AVG/MIN/MAX over nothing, and DISTINCT over nothing for a non-count question, fail.
13. **A bare `$` is USD.** `C$`, `A$`, `US$` and ISO codes are recognized. Ambiguous slash dates (`3/4/2026`) are an error unless `DATE(..., format:"MDY"|"DMY")` is given.
14. **Accounting parentheses `(1,200)` mean negative** only for money or a bare cell value, not for prose like "(12) items".

**Harness and serving**
15. **Retries escalate temperature** (first try at the configured temperature, then 0.3, then 0.6, seed 7), so a rejected formula isn't regenerated verbatim. A deterministic first attempt is preserved.
16. **Offline backends.** `fake` (a rule-based formula writer) powers `selftest --no-model`, demos and CI. `ScriptedBackend` is used in tests. Neither is a quality proxy.
17. **Starlette instead of FastAPI, and argparse instead of Typer/Click** for the CLI. This means fewer dependencies, and the API surface is small.
18. **`model.runtime` defaults to `external`**, and `spanql init` writes `managed` when `llama-server` is on PATH. The Homebrew formula always writes `managed`, since it depends on `llama.cpp`.
19. **`server.allow_unauthenticated`** is an explicit opt-in to bind a non-loopback address without a token. It's used only by the Docker image, whose port compose publishes on the host's `127.0.0.1`. A warning is printed at startup.

**Packaging**
20. **The llama.cpp image tags `server`, `server-cuda`, `server-vulkan`, `full` and `light` were verified to exist** on 2026-11-18. Versioned tags couldn't be enumerated, so reproducible deployments should pin by digest. GPU is a compose override file (`compose.gpu.yaml`) rather than a profile, so the app always talks to a host named `llm`.
21. **The Homebrew formula uses `post_install_steps`** (Homebrew 7 deprecates `post_install`) and runs `spanql init --homebrew`. spanql detects its Homebrew prefix from its keg path, so config lookup also finds `$(brew --prefix)/etc/spanql/config.toml`. The `service` block helpers (`std_service_path_env`, `keep_alive`, `working_dir`, `log_path`, `error_log_path`, `environment_variables`) were checked against Homebrew's `service.rb`. The formula is HEAD-only until there's a release tag.
22. **Formula resources are generated from `uv.lock`** (`packaging/homebrew/gen_resources.py`), so the formula pins what CI tested. The tree-sitter `code` extra is excluded from the formula: Homebrew builds resources from sdists, and the tree-sitter-typescript 0.23.2 sdist fails to compile. Homebrew installs therefore extract TS/JS/Go symbols with the regex fallback. Python symbols are unaffected.

## Consequences
ARCHITECTURE.md, HARNESS.md and DEPLOYMENT.md describe the original design. Where they disagree with this ADR, this ADR and the code win until the docs are revised at M0 sign-off.
