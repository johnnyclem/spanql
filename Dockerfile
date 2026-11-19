# syntax=docker/dockerfile:1
# spanql app image. No model weights inside: they live in the /models volume
# (`spanql model pull ...`) and are served by a llama.cpp sidecar (see compose.yaml).

ARG PYTHON_IMAGE=python:3.12-slim-bookworm

FROM ghcr.io/astral-sh/uv:0.13.7 AS uv

FROM ${PYTHON_IMAGE} AS build
COPY --from=uv /uv /usr/local/bin/uv
ENV UV_COMPILE_BYTECODE=1 UV_LINK_MODE=copy UV_PYTHON_DOWNLOADS=never \
    UV_PROJECT_ENVIRONMENT=/opt/spanql
WORKDIR /src
# dependencies first (cached layer), then the project itself
COPY pyproject.toml uv.lock README.md ./
RUN --mount=type=cache,target=/root/.cache/uv \
    uv sync --frozen --no-dev --no-install-project --extra code --extra pdf
COPY src ./src
RUN --mount=type=cache,target=/root/.cache/uv \
    uv sync --frozen --no-dev --no-editable --extra code --extra pdf

FROM ${PYTHON_IMAGE}
LABEL org.opencontainers.image.source="https://github.com/johnnyclem/spanql" \
      org.opencontainers.image.description="Grounded Q&A over docs, logs, files and code"
RUN useradd --system --uid 10001 --home-dir /data --shell /usr/sbin/nologin spanql \
    && mkdir -p /data /models /config /sources \
    && chown spanql:spanql /data /models
COPY --from=build /opt/spanql /opt/spanql
COPY packaging/docker/config.toml /config/config.toml
ENV PATH=/opt/spanql/bin:$PATH \
    SPANQL_CONFIG=/config/config.toml \
    SPANQL_MODELS=/models \
    PYTHONUNBUFFERED=1
USER spanql
WORKDIR /data
EXPOSE 7717
VOLUME ["/data", "/models"]
HEALTHCHECK --interval=30s --timeout=5s --start-period=60s --retries=3 \
    CMD ["spanql", "health"]
ENTRYPOINT ["spanql"]
CMD ["serve"]
