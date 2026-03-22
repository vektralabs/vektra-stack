#!/usr/bin/env bash
# Vektra document ingestion script
# Usage: scripts/ingest.sh FILE [NAMESPACE]
set -euo pipefail

BASE_URL="${VEKTRA_API_URL:-http://localhost:${VEKTRA_PORT:-8000}}"
POLL_INTERVAL=2
POLL_TIMEOUT=300

# ---- arg parsing ----
for arg in "$@"; do
  case "$arg" in
    --dry-run)
      echo "[dry-run] Would ingest a file to ${BASE_URL}/api/v1/ingest"
      exit 0
      ;;
    -h|--help)
      echo "Usage: scripts/ingest.sh FILE [NAMESPACE]"
      echo ""
      echo "Ingest a document into Vektra."
      echo ""
      echo "Arguments:"
      echo "  FILE        Path to the document (PDF, DOCX, PPTX, MD)"
      echo "  NAMESPACE   Target namespace (default: 'default')"
      echo ""
      echo "Environment:"
      echo "  VEKTRA_API_URL   Base URL (default: http://localhost:8000)"
      echo "  VEKTRA_API_KEY   Bearer token (required)"
      exit 0
      ;;
    --invalid-*|--*)
      echo "Error: unknown argument '$arg'" >&2
      echo "Usage: scripts/ingest.sh FILE [NAMESPACE]" >&2
      exit 1
      ;;
  esac
done

FILE="${1:-}"
NAMESPACE="${2:-default}"

# ---- validation ----
if [ -z "$FILE" ]; then
  echo "Error: FILE argument is required" >&2
  echo "Usage: scripts/ingest.sh FILE [NAMESPACE]" >&2
  exit 1
fi

if [ ! -f "$FILE" ]; then
  echo "Error: file not found: $FILE" >&2
  exit 1
fi

if [ -z "${VEKTRA_API_KEY:-}" ]; then
  echo "Error: VEKTRA_API_KEY environment variable is required" >&2
  exit 1
fi

# Detect content type
FILENAME=$(basename "$FILE")
case "${FILENAME##*.}" in
  pdf)  CONTENT_TYPE="application/pdf" ;;
  docx) CONTENT_TYPE="application/vnd.openxmlformats-officedocument.wordprocessingml.document" ;;
  pptx) CONTENT_TYPE="application/vnd.openxmlformats-officedocument.presentationml.presentation" ;;
  md)   CONTENT_TYPE="text/markdown" ;;
  *)    CONTENT_TYPE="application/octet-stream" ;;
esac

# ---- ingest ----
echo "Ingesting ${FILENAME} into namespace '${NAMESPACE}'..."
RESP=$(curl -sf -w "\n%{http_code}" \
  -H "Authorization: Bearer ${VEKTRA_API_KEY}" \
  -F "file=@${FILE};type=${CONTENT_TYPE}" \
  "${BASE_URL}/api/v1/ingest?namespace=${NAMESPACE}") || {
  echo "Error: ingest request failed" >&2
  exit 1
}

HTTP_CODE=$(echo "$RESP" | tail -1)
BODY=$(echo "$RESP" | sed '$d')

case "$HTTP_CODE" in
  200)
    # Synchronous completion
    DOC_ID=$(echo "$BODY" | python3 -c "import sys,json; print(json.load(sys.stdin)['document_id'])")
    CHUNKS=$(echo "$BODY" | python3 -c "import sys,json; print(json.load(sys.stdin).get('chunk_count','?'))")
    echo "Ingested: document_id=${DOC_ID}, chunks=${CHUNKS}"
    ;;
  202)
    # Async job - poll until complete
    JOB_ID=$(echo "$BODY" | python3 -c "import sys,json; print(json.load(sys.stdin)['job_id'])")
    echo "Async job started: ${JOB_ID}"
    ELAPSED=0
    while [ "$ELAPSED" -lt "$POLL_TIMEOUT" ]; do
      sleep "$POLL_INTERVAL"
      ELAPSED=$((ELAPSED + POLL_INTERVAL))
      STATUS_RESP=$(curl -sf \
        -H "Authorization: Bearer ${VEKTRA_API_KEY}" \
        "${BASE_URL}/api/v1/ingest/jobs/${JOB_ID}/status") || continue
      STATUS=$(echo "$STATUS_RESP" | python3 -c "import sys,json; print(json.load(sys.stdin)['status'])")
      printf "  [%3ds] status: %s\n" "$ELAPSED" "$STATUS"
      case "$STATUS" in
        indexed|completed|complete)
          DOC_ID=$(echo "$STATUS_RESP" | python3 -c "import sys,json; print(json.load(sys.stdin).get('document_id','?'))")
          CHUNKS=$(echo "$STATUS_RESP" | python3 -c "import sys,json; print(json.load(sys.stdin).get('chunk_count','?'))")
          echo "Ingested: document_id=${DOC_ID}, chunks=${CHUNKS}"
          exit 0
          ;;
        failed|error)
          echo "Error: ingestion failed" >&2
          echo "$STATUS_RESP" | python3 -m json.tool 2>/dev/null || echo "$STATUS_RESP" >&2
          exit 1
          ;;
      esac
    done
    echo "Error: ingestion timed out after ${POLL_TIMEOUT}s" >&2
    exit 1
    ;;
  *)
    echo "Error: unexpected HTTP ${HTTP_CODE}" >&2
    echo "$BODY" >&2
    exit 1
    ;;
esac
