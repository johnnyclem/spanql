# Roadmap

> Design phase. Milestones are ordered; dates are deliberately absent until M0 is approved.

| # | Milestone | Scope | Exit criteria |
| --- | --- | --- | --- |
| **M0** | Design sign-off | These docs; ADRs accepted or amended; license chosen | John approves the design; open questions below answered or deferred explicitly |
| **M1** | Span store + ingestion | Plain text, Markdown, CSV/JSON, line logs, git repos (tree-sitter for Python/TS/Go first); snapshots, stable spans, FTS5; `spanql ingest`, `spanql read-span` | Re-ingesting an unchanged corpus is a no-op; every span round-trips byte-exact; re-anchoring works on edited files |
| **M2** | Formula language + engine (no model) | Grammar, parser, type checker, planner, SQL lowering, extractors, cell graph, `POST /v1/evaluate`, `spanql trace` | Hand-written formulas over the M1 corpus evaluate correctly with full traces; property tests for determinism |
| **M3** | Verifier + harness with off-the-shelf model | Retrieval, candidate packing, per-request GBNF, Qwen3.5-4B via llama.cpp, retries, `NOT_FOUND`/`AMBIGUOUS`, CLI `ask` | Eval harness runs end to end; Phase A baseline numbers recorded in `eval/RESULTS.md` |
| **M4** | Eval set + model bake-off | Build eval sets (MODEL.md §5.1), compare candidates, fix default model | Default model chosen on data; gates set from the baseline |
| **M5** | Chat UI + API + provenance panel | Web UI, SSE streaming, cell chips, trace panel, staleness/recompute, exports | Usable daily on John's own repos and logs (dogfood) |
| **M6** | Packaging | Docker image + Compose (CPU/GPU), Homebrew tap + `brew services`, `spanql model pull` | Fresh Mac: tap → install → pull → start → first grounded answer in under 10 minutes; same for `docker compose up` |
| **M7** | Fine-tune v1 | Data generation pipeline, LoRA on Qwen3.5-2B and -4B, GGUF/MLX exports | Beats Phase A on grounded accuracy without raising the unanswerable wrong-answer rate |
| **M8** | Rich documents + integrations | Docling extra (PDF tables, DOCX, PPTX), PDF highlight, MCP server mode, optional concussion-protocol gate | PDF table questions pass the `docs-num` gate; MCP tools usable from a coding agent |

## Open questions for John

1. **Name.** `spanql` is a working name. Keep it?
2. **License.** MIT / Apache-2.0 / source-available? Matters for the Homebrew tap and for publishing fine-tuned weights.
3. **Primary corpus for dogfooding.** Which repos/logs/docs should M5 target first?
4. **Fine-tune compute.** Local Apple Silicon (MLX LoRA) only, or rent a GPU for TRL/Unsloth runs?
5. **Teacher model for synthetic data.** Local larger model vs. a hosted API (cost and data-sharing trade-off; only public corpora would be sent).
6. **Public or private long term?** The repo is private for now.
