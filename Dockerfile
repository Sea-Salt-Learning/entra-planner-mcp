# syntax=docker/dockerfile:1
FROM docker.io/astral/uv:0.13.7@sha256:96f40d3904aad8d0bd010539001d36c8e2188b981e1334471a90c4a1d93bb490 AS uv

# Build stage: install the locked dependencies (uv.lock pins every version and verifies every hash).
FROM cgr.dev/chainguard/python:latest-dev@sha256:62c16c709c48c8e66647c843b5aceeac81d4523134c742bc017a46f5a86ba5dd AS build
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
FROM cgr.dev/chainguard/python:latest@sha256:c932048a3f1db1fecefbd589d4fb90bf1d364b4c1ebd9aa8c866f29bb93e88cc
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
