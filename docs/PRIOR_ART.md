# Prior art

What spanql borrows, and what it does differently. All links checked 2026-11-17.

| Work | Idea | Relation to spanql |
| --- | --- | --- |
| Program of Thoughts (Chen et al., 2022) [arXiv:2211.12588](https://arxiv.org/abs/2211.12588) | The model writes a program; an interpreter does the computation | Core inspiration: separate reasoning from computation. spanql's "program" is a tiny, total DSL whose inputs must be source spans, not free literals. |
| PAL: Program-aided Language Models (Gao et al., 2022) [arXiv:2211.10435](https://arxiv.org/abs/2211.10435) | Offload solution steps to a Python runtime | Same split; spanql forbids general code for safety and determinism. |
| Binder (Cheng et al., 2022) [arXiv:2210.02875](https://arxiv.org/abs/2210.02875) | Bind LM calls into SQL/Python for table QA | Close cousin of "chat that runs SQL underneath". spanql keeps the model *out* of execution entirely. |
| Spider [arXiv:1809.08887](https://arxiv.org/abs/1809.08887), BIRD [arXiv:2305.03111](https://arxiv.org/abs/2305.03111) | Text-to-SQL benchmarks with execution accuracy | spanql's composition skill is text-to-DSL; we reuse *execution accuracy* as a metric. |
| FinQA [arXiv:2109.00122](https://arxiv.org/abs/2109.00122), TAT-QA [arXiv:2105.07624](https://arxiv.org/abs/2105.07624) | Numerical reasoning over text + tables with gold programs | Candidate training/eval data for the `docs-num` skill, license permitting. |
| Attributed QA (Bohnet et al., 2022) [arXiv:2212.08037](https://arxiv.org/abs/2212.08037) | Answers must come with evidence; evaluation framework for attribution | spanql makes attribution structural (every value has spans) rather than post hoc. |
| ALCE (Gao et al., 2023) [arXiv:2305.14627](https://arxiv.org/abs/2305.14627) | Benchmark for LLM text with citations; citation precision/recall | Metric inspiration for span-level attribution. |
| LlamaIndex CitationQueryEngine [docs](https://docs.llamaindex.ai/en/stable/examples/query_engine/citation_query_engine/) | RAG answers with numbered source citations | Citations are chunk-level and the model still writes the prose and numbers; spanql cites byte spans and computes values in code. |
| DSPy [arXiv:2310.03714](https://arxiv.org/abs/2310.03714), [dspy.ai](https://dspy.ai) | Declarative LM pipelines with optimized prompts | Candidate tool for Phase A prompt optimization against our eval metric. |
| IBM Granite citation adapter [model card](https://huggingface.co/ibm-granite/granitelib-rag-r1.0/blob/main/citations/README.md) | LoRA that produces sentence-level citations for an existing response | Post-hoc citation; spanql's citations are the inputs, not an afterthought. Useful as a comparison baseline. |
| Grammar-constrained decoding: llama.cpp GBNF [docs](https://github.com/ggml-org/llama.cpp/blob/HEAD/grammars/README.md), [Outlines](https://github.com/dottxt-ai/outlines), [vLLM structured outputs](https://docs.vllm.ai/en/latest/features/structured_outputs.html) | Mask tokens so outputs match a grammar | How a small model is kept to valid formulas with only real handles. |
| Spreadsheets | Every value has a visible formula and precedents | The UX model for provenance. |
| [concussion-protocol](https://github.com/johnnyclem/concussion-protocol) (John Clem) | Middleware that blocks/flags ungrounded external claims in agent output and emits provenance | Complementary outer gate when spanql is used as a tool by a larger agent (optional; see ARCHITECTURE §5.1). |
