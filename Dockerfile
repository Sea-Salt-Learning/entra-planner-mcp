# syntax=docker/dockerfile:1
FROM docker.io/astral/uv:0.13.6@sha256:af22a52f5eafef3d78e551599d405c02df61919d18028f0cea8eda93bc9986d0 AS uv

# Build stage: install the locked dependencies (uv.lock pins every version and verifies every hash).
FROM cgr.dev/chainguard/python:latest-dev@sha256:8a5780972067cfba4592b10e786a889807ed876c1100f89410fd0ed4e398992c AS build
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
FROM cgr.dev/chainguard/python:latest@sha256:60e3c0d33ab77e50ace0850ccdf6eaec825ce89dad9b1ec2b0300a2070892b05
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
