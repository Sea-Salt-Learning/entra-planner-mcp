# syntax=docker/dockerfile:1
FROM docker.io/astral/uv:0.13.7@sha256:96f40d3904aad8d0bd010539001d36c8e2188b981e1334471a90c4a1d93bb490 AS uv

# Build stage: install the locked dependencies (uv.lock pins every version and verifies every hash).
FROM cgr.dev/chainguard/python:latest-dev@sha256:6b7c43c25f9446e8b1cf850797826a3d40b7cccdc6370c6e1b31cb28adae0a18 AS build
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
FROM cgr.dev/chainguard/python:latest@sha256:a57ac9d33d8ca6eb2946d1d7e93a5600153d02e0c7c1fa3a322fc343f89451f5
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
