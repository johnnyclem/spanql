# Deployment

> Design phase. File names, ports and paths are proposals.

spanql ships two ways, from one codebase:

1. **Docker** (Linux/macOS/Windows hosts, servers, CI).
2. **Homebrew service** (macOS first, Linux Homebrew best-effort), managed by `brew services` / launchd.

In both, there are two processes:

- **`spanql serve`**: the app (API, web UI, harness, engine, ingestion workers). Python.
- **A model runtime**: an OpenAI-compatible server (llama.cpp `llama-server` by default). Either *managed* (spanql starts and supervises it as a child process) or *external* (any endpoint you point it at: Ollama, LM Studio, vLLM, a remote box).

See [ADR-0004](adr/0004-python-core-external-model-runtime.md) and [ADR-0005](adr/0005-docker-and-homebrew-packaging.md).

## 1. Configuration

One TOML file plus env overrides (`SPANQL_*`).

```toml
# config.toml
[server]
bind = "127.0.0.1:7717"
# api_token from env SPANQL_API_TOKEN if bind is not loopback

[store]
path = "/data/spanql.db"          # Docker; Homebrew default: $(brew --prefix)/var/spanql/spanql.db
snapshot_retention_days = 90

[[sources]]
path = "/sources/docs"
kind = "doc"
watch = true

[[sources]]
path = "/sources/repos/payments"
kind = "code"
respect_gitignore = true

[[sources]]
path = "/sources/logs/*.log"
kind = "log"
parser = "auto"                   # or "jsonl", "nginx", custom regex profile

[model]
runtime = "managed"               # managed | external
backend = "llama.cpp"             # llama.cpp | ollama | mlx | vllm | openai-compatible
model = "qwen3.5-4b-instruct-q4_k_m"   # alias resolved by `spanql model pull`
endpoint = "http://127.0.0.1:7718/v1"  # used when runtime = "external"
context = 32768
threads = "auto"
gpu_layers = "auto"

[harness]
max_attempts = 3
candidate_token_budget = 8192
relevance_check = true

[retrieval]
vectors = false                   # true downloads an embedding model
```

## 2. Model download

Weights are **never** baked into the Homebrew bottle and are optional in the Docker image.

```sh
spanql model list                 # known aliases, sizes, licenses
spanql model pull qwen3.5-4b-instruct-q4_k_m
spanql model pull qwen3.5-2b-instruct-q4_k_m
spanql model pull spanql-ft-qwen3.5-2b-q4_k_m   # after the fine-tune exists
```

- Source: Hugging Face Hub (GGUF for llama.cpp, MLX for Apple Silicon), with pinned revision and SHA-256 per alias in a manifest shipped with each release.
- Cache: `$SPANQL_MODELS` (Docker: `/models` volume; Homebrew: `$(brew --prefix)/var/spanql/models`).
- First `spanql serve` without a model prints the exact `pull` command and serves the UI in a "no model" state (ingestion and `/v1/evaluate` still work).
- Offline installs: `spanql model import ./file.gguf --alias ...`.
- The license of each model is shown on pull and stored with it.

## 3. Docker

### 3.1 Images

| Image | Contents | Use |
| --- | --- | --- |
| `ghcr.io/johnnyclem/spanql:<ver>` | Python app, converters, tree-sitter grammars. No model, no torch by default. | the app |
| `ghcr.io/johnnyclem/spanql:<ver>-full` | + Docling with its layout models for high-fidelity PDF/DOCX | heavy document ingestion |
| upstream `llama.cpp` server image | `llama-server` (CPU or CUDA build) | model runtime sidecar |

Multi-arch (`linux/amd64`, `linux/arm64`). Non-root user, read-only root FS except volumes. Images will be private until John decides otherwise.

### 3.2 Dockerfile (outline, not final)

```dockerfile
FROM python:3.12-slim AS build
RUN pip install uv
WORKDIR /src
COPY pyproject.toml uv.lock ./
RUN uv sync --frozen --no-dev --no-install-project
COPY . .
RUN uv sync --frozen --no-dev

FROM python:3.12-slim
RUN useradd -r -u 10001 spanql
COPY --from=build /src/.venv /opt/spanql
ENV PATH=/opt/spanql/bin:$PATH SPANQL_CONFIG=/config/config.toml SPANQL_MODELS=/models
USER spanql
EXPOSE 7717
VOLUME ["/data", "/models"]
HEALTHCHECK CMD ["spanql", "health"]
ENTRYPOINT ["spanql", "serve"]
```

### 3.3 Compose

```yaml
# compose.yaml (sketch)
services:
  spanql:
    image: ghcr.io/johnnyclem/spanql:latest
    ports: ["127.0.0.1:7717:7717"]
    volumes:
      - spanql-data:/data              # SQLite store, run records
      - models:/models                 # shared with runtime
      - ./config.toml:/config/config.toml:ro
      - ~/Documents/kb:/sources/docs:ro   # sources: always read-only
      - ~/src:/sources/repos:ro
      - /var/log/myapp:/sources/logs:ro
    environment:
      SPANQL_MODEL__RUNTIME: external
      SPANQL_MODEL__ENDPOINT: http://llm:8080/v1
    depends_on: [llm]

  llm:
    image: ghcr.io/ggml-org/llama.cpp:server        # CPU
    command: ["-m", "/models/qwen3.5-4b-instruct-q4_k_m.gguf", "-c", "32768",
              "--host", "0.0.0.0", "--port", "8080"]
    volumes: ["models:/models:ro"]

  # GPU profile: `docker compose --profile gpu up`
  llm-gpu:
    profiles: ["gpu"]
    image: ghcr.io/ggml-org/llama.cpp:server-cuda
    command: ["-m", "/models/qwen3.5-4b-instruct-q4_k_m.gguf", "-c", "32768", "-ngl", "99",
              "--host", "0.0.0.0", "--port", "8080"]
    volumes: ["models:/models:ro"]
    deploy:
      resources:
        reservations:
          devices: [{driver: nvidia, count: 1, capabilities: [gpu]}]

  # model download job: `docker compose run --rm pull`
  pull:
    profiles: ["tools"]
    image: ghcr.io/johnnyclem/spanql:latest
    entrypoint: ["spanql", "model", "pull", "qwen3.5-4b-instruct-q4_k_m"]
    volumes: ["models:/models"]

volumes: {spanql-data: {}, models: {}}
```

Image tags for llama.cpp must be checked against the upstream registry at implementation time. A vLLM profile is an option for multi-user GPU hosts.

**macOS + Docker caveat:** Docker Desktop on macOS can't use the Apple GPU, so on a Mac the container runtime is CPU-only. Mac users who want Metal/MLX speed should use the Homebrew install, or run the app in Docker and point `model.endpoint` at a native runtime on the host (`http://host.docker.internal:...`).

### 3.4 Volumes and data

| Path | Contents | Backup? |
| --- | --- | --- |
| `/data/spanql.db` (+ `-wal`) | snapshots, spans, indexes, cells, run records | yes; SQLite online backup via `spanql backup` |
| `/models` | weights | no (re-pullable) |
| `/sources/*` | your files, mounted read-only | yours |

## 4. Homebrew

### 4.1 Distribution

A tap: `johnnyclem/homebrew-spanql` (private until release).

```sh
brew tap johnnyclem/spanql
brew install spanql
spanql init                      # writes $(brew --prefix)/etc/spanql/config.toml if absent
spanql model pull qwen3.5-4b-instruct-q4_k_m
brew services start spanql       # launchd agent, starts at login
open http://localhost:7717
```

### 4.2 Formula (sketch)

Python apps in Homebrew use `Language::Python::Virtualenv` with every dependency declared as a `resource` (see the [Formula Cookbook](https://docs.brew.sh/Formula-Cookbook) and the [`Homebrew::Service` API](https://docs.brew.sh/rubydoc/Homebrew/Service.html)).

```ruby
class Spanql < Formula
  include Language::Python::Virtualenv

  desc "Grounded Q&A over docs, logs, files and code with traceable formulas"
  homepage "https://github.com/johnnyclem/spanql"
  url "https://github.com/johnnyclem/spanql/archive/refs/tags/v0.1.0.tar.gz"
  sha256 "..."
  license "TBD"

  depends_on "python@3.12"
  depends_on "sqlite"
  depends_on "llama.cpp"          # provides llama-server for runtime = "managed"

  # resource "..." blocks generated with `brew update-python-resources`

  def install
    virtualenv_install_with_resources
    (etc/"spanql").mkpath
    (var/"spanql/models").mkpath
  end

  def post_install
    cfg = etc/"spanql/config.toml"
    cfg.write(default_config) unless cfg.exist?   # never overwrite user config
  end

  service do
    run [opt_bin/"spanql", "serve", "--config", etc/"spanql/config.toml"]
    keep_alive true
    working_dir var/"spanql"
    log_path var/"log/spanql.log"
    error_log_path var/"log/spanql.err.log"
    environment_variables SPANQL_MODELS: var/"spanql/models", PATH: std_service_path_env
  end

  test do
    assert_match "ok", shell_output("#{bin}/spanql selftest --no-model")
  end
end
```

`brew services start spanql` generates a launchd plist (`~/Library/LaunchAgents/homebrew.mxcl.spanql.plist`) from the `service` block; `keep_alive true` maps to launchd `KeepAlive`. On Linux, `brew services` uses systemd user units from the same block.

### 4.3 Model runtime on macOS

- **Managed llama.cpp (default):** `spanql serve` spawns `llama-server` from the `llama.cpp` formula on `127.0.0.1:7718`, with Metal offload on Apple Silicon, and restarts it on crash. One service to manage.
- **MLX (opt-in):** `backend = "mlx"` runs an MLX-LM server in the same venv (installed via `spanql extras install mlx`, Apple Silicon only). Constrained decoding on MLX needs validating first (see [MODEL.md §6](MODEL.md#6-serving-backends)).
- **External Ollama:** if the user already runs `brew services start ollama`, set `runtime = "external"`, `backend = "ollama"`.

### 4.4 Homebrew-specific constraints

- No network access during `install`: model weights and Docling layout models are fetched at runtime (`spanql model pull`, `spanql extras install docling`), never by the formula.
- Heavy optional dependencies (torch via Docling, MLX) stay out of the bottle to keep install size small; the base install handles PDFs with a lightweight text extractor, and the `docling` extra upgrades fidelity.
- File watching uses FSEvents on macOS (via the `watchdog` package) for `watch = true` sources. launchd agents run as the user, so they can read the user's files; macOS privacy prompts (TCC) may apply to `~/Documents`, `~/Desktop` and similar folders, which the docs must explain.

## 5. Security posture

- Loopback-only by default; token required for anything else.
- Sources mounted read-only in Docker; spanql never writes to source paths.
- The model has no tools that touch the filesystem or network; all I/O is done by the harness.
- No telemetry. Run records stay in the local store.
- Remote model endpoints are opt-in and clearly labeled in the UI ("questions and candidate text are sent to …").
