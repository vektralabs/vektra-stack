#!/bin/sh
# ==========================================================================
# Vektra container entrypoint
#
# Dispatches based on CMD_TARGET env var (default: server).
# Phase 1: single container runs API server with in-process background
# tasks (ADR-0006). Phase 2: add worker role for Redis-backed arq scaling.
# ==========================================================================
set -e

# Prefer positional arg ($1 from CMD), then env var, then default.
# This allows both: docker run image migrate
#              and: CMD_TARGET=migrate docker run image
# The second form only works because the Dockerfile has no CMD: Docker
# always passes CMD as $1, so a CMD would permanently win over the env var.
CMD_TARGET="${1:-${CMD_TARGET:-server}}"

case "$CMD_TARGET" in
  server)
    echo "Running database migrations..."
    alembic upgrade head

    echo "Starting Vektra API server..."
    # main() validates (ARCH-057) before calling uvicorn, so a misconfiguration
    # prints its structured [STARTUP ERROR] and exits non-zero without an ASGI
    # traceback (BUG-025, NFR-009). Do not switch back to `uvicorn ...:app`: that
    # runs the validation inside the lifespan and reintroduces the traceback.
    exec python -m vektra_app.main
    ;;

  migrate)
    echo "Running database migrations..."
    exec alembic upgrade head
    ;;

  *)
    echo "Unknown CMD_TARGET: $CMD_TARGET" >&2
    echo "Valid targets: server, migrate" >&2
    exit 1
    ;;
esac
