# ADR-0004: Python core; model served by a separate OpenAI-compatible runtime

- Status: Proposed
- Date: 2026-11-17

## Context
Ingestion leans on Python-first libraries (Docling, tree-sitter bindings, log parsing), and the fine-tune and eval tooling (TRL, Unsloth, MLX-LM, DSPy) is Python. Model inference is best left to mature runtimes (llama.cpp, Ollama, MLX, vLLM), which all speak an OpenAI-compatible HTTP API.

## Options
1. **Python app + external runtime over HTTP.**
2. Python app with in-process inference (llama-cpp-python / MLX). Fewer processes, but crashes and memory pressure take the whole app down, and GPU builds complicate packaging.
3. Rust or Go single binary + external runtime. Nicer Homebrew bottle and startup, but loses the Python ingestion/eval ecosystem.

## Decision
Option 1. The app is a Python 3.12 package (`uv`-managed). It talks to the model only via an OpenAI-compatible endpoint with grammar/JSON-schema constrained output. In "managed" mode it spawns and supervises `llama-server`; in "external" mode it uses whatever endpoint is configured. Hot paths (parser, evaluator) are kept pure and small so they could be ported to Rust later without changing the DSL.

## Consequences
- + Swap models/backends without code changes; GPU support comes from the runtime.
- + Same eval harness drives training and production.
- − Two processes to supervise; mitigated by managed mode.
- − Homebrew Python formulas need many `resource` blocks; heavy extras (Docling/torch, MLX) are installed on demand instead of bottled.
