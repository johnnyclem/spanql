# Model

> Design phase. Model facts below were checked against model cards and repositories on 2026-11-17. No spanql-specific benchmark has been run yet; every quality number in this repo will come from the eval harness in §5, not from vendor leaderboards.

## 1. What the model has to do

The model is a **formula writer**, not an answerer. Given:

- the user's question (and short chat history),
- a list of 5–40 candidate spans with handles `$1..$k`,
- a compact description of the formula language and a few examples,

it emits **one** formula: `ANSWER(...)`, `NOT_FOUND(...)`, or `AMBIGUOUS(...)`. Decoding is grammar-constrained (see [ARCHITECTURE.md §2.3](ARCHITECTURE.md#23-grammar-ebnf-sketch)).

That task decomposes into skills a small model can learn:

1. **Selection**: which candidates actually answer the question (reading comprehension).
2. **Pinpointing**: copying the exact substring to quote (copying is something LMs are good at; grammar + exact-match verification catch errors).
3. **Composition**: choosing operators (`DIFF`, `PCT`, `COUNT(FIND(...))`), essentially text-to-SQL in a tiny DSL.
4. **Abstention**: emitting `NOT_FOUND` when nothing supports an answer.

It does *not* need world knowledge, arithmetic, or long-form writing. That is why a small model is the target.

## 2. Selection criteria

| Criterion | Why | Threshold |
| --- | --- | --- |
| License | ships in a Homebrew/Docker product | Apache-2.0 or MIT preferred |
| Size | must run on a laptop CPU or Apple Silicon alongside the app | ≤ 4B params; Q4 GGUF ≤ ~3 GB |
| Context | candidates + grammar description + history | ≥ 32K tokens |
| Structured output | follows a format under constrained decoding | good in independent small-model structured-output tests |
| Runtime support | llama.cpp/GGUF, Ollama, MLX, vLLM | at least llama.cpp + MLX |
| Fine-tunability | LoRA support in common tooling (TRL, Unsloth, MLX-LM) | yes |
| Non-thinking mode | short latency, no hidden chain of thought in output | switchable or absent |

## 3. Candidates

| Model | Params | License | Context | Notes | Source |
| --- | --- | --- | --- | --- | --- |
| **Qwen3.5-4B** | 4B | Apache-2.0 | 262,144 native (per card) | Released Feb 2026. Hybrid Gated DeltaNet + attention architecture, includes a vision encoder. Card documents a non-thinking (instruct) mode via `enable_thinking: False`. GGUF (e.g. `unsloth/Qwen3.5-4B-GGUF`), MLX (`mlx-community/Qwen3.5-4B-4bit`) and Ollama (`qwen3.5:4b`) builds exist. | [HF card](https://huggingface.co/Qwen/Qwen3.5-4B) |
| **Qwen3.5-2B** | ~2B | Apache-2.0 | (per card) | Same family; low-memory tier. Ollama tag `qwen3.5:2b`. | [HF card](https://huggingface.co/Qwen/Qwen3.5-2B) |
| Qwen3.5-0.8B | 0.8B | Apache-2.0 | (per card) | Candidate for the V6 relevance classifier or a very-low-end tier after fine-tune. | [HF card](https://huggingface.co/Qwen/Qwen3.5-0.8B) |
| Gemma 4 E2B (it) | 2.3B effective (5.1B with embeddings) | Apache-2.0 | 128K | Released Mar 2026; multimodal (text/image/audio). Larger on disk than its effective size suggests. | [HF card](https://huggingface.co/google/gemma-4-E2B-it) |
| Granite 4.0 H 1B / 350M | 1B / 0.35B | Apache-2.0 | (per card) | IBM; card and repo advertise RAG, tool use and native JSON output; chat template has a `documents` mode. IBM also publishes a citation-generation LoRA, but for larger Granite models, not Nano. | [repo](https://github.com/ibm-granite/granite-4.0-nano-language-models), [citations adapter](https://huggingface.co/ibm-granite/granitelib-rag-r1.0/blob/main/citations/README.md) |
| SmolLM3-3B | 3B | Apache-2.0 | 128K | Fully open data and training recipe; attractive for reproducible fine-tunes. | [HF card](https://huggingface.co/HuggingFaceTB/SmolLM3-3B) |
| Phi-4-mini-instruct | 3.8B | MIT | 128K | Feb 2025; older. | [HF card](https://huggingface.co/microsoft/Phi-4-mini-instruct) |
| Llama 3.2 3B Instruct | 3B | Llama 3.2 Community License | 128K | Sep 2024; custom license is less convenient for redistribution. | [HF card](https://huggingface.co/meta-llama/Llama-3.2-3B-Instruct) |

**Embeddings (optional vector retrieval):** [Qwen3-Embedding-0.6B](https://huggingface.co/Qwen/Qwen3-Embedding-0.6B) (Apache-2.0). BM25 alone is the default; vectors are opt-in.

**Third-party evidence, read with care.** An independent comparison of twelve 0.27–4B models on six skills (BF16, greedy, thinking off) reported Qwen3.5-2B, Gemma 4 E2B, Granite 4.2 3B and Nemotron 3 Nano 4B among the stronger structured-output scorers ([lokalniai SLM experiments](https://lokalniai.github.io/slm-experiments/)). A separate small test of older Qwen3 GGUF files found Qwen3-4B Q4_K_M produced exact JSON far more often than Qwen3-0.6B/1.7B Q8_0 *without* grammar constraints ([Kingy AI](https://kingy.ai/qwen3-small-models-json-benchmark/)), with the authors noting the sample is small. Neither tests our DSL; they only justify the shortlist.

### Recommendation

- **Default model: Qwen3.5-4B, instruct/non-thinking mode, Q4_K_M GGUF (or MLX 4-bit on Apple Silicon), with grammar-constrained decoding.** Best license/size/context/runtime-coverage combination on the list, and it's the strongest base to fine-tune.
- **Low-memory tier: Qwen3.5-2B**, same prompts and adapter recipe.
- **Bake-off before committing:** Qwen3.5-4B vs Qwen3.5-2B vs Gemma 4 E2B vs Granite 4.0 H 1B vs SmolLM3-3B on the spanql eval set (§5), off-the-shelf with constrained decoding. The winner of the bake-off, not this table, becomes the default.

## 4. Off-the-shelf first, then fine-tune

### 4.1 Phase A: off-the-shelf baseline (no training)

- System prompt: ~1.5K tokens describing the DSL (functions, reference forms, `NOT_FOUND` rules) + 8–12 worked examples covering extraction, arithmetic, log counting, code-symbol queries, ambiguity and refusal.
- Constrained decoding with a per-request GBNF (handles restricted to `$1..$k`).
- Verifier-driven retries (max 3) with precise error feedback.
- Optional: few-shot example selection by question similarity, and prompt optimization with [DSPy](https://dspy.ai) ([paper](https://arxiv.org/abs/2310.03714)) using the eval set as the metric.

If Phase A hits the MVP gates in §5.3, ship it and keep fine-tuning as an improvement track.

### 4.2 Phase B: fine-tune (LoRA)

Expected to be needed for: reliable abstention, choosing `FIND` queries for logs/code, and staying within budget on 2B-class models.

**Data generation (execution-filtered synthetic data):**

1. **Corpora.** Public, redistributable text of each source type: SEC-style financial filings and earnings releases (numbers + tables), open-source project docs, permissively licensed codebases, synthetic and public sample logs (web server, application JSON logs), CSV datasets. Plus existing QA datasets reformatted where licenses allow: [FinQA](https://arxiv.org/abs/2109.00122) and [TAT-QA](https://arxiv.org/abs/2105.07624) (numerical reasoning over text + tables, with gold programs/derivations that map naturally to formulas).
2. **Question + formula synthesis.** A larger teacher model (local or API, one-time) is shown a chunk and asked for (question, formula) pairs in the DSL. Templates seed the operator mix (extraction, DIFF/PCT, COUNT over FIND, date math, symbol queries).
3. **Execute and filter.** Run every formula through the real engine and verifier. Keep only pairs that verify. Drop pairs where a trivially different formula gives a different answer (ambiguity filter).
4. **Answerability negatives (≈25–30% of data).** Build `NOT_FOUND` examples by (a) removing the gold span from the candidate list, (b) swapping in near-miss distractors (Q2 instead of Q3, forecast instead of actual), (c) asking about entities absent from the corpus. Build `AMBIGUOUS` examples from genuinely conflicting spans.
5. **Retrieval realism.** Candidates are produced by the actual retriever, not hand-picked, so the model learns to work with noisy candidate lists. Shuffle handle order.
6. **Repair data.** Include (bad formula, verifier error) → corrected formula turns so the model learns to use retry feedback.
7. **Splits by source**, not by example, so eval measures generalization to unseen documents/repos.

**Training:**

- Method: [LoRA](https://arxiv.org/abs/2106.09685) (or [QLoRA](https://arxiv.org/abs/2305.14314) on small GPUs) supervised fine-tuning on chat-formatted examples; loss on the formula tokens only.
- Tooling: [TRL](https://github.com/huggingface/trl) SFTTrainer or [Unsloth](https://github.com/unslothai/unsloth) on CUDA; [MLX-LM](https://github.com/ml-explore/mlx-lm) LoRA for Apple Silicon experiments.
- Starting hyperparameters to sweep (not tuned values): rank 16–64, lr 1e-4–2e-4, 1–3 epochs, 5–50K examples. Pick by eval §5, not training loss.
- Optional second stage: preference optimization (DPO) with verified vs. verifier-rejected formulas for the same question.
- Artifacts: LoRA adapter + merged GGUF (Q4_K_M, Q8_0) + MLX 4-bit, published to a Hugging Face repo with a model card listing base model, data sources and licenses, and eval results.

## 5. Evaluation

### 5.1 Eval sets

| Set | Contents | Size target |
| --- | --- | --- |
| `docs-num` | numeric/date questions over reports and tables | 300 |
| `logs` | counts, rates, first/last occurrence, time windows | 200 |
| `code` | symbol, call-site and config questions over real repos | 200 |
| `files` | CSV/JSON/YAML lookups and aggregates | 150 |
| `unanswerable` | plausible questions with no support, and near-miss distractors | 300 |
| `ambiguous` | conflicting sources | 100 |
| `john-dogfood` | private, hand-labeled questions over John's own repos/logs (never used for training) | 100+ |

Each item: question, source set, gold answer value(s), gold supporting span(s) (or `NOT_FOUND`/`AMBIGUOUS`), and optionally a gold formula.

### 5.2 Metrics

- **Execution accuracy**: the evaluated value equals gold (with type-aware tolerance). Does not require the formula to match the gold formula (cf. execution accuracy in text-to-SQL benchmarks like [Spider](https://arxiv.org/abs/1809.08887) and [BIRD](https://arxiv.org/abs/2305.03111)).
- **Attribution precision / recall**: cited spans overlap gold support spans (span-level, byte overlap ≥ 50%), in the spirit of [ALCE](https://arxiv.org/abs/2305.14627) citation precision/recall and [Attributed QA](https://arxiv.org/abs/2212.08037).
- **Abstention**: precision and recall of `NOT_FOUND` on the union of answerable and unanswerable items. The headline safety metric is **wrong-answer rate on unanswerable items** (we want it near zero).
- **Grounded accuracy** = correct *and* attributed. The headline quality metric.
- **First-try verify rate** and **mean attempts**.
- **Latency** p50/p95 per question on reference hardware (M-series Mac, 8-core x86 CPU, single consumer GPU), and peak memory.

### 5.3 Gates

Targets are set *after* the Phase A baseline is measured. Proposed shape:

- MVP ship: wrong-answer rate on `unanswerable` ≤ an agreed small threshold, grounded accuracy ≥ an agreed floor on `docs-num`/`files`, p95 latency acceptable on an M-series laptop.
- Fine-tune adopted only if it beats the off-the-shelf baseline on grounded accuracy *without* worsening the unanswerable wrong-answer rate.

The eval harness runs in CI against a small smoke subset and on demand against the full set.

## 6. Serving backends

The harness talks to any **OpenAI-compatible** chat endpoint that supports grammar or JSON-schema constrained output:

| Backend | Where | Constrained decoding |
| --- | --- | --- |
| llama.cpp `llama-server` | default in Docker (CPU) and Homebrew | GBNF via `grammar` field; JSON Schema via `json_schema`/`response_format` ([docs](https://github.com/ggml-org/llama.cpp/blob/HEAD/grammars/README.md)) |
| Ollama | optional; users who already run it | JSON Schema via `format` ([docs](https://docs.ollama.com/capabilities/structured-outputs)); its documented interface is JSON Schema, so the DSL would be carried in a JSON envelope (`{"formula": "..."}`) and validated by the parser/verifier rather than masked token-by-token |
| MLX-LM server | Apple Silicon option | constrained decoding via a logits processor (e.g. [Outlines](https://github.com/dottxt-ai/outlines)) in-process; to be validated |
| vLLM | GPU Docker profile | [structured outputs](https://docs.vllm.ai/en/latest/features/structured_outputs.html) including grammar-based |

Because the verifier exists, a backend without true grammar support still works; it just costs more retries.
