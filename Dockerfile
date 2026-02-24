# ==========================================================================
# Vektra platform - multi-stage Docker build
# ==========================================================================
# Single image: CMD_TARGET=server (default) or CMD_TARGET=migrate.
# See docker/entrypoint.sh for command dispatch.
# ==========================================================================

# --------------------------------------------------------------------------
# Stage 1: builder - install dependencies with uv
# --------------------------------------------------------------------------
FROM python:3.12-slim AS builder

COPY --from=ghcr.io/astral-sh/uv:0.6.17 /uv /usr/local/bin/uv

WORKDIR /app

# Copy dependency manifests first (maximizes Docker layer cache).
# This layer only rebuilds when pyproject.toml or uv.lock change.
COPY pyproject.toml uv.lock .python-version ./
COPY vektra-shared/pyproject.toml vektra-shared/pyproject.toml
COPY vektra-admin/pyproject.toml vektra-admin/pyproject.toml
COPY vektra-core/pyproject.toml vektra-core/pyproject.toml
COPY vektra-ingest/pyproject.toml vektra-ingest/pyproject.toml
COPY vektra-index/pyproject.toml vektra-index/pyproject.toml
COPY vektra-app/pyproject.toml vektra-app/pyproject.toml

# Install third-party dependencies only (no workspace packages yet).
# Cached as long as manifests stay the same.
RUN uv sync --frozen --no-install-workspace --no-dev

# Copy README.md files (required by hatchling for wheel metadata)
COPY vektra-shared/README.md vektra-shared/README.md
COPY vektra-admin/README.md vektra-admin/README.md
COPY vektra-core/README.md vektra-core/README.md
COPY vektra-ingest/README.md vektra-ingest/README.md
COPY vektra-index/README.md vektra-index/README.md
COPY vektra-app/README.md vektra-app/README.md

# Copy all workspace source code
COPY vektra-shared/src vektra-shared/src
COPY vektra-admin/src vektra-admin/src
COPY vektra-core/src vektra-core/src
COPY vektra-ingest/src vektra-ingest/src
COPY vektra-index/src vektra-index/src
COPY vektra-app/src vektra-app/src

# Build and install workspace packages (non-editable: packages are placed
# in site-packages, no source dirs needed at runtime).
RUN uv sync --frozen --no-editable --no-dev

# --------------------------------------------------------------------------
# Stage 2: runtime - minimal production image
# --------------------------------------------------------------------------
FROM python:3.12-slim AS runtime

# Runtime system dependencies:
#   libmagic1  - content type detection via python-magic (vektra-ingest)
#   curl       - Docker HEALTHCHECK against /health endpoint
RUN apt-get update \
    && apt-get install -y --no-install-recommends libmagic1 curl \
    && rm -rf /var/lib/apt/lists/*

# Non-root user (uid 1000)
RUN useradd --uid 1000 --create-home vektra

WORKDIR /app

# Copy installed virtualenv from builder (includes all packages)
COPY --from=builder /app/.venv /app/.venv

# Alembic migrations (run via entrypoint: CMD_TARGET=migrate)
COPY alembic.ini /app/alembic.ini
COPY migrations/ /app/migrations/

# Entrypoint script (dispatches server / migrate)
COPY docker/entrypoint.sh /app/entrypoint.sh
RUN chmod +x /app/entrypoint.sh

RUN chown -R vektra:vektra /app
USER vektra

ENV PATH="/app/.venv/bin:$PATH" \
    PYTHONUNBUFFERED=1

EXPOSE 8000

# Liveness probe: check that the HTTP server is responding. Uses -so
# (silent, discard body) WITHOUT --fail so HTTP 503 (LLM unavailable
# when Ollama is not running) still counts as alive.  Connection refused
# (exit 7) correctly signals the process is down.
# The 45s start_period covers alembic migrations + sentence-transformers
# embedding model warm-up (~35s observed).
HEALTHCHECK --interval=10s --timeout=5s --start-period=45s --retries=3 \
    CMD curl -so /dev/null http://localhost:8000/health || exit 1

ENTRYPOINT ["/app/entrypoint.sh"]
CMD ["server"]
