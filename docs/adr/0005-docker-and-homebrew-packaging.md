# ADR-0005: Ship as a Docker image and a Homebrew service; weights downloaded at runtime

- Status: Proposed
- Date: 2026-11-17

## Context
John wants it runnable both in a Docker container and as a Homebrew service. Docker Desktop on macOS can't use the Apple GPU, while Homebrew can run llama.cpp with Metal or MLX natively. Homebrew formulas must not download at install time, and multi-GB weights don't belong in a bottle or a base image.

## Decision
- One codebase, one `spanql serve` entrypoint, one config schema for both targets.
- Docker: app image without weights; Compose with a llama.cpp server sidecar (CPU default, CUDA profile), volumes for `/data` and `/models`, sources mounted read-only.
- Homebrew: a tap formula using `Language::Python::Virtualenv`, depending on `llama.cpp`, with a `service do` block so `brew services start spanql` creates a launchd agent (systemd user unit on Linux).
- Weights fetched by `spanql model pull <alias>` from Hugging Face with pinned revisions and checksums.

## Consequences
- + Mac users get native GPU speed via Homebrew; servers get Docker.
- + Small install artifacts; model choice decoupled from release.
- − First run requires a multi-GB download (or `model import` for offline use).
- − Two packaging paths to test in CI (Docker build + `brew test`/`brew audit` on macOS runners).
