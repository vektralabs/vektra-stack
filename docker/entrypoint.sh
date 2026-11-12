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

# VEKTRA_ASSESS_ENABLED as the settings read it (a pydantic bool): 1, true, t, yes, y, on, in any
# case. Anything else, unset included, is disabled.
assess_enabled() {
  case "$(printf '%s' "${VEKTRA_ASSESS_ENABLED:-false}" | tr '[:upper:]' '[:lower:]')" in
    1|true|t|yes|y|on) return 0 ;;
    *) return 1 ;;
  esac
}

# Two Alembic branches (ADR-0033 D7). The platform is always migrated. The assess schema (its
# tables, the three vektra_assess_* roles, the SECURITY DEFINER functions, and with them the
# CREATEROLE requirement) only where assess is enabled: a deployment that never enables it gets
# none of them.
run_migrations() {
  echo "Running database migrations (platform)..."
  alembic upgrade platform@head
  if assess_enabled; then
    echo "Running database migrations (assess)..."
    alembic upgrade assess@head
  else
    echo "Assess is disabled (VEKTRA_ASSESS_ENABLED is not true): its schema is not migrated."
  fi
}

case "$CMD_TARGET" in
  server)
    run_migrations

    echo "Starting Vektra API server..."
    # main() validates (ARCH-057) before calling uvicorn, so a misconfiguration
    # prints its structured [STARTUP ERROR] and exits non-zero without an ASGI
    # traceback (BUG-025, NFR-009). Do not switch back to `uvicorn ...:app`: that
    # runs the validation inside the lifespan and reintroduces the traceback.
    exec python -m vektra_app.main
    ;;

  migrate)
    run_migrations
    ;;

  # Assess processes (ARCH-066). Each has its own target and never runs
  # migrations: only the API container (server) owns the schema. There is
  # deliberately no generic "worker" target that could start them by accident.
  assess-worker)
    echo "Starting Vektra assess worker..."
    exec python -m vektra_app.assess_worker
    ;;

  assess-parser)
    echo "Starting Vektra assess parser sidecar..."
    exec python -m vektra_app.assess_parser
    ;;

  *)
    echo "Unknown CMD_TARGET: $CMD_TARGET" >&2
    echo "Valid targets: server, migrate, assess-worker, assess-parser" >&2
    exit 1
    ;;
esac
