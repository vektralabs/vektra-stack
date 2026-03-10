#!/usr/bin/env bash
# Vektra zero-downtime reindex script
# Usage: scripts/reindex.sh TARGET_VERSION [NAMESPACE]
set -euo pipefail

BASE_URL="${VEKTRA_API_URL:-http://localhost:${VEKTRA_PORT:-8000}}"
POLL_INTERVAL=5
POLL_TIMEOUT=600

# ---- arg parsing ----
for arg in "$@"; do
  case "$arg" in
    --dry-run)
      echo "[dry-run] Would trigger reindex at ${BASE_URL}/api/v1/reindex"
      exit 0
      ;;
    -h|--help)
      echo "Usage: scripts/reindex.sh TARGET_VERSION [NAMESPACE]"
      echo ""
      echo "Trigger a zero-downtime reindex job."
      echo ""
      echo "Arguments:"
      echo "  TARGET_VERSION   Target index version (required, integer >= 1)"
      echo "  NAMESPACE        Target namespace (default: 'default')"
      echo ""
      echo "Environment:"
      echo "  VEKTRA_API_URL   Base URL (default: http://localhost:8000)"
      echo "  VEKTRA_API_KEY   Bearer token (required)"
      exit 0
      ;;
    --invalid-*|--*)
      echo "Error: unknown argument '$arg'" >&2
      echo "Usage: scripts/reindex.sh TARGET_VERSION [NAMESPACE]" >&2
      exit 1
      ;;
  esac
done

TARGET_VERSION="${1:-}"
NAMESPACE="${2:-default}"

# ---- validation ----
if [ -z "$TARGET_VERSION" ]; then
  echo "Error: TARGET_VERSION argument is required" >&2
  echo "Usage: scripts/reindex.sh TARGET_VERSION [NAMESPACE]" >&2
  exit 1
fi

if ! [[ "$TARGET_VERSION" =~ ^[0-9]+$ ]] || [ "$TARGET_VERSION" -lt 1 ]; then
  echo "Error: TARGET_VERSION must be an integer >= 1" >&2
  exit 1
fi

if [ -z "${VEKTRA_API_KEY:-}" ]; then
  echo "Error: VEKTRA_API_KEY environment variable is required" >&2
  exit 1
fi

# ---- trigger reindex ----
echo "Triggering reindex for namespace '${NAMESPACE}' (target version: ${TARGET_VERSION})..."
JSON_BODY=$(python3 -c "
import json, sys
print(json.dumps({'namespace': sys.argv[1], 'target_index_version': int(sys.argv[2])}))
" "$NAMESPACE" "$TARGET_VERSION")

RESP=$(curl -sf -w "\n%{http_code}" \
  -X POST \
  -H "Authorization: Bearer ${VEKTRA_API_KEY}" \
  -H "Content-Type: application/json" \
  -d "$JSON_BODY" \
  "${BASE_URL}/api/v1/reindex") || {
  echo "Error: reindex request failed" >&2
  exit 1
}

HTTP_CODE=$(echo "$RESP" | tail -1)
BODY=$(echo "$RESP" | sed '$d')

if [ "$HTTP_CODE" != "200" ] && [ "$HTTP_CODE" != "202" ]; then
  echo "Error: unexpected HTTP ${HTTP_CODE}" >&2
  echo "$BODY" >&2
  exit 1
fi

JOB_ID=$(echo "$BODY" | python3 -c "import sys,json; print(json.load(sys.stdin)['job_id'])")
echo "Reindex job started: ${JOB_ID}"

# ---- poll for completion ----
ELAPSED=0
while [ "$ELAPSED" -lt "$POLL_TIMEOUT" ]; do
  sleep "$POLL_INTERVAL"
  ELAPSED=$((ELAPSED + POLL_INTERVAL))
  STATUS_RESP=$(curl -sf \
    -H "Authorization: Bearer ${VEKTRA_API_KEY}" \
    "${BASE_URL}/api/v1/reindex/${JOB_ID}/status") || continue
  STATUS=$(echo "$STATUS_RESP" | python3 -c "import sys,json; print(json.load(sys.stdin)['status'])")
  PROCESSED=$(echo "$STATUS_RESP" | python3 -c "import sys,json; d=json.load(sys.stdin); print(f\"{d['processed_documents']}/{d['total_documents']}\")" 2>/dev/null || echo "?")
  printf "  [%3ds] status: %s  progress: %s\n" "$ELAPSED" "$STATUS" "$PROCESSED"
  case "$STATUS" in
    completed|complete|done)
      echo "Reindex completed successfully."
      echo "To activate the new index: set VEKTRA_ACTIVE_INDEX_VERSION=${TARGET_VERSION} and restart."
      exit 0
      ;;
    failed|error)
      echo "Error: reindex failed" >&2
      echo "$STATUS_RESP" | python3 -m json.tool 2>/dev/null || echo "$STATUS_RESP" >&2
      exit 1
      ;;
  esac
done
echo "Error: reindex timed out after ${POLL_TIMEOUT}s" >&2
exit 1
