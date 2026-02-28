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
CMD_TARGET="${1:-${CMD_TARGET:-server}}"

case "$CMD_TARGET" in
  server)
    echo "Running database migrations..."
    alembic upgrade head

    echo "Starting Vektra API server..."
    exec uvicorn vektra_app.main:app \
      --host 0.0.0.0 \
      --port 8000
    ;;

  migrate)
    echo "Running database migrations..."
    exec alembic upgrade head
    ;;

  *)
    echo "Unknown CMD_TARGET: $CMD_TARGET"
    echo "Valid targets: server, migrate"
    exit 1
    ;;
esac
