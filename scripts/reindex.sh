#!/usr/bin/env bash
# Vektra zero-downtime reindex script
# Usage: scripts/reindex.sh TARGET_VERSION [NAMESPACE]
#        scripts/reindex.sh --cleanup OLD_VERSION [NAMESPACE]
#
# The two modes are deliberately two runs. A reindex writes the new version
# alongside the old one and does not switch to it: until the operator sets
# VEKTRA_ACTIVE_INDEX_VERSION and restarts, the old version is the one serving
# traffic, and the API refuses to delete it (409). So the cleanup cannot happen
# in the same run as the reindex -- it happens after the switch, and refusing to
# fake that is what keeps the script from deleting a live index.
set -euo pipefail

BASE_URL="${VEKTRA_API_URL:-http://localhost:${VEKTRA_PORT:-8000}}"
POLL_INTERVAL=5
POLL_TIMEOUT=600

usage() {
  echo "Usage: scripts/reindex.sh TARGET_VERSION [NAMESPACE]"
  echo "       scripts/reindex.sh --cleanup OLD_VERSION [NAMESPACE]"
  echo ""
  echo "Trigger a zero-downtime reindex job, or reclaim a superseded version."
  echo ""
  echo "Arguments:"
  echo "  TARGET_VERSION   Target index version (required, integer >= 1)"
  echo "  OLD_VERSION      With --cleanup: the superseded version to delete"
  echo "  NAMESPACE        Target namespace (default: 'default')"
  echo ""
  echo "Options:"
  echo "  --cleanup        Delete a superseded index version. Run it only after"
  echo "                   switching VEKTRA_ACTIVE_INDEX_VERSION to the new one:"
  echo "                   the API refuses to delete the version it is serving."
  echo "  --dry-run        Print what would be called, then exit"
  echo ""
  echo "Environment:"
  echo "  VEKTRA_API_URL   Base URL (default: http://localhost:8000)"
  echo "  VEKTRA_API_KEY   Bearer token (required)"
}

# ---- arg parsing ----
MODE="reindex"
DRY_RUN="false"
VERSION=""
NAMESPACE="default"
POSITIONAL=0

for arg in "$@"; do
  case "$arg" in
    --dry-run)
      DRY_RUN="true"
      ;;
    --cleanup)
      MODE="cleanup"
      ;;
    -h|--help)
      usage
      exit 0
      ;;
    --invalid-*|--*|-*)
      echo "Error: unknown argument '$arg'" >&2
      usage >&2
      exit 1
      ;;
    *)
      if [ "$POSITIONAL" -eq 0 ]; then
        VERSION="$arg"
      elif [ "$POSITIONAL" -eq 1 ]; then
        NAMESPACE="$arg"
      else
        echo "Error: too many positional arguments" >&2
        usage >&2
        exit 1
      fi
      POSITIONAL=$((POSITIONAL + 1))
      ;;
  esac
done

if [ "$DRY_RUN" = "true" ]; then
  if [ "$MODE" = "cleanup" ]; then
    echo "[dry-run] Would delete index version ${VERSION:-<OLD_VERSION>} of namespace '${NAMESPACE}'" \
         "at ${BASE_URL}/api/v1/index-versions/${VERSION:-<OLD_VERSION>}"
  else
    echo "[dry-run] Would trigger reindex at ${BASE_URL}/api/v1/reindex"
  fi
  exit 0
fi

# ---- validation ----
if [ -z "$VERSION" ]; then
  if [ "$MODE" = "cleanup" ]; then
    echo "Error: OLD_VERSION argument is required with --cleanup" >&2
  else
    echo "Error: TARGET_VERSION argument is required" >&2
  fi
  usage >&2
  exit 1
fi

if ! [[ "$VERSION" =~ ^[0-9]+$ ]] || [ "$VERSION" -lt 1 ]; then
  echo "Error: version must be an integer >= 1" >&2
  exit 1
fi

if [ -z "${VEKTRA_API_KEY:-}" ]; then
  echo "Error: VEKTRA_API_KEY environment variable is required" >&2
  exit 1
fi

# ---- cleanup mode ----
if [ "$MODE" = "cleanup" ]; then
  echo "Deleting index version ${VERSION} of namespace '${NAMESPACE}'..."

  # No -f: a 409 means the API refused because that version is the one being
  # served, and that body is the most useful thing the operator can read.
  CLEANUP_RESP=$(curl -s -w "\n%{http_code}" \
    -X DELETE \
    -H "Authorization: Bearer ${VEKTRA_API_KEY}" \
    "${BASE_URL}/api/v1/index-versions/${VERSION}?namespace=${NAMESPACE}") || {
    echo "Error: cleanup request failed" >&2
    exit 1
  }

  CLEANUP_CODE=$(echo "$CLEANUP_RESP" | tail -1)
  CLEANUP_BODY=$(echo "$CLEANUP_RESP" | sed '$d')

  case "$CLEANUP_CODE" in
    200)
      REMOVED=$(echo "$CLEANUP_BODY" | python3 -c "import sys,json; print(json.load(sys.stdin)['chunks_removed'])")
      echo "Removed ${REMOVED} chunk(s) at index version ${VERSION}."
      if [ "$REMOVED" -eq 0 ]; then
        echo "Nothing was left at that version: it had already been cleaned up."
      fi
      exit 0
      ;;
    409)
      # The refusal (ERR-INDEX-001) is the one message the operator must read, so
      # dig it out of the envelope rather than dumping JSON at them. Falls back to
      # the raw body if the shape is not what we expect, instead of dying on a
      # KeyError and hiding the reason the delete was refused.
      echo "Error: refused. Index version ${VERSION} is the one currently being served." >&2
      echo "$CLEANUP_BODY" | python3 -c "
import sys, json
raw = sys.stdin.read()
try:
    body = json.loads(raw)
    err = body.get('detail', body).get('error', {})
    print(err.get('message') or err.get('remediation') or raw)
except Exception:
    print(raw)
" >&2
      echo "" >&2
      echo "Switch VEKTRA_ACTIVE_INDEX_VERSION to the new version and restart first." >&2
      exit 1
      ;;
    *)
      echo "Error: unexpected HTTP ${CLEANUP_CODE}" >&2
      echo "$CLEANUP_BODY" >&2
      exit 1
      ;;
  esac
fi

TARGET_VERSION="$VERSION"

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
      CHUNKS=$(echo "$STATUS_RESP" | python3 -c "import sys,json; print(json.load(sys.stdin)['chunks_reindexed'])")
      echo "Reindex completed: ${CHUNKS} chunk(s) written at version ${TARGET_VERSION}."
      if [ "$CHUNKS" -eq 0 ]; then
        echo "Warning: it wrote nothing. Do not switch: the new version is empty." >&2
        exit 1
      fi
      SOURCE_VERSION=$(echo "$STATUS_RESP" | python3 -c "import sys,json; print(json.load(sys.stdin)['source_index_version'])")
      echo ""
      echo "Next:"
      echo "  1. Check that ${CHUNKS} matches what you expected before switching."
      echo "  2. Set VEKTRA_ACTIVE_INDEX_VERSION=${TARGET_VERSION} and restart."
      echo "  3. Reclaim the old version: scripts/reindex.sh --cleanup ${SOURCE_VERSION} ${NAMESPACE}"
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
