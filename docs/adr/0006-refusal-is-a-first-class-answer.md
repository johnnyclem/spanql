# ADR-0006: Refusal ("not found in your sources") is a first-class, evaluated outcome

- Status: Proposed (implemented on `feat/implementation`; deviations in [ADR-0007](0007-implementation-deviations.md))
- Date: 2026-11-17

## Context
"If the answer isn't there, it says so" is a core promise. Small models tend to answer anyway, and RAG systems usually optimize for answering.

## Decision
- `NOT_FOUND` and `AMBIGUOUS` are formula forms the model can emit, and the harness emits `NOT_FOUND` itself when retries are exhausted.
- Refusals include a search report (queries, sources, block counts, near misses).
- Eval sets include ~25–30% unanswerable and ambiguous items; the headline safety metric is the wrong-answer rate on unanswerable questions, and no model or prompt change ships if it raises it.
- Fine-tune data includes deliberate negatives (gold span removed, near-miss distractors).

## Consequences
- + Users can trust silence as much as answers.
- − Some answerable questions will be refused (false refusals); tracked as abstention recall and improved over time, never by relaxing the verifier.
