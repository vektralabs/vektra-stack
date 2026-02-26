#!/usr/bin/env bash
# Vektra health check script
# Usage: scripts/health.sh [--dry-run]
set -euo pipefail

BASE_URL="${VEKTRA_API_URL:-http://localhost:${VEKTRA_PORT:-8000}}"

# ---- arg parsing ----
for arg in "$@"; do
  case "$arg" in
    --dry-run)
      echo "[dry-run] Would check health at ${BASE_URL}/health"
      exit 0
      ;;
    -h|--help)
      echo "Usage: scripts/health.sh [--dry-run]"
      echo ""
      echo "Check Vektra health endpoint."
      echo "Set VEKTRA_API_KEY for detailed component output."
      echo ""
      echo "Environment:"
      echo "  VEKTRA_API_URL   Base URL (default: http://localhost:8000)"
      echo "  VEKTRA_API_KEY   Bearer token for detailed health"
      exit 0
      ;;
    *)
      echo "Error: unknown argument '$arg'" >&2
      echo "Usage: scripts/health.sh [--dry-run]" >&2
      exit 1
      ;;
  esac
done

# ---- shallow health check ----
echo "Checking ${BASE_URL}/health ..."
SHALLOW=$(curl -sf "${BASE_URL}/health" 2>&1) || {
  echo "Error: health endpoint unreachable at ${BASE_URL}/health" >&2
  exit 1
}

# Pretty-print if python3 is available, raw otherwise
if command -v python3 >/dev/null 2>&1; then
  echo "$SHALLOW" | python3 -m json.tool 2>/dev/null || echo "$SHALLOW"
else
  echo "$SHALLOW"
fi

# ---- detailed health (if API key is set) ----
if [ -n "${VEKTRA_API_KEY:-}" ]; then
  echo ""
  echo "Detailed health (authenticated):"
  DETAIL=$(curl -sf \
    -H "Authorization: Bearer ${VEKTRA_API_KEY}" \
    "${BASE_URL}/health?detail=full" 2>&1) || {
    echo "Warning: detailed health check failed" >&2
    exit 0
  }
  if command -v python3 >/dev/null 2>&1; then
    echo "$DETAIL" | python3 -m json.tool 2>/dev/null || echo "$DETAIL"
  else
    echo "$DETAIL"
  fi
fi
