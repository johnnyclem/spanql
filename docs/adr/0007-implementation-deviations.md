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

**Reconciled with the parallel implementation (PR #1)**

PR #1 (`claude/dazzling-bohr-gh2dqx`, merged to `main` on 2026-11-18) was a second, independent implementation of the same design. After a side-by-side comparison this branch was kept as the base (`git merge -s ours`), and the following decisions from PR #1's `docs/IMPLEMENTATION.md` were adopted or explicitly declined.

23. **V4 is strict; no "obvious constant" allowance.** ARCHITECTURE.md §2.1 allowed "obvious constants" with a warning, but that let `MONEY($1) * 2` produce a grounded-looking value. A numeric literal is allowed only in formatting/config keywords (`places:`), inside query filters, or when the user typed that number in the question ("what is 3 times …"; numbers glued to letters such as `Q3` don't count). Adopted from PR #1.
24. **V5 for a `src:` that matches nothing.** A glob that matches no ingested source is a V5 error whose hint lists the three closest source URIs ("did you mean …"). A glob that matches sources but finds no records is still a legitimate 0 for a count question (decision 12). Adopted from PR #1.
25. **Regex safety.** `re:` patterns are checked up front (invalid patterns and nested unbounded repeats like `(a+)+` are V1 with a hint) and matched with the `regex` module (VERSION0, `re`-compatible) under a per-query time budget (10 s), with a SQLite progress handler aborting the statement when it runs out. PR #1 had the static check and a deadline but used `re`, which can't be interrupted, so `(a|a)*b` could still hang it; the `regex` timeout closes that.
26. **Re-anchoring "edited in place".** Between the exact-match and fuzzy steps, re-anchoring looks for the unchanged bytes on both sides of the old span (32/16/8-byte anchors) and reports the text now between them as `changed` (`method: "anchors"`). Results carry `method` (`context | exact | anchors | fuzzy`). Recompute still treats `changed` as stale. Adopted from PR #1.
27. **`[n]` references inside one `ANSWER`.** `[0]`, `[1]` (0-based) refer to earlier values of the same `ANSWER`, so `PCT([0], [1], places:1)` needn't repeat two `COUNT(FIND(...))` expressions. They are expanded before type checking, so every stored cell is a self-contained formula; forward and self references are V1. `[c_id]` still references a stored cell. Adopted from PR #1.
28. **A failed model backend is an error, not a refusal.** An unreachable or broken endpoint returns status `error` (CLI exit 2, HTTP 503, MCP `isError`), never "I couldn't find this in your sources". Adopted from PR #1's `HarnessError`.
29. **Ambiguous number separators are refused.** A number whose digits and separators continue past what the parser reads (`€1.234,50`, `12,5`, `10,20`) is a V3 error instead of being read as `€1.23`. spanql reads `1,234.50`-style numbers only; locale-aware parsing is future work.
30. **Unit decorations in templates.** `{0}%` / `{0} percent` after a percentage, and `${0}` / `€{0}` before an amount in that currency, are accepted and the duplicate is dropped (so answers never read "11.8%%"). On a value of any other unit they are V4. Scale words (`{0} million`) remain V4 (decision 11).
31. **Docs and determinism tests** from PR #1: the ARCHITECTURE.md examples, the README `evaluate` examples and the Docker config are tested, and a randomized test checks that 250 generated formulas give identical results across repeats and across stores ingested in different orders.

Decisions in PR #1 that were **not** adopted, and why:

- **Snapshot id = `sha256(canonical text)` with a separate `source_snapshot` history table.** PR #1 kept the design's pure content address and moved `source_id`/`raw_sha256`/`git_commit` to a history table, so identical files share one snapshot. This branch keeps decision 1 (`sha256(uri ‖ NUL ‖ text_sha256)`): a span id then always names exactly one file, which keeps provenance, staleness and re-anchoring one-to-one, at the cost of storing duplicate files twice. Content addressing within a source is unchanged, and the printable span id format is the same. Revisit if duplicate-heavy corpora (vendored code, many empty `__init__.py`) make storage a problem.
- **Globs follow SQLite `GLOB` (`*` crosses `/`).** This branch keeps segment-aware globs (`*` within a segment, `**` across) that also match the path without the root name or, for a bare pattern, the file name. Together with decision 24 a too-narrow glob can no longer undercount silently, which was PR #1's reason for the looser rule.
- **NOT_FOUND / AMBIGUOUS messages must not contain digits.** PR #1 applied V4 to refusal messages. They carry no values and are shown as prose, so this branch doesn't; the cited handles are still checked (V2).
- **Naive log timestamps read as UTC (`ts_utc`) plus a `day` column as written.** This branch keeps decision 6 (normalized ISO, no UTC conversion; zone-less RFC 3164 lines get `ts = NULL`). The observable `day:` behaviour on the demo and test corpora is the same.
- **Stdlib-only core and `http.server` API.** This branch keeps Starlette/uvicorn/httpx (decision 17) for the web UI, SSE, the OpenAI-compatible shim and `/metrics`.

## Consequences
ARCHITECTURE.md, HARNESS.md and DEPLOYMENT.md describe the original design. Where they disagree with this ADR, this ADR and the code win until the docs are revised at M0 sign-off.
