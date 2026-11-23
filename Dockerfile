# syntax=docker/dockerfile:1
FROM docker.io/astral/uv:0.13.9@sha256:d3b601f0c7b118bc1753febf7c41cc80981df678445e0254284eecf84db79a7d AS uv

# Build stage: install the locked dependencies (uv.lock pins every version and verifies every hash).
FROM cgr.dev/chainguard/python:latest-dev@sha256:e2736acdf97f2b1cb59545f4f26317b18a953e90e59fa4c212a782315b248111 AS build
COPY --from=uv /uv /usr/local/bin/uv
WORKDIR /app
ENV UV_PROJECT_ENVIRONMENT=/app/venv \
    UV_PYTHON=/usr/bin/python3 \
    UV_PYTHON_DOWNLOADS=never \
    UV_NO_CACHE=1 \
    UV_COMPILE_BYTECODE=1 \
    UV_LINK_MODE=copy
COPY --chown=65532:65532 pyproject.toml uv.lock README.md ./
RUN uv sync --frozen --no-dev --no-install-project --no-build
COPY --chown=65532:65532 src/entra_planner_mcp /app/src/entra_planner_mcp
RUN /app/venv/bin/python -m compileall -q /app/src

# Runtime stage: distroless (no shell, no package manager), non-root.
FROM cgr.dev/chainguard/python:latest@sha256:f33049a06b10d6bd729e08a181094774ca5e9c7b0cf1bbe674aa977cd5258daa
LABEL org.opencontainers.image.title="entra-planner-mcp" \
      org.opencontainers.image.description="Stateless MCP server for Microsoft Planner using Entra ID On-Behalf-Of" \
      org.opencontainers.image.licenses="MIT"
WORKDIR /app
COPY --from=build --chown=0:0 /app/venv /app/venv
COPY --from=build --chown=0:0 /app/src /app/src
ENV PATH="/app/venv/bin:$PATH" \
    PYTHONPATH=/app/src \
    PYTHONDONTWRITEBYTECODE=1 \
    PYTHONUNBUFFERED=1 \
    PORT=8000
USER 65532:65532
EXPOSE 8000
ENTRYPOINT ["/app/venv/bin/python", "-m", "entra_planner_mcp"]
