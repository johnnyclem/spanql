# ADR-0002: The model writes formulas; a deterministic engine computes values

- Status: Proposed
- Date: 2026-11-17

## Context
The product promise is that every value is traceable to exact source text and that missing answers are reported as missing. Free-text LLM answers can't guarantee either: numbers in prose may be copied, computed, or invented, and citations are added by the same model that might hallucinate.

## Decision
- The model's only output is a formula in a small, total, typed DSL (`ANSWER`, `NOT_FOUND`, `AMBIGUOUS`).
- All data enters formulas through references (`$n`, `$n."quote"`, `FIND(...)`), resolved by exact match against the span store.
- Decoding is grammar-constrained with a per-request grammar that only admits retrieved handles.
- A deterministic engine evaluates formulas; a verifier rejects ungrounded literals, unresolved quotes, type errors and empty support. After bounded retries, the system refuses.
- Final prose is a template with placeholders filled by the engine; the model never writes final values.

## Consequences
- + Every shown value has a reproducible derivation.
- + A small model suffices because its task is narrow.
- − Questions needing open-ended synthesis ("summarize the design") are out of scope or need a separate, clearly-labeled mode.
- − DSL coverage limits what can be asked; operators will be added based on eval failures.
- − Picking the *wrong but real* span remains possible; mitigated by the relevance check and the provenance UI, not eliminated.
