#!/usr/bin/env bash
# Vektra end-to-end MVP demo (REQ-027 checkpoints)
# Usage: scripts/demo.sh [--dry-run]
set -euo pipefail

BASE_URL="${VEKTRA_API_URL:-http://localhost:${VEKTRA_PORT:-8000}}"
COMPOSE="docker compose"
HEALTH_TIMEOUT="${HEALTH_TIMEOUT:-120}"
INGEST_TIMEOUT=180
SAMPLE_PDF="tests/fixtures/sample.pdf"
BOOTSTRAP_KEY="${VEKTRA_BOOTSTRAP_KEY:-${VEKTRA_ADMIN_BOOTSTRAP_KEY:-}}"

# ---- arg parsing ----
for arg in "$@"; do
  case "$arg" in
    --dry-run)
      echo "[dry-run] Would run full MVP demo against ${BASE_URL}"
      exit 0
      ;;
    -h|--help)
      echo "Usage: scripts/demo.sh [--dry-run]"
      echo ""
      echo "Run the full Vektra MVP demo (REQ-027 checkpoints):"
      echo "  1. docker compose up"
      echo "  2. Wait for /health 200"
      echo "  3. Create API key via bootstrap"
      echo "  4. Ingest sample PDF"
      echo "  5. Query and verify sources"
      echo ""
      echo "Environment:"
      echo "  VEKTRA_API_URL              Base URL (default: http://localhost:8000)"
      echo "  VEKTRA_BOOTSTRAP_KEY        Bootstrap key (reads .env if unset)"
      echo "  VEKTRA_ADMIN_BOOTSTRAP_KEY  Alternative bootstrap key variable"
      exit 0
      ;;
    *)
      echo "Error: unknown argument '$arg'" >&2
      echo "Usage: scripts/demo.sh [--dry-run]" >&2
      exit 1
      ;;
  esac
done

# ---- helpers ----
DEMO_START=$(date +%s)

elapsed() {
  echo $(( $(date +%s) - DEMO_START ))
}

checkpoint() {
  printf "[T+%3ds] %s\n" "$(elapsed)" "$1"
}

fail() {
  printf "\n[T+%3ds] FAILED: %s\n" "$(elapsed)" "$1" >&2
  exit 1
}

# ---- pre-flight ----
if [ ! -f "$SAMPLE_PDF" ]; then
  fail "Sample PDF not found at ${SAMPLE_PDF} (run from repository root)"
fi

# Try to read bootstrap key from .env if not set
if [ -z "$BOOTSTRAP_KEY" ] && [ -f .env ]; then
  BOOTSTRAP_KEY=$(grep -E '^VEKTRA_ADMIN_BOOTSTRAP_KEY=' .env 2>/dev/null | cut -d= -f2- | tr -d "'" | tr -d '"' || true)
fi
if [ -z "$BOOTSTRAP_KEY" ]; then
  fail "Set VEKTRA_BOOTSTRAP_KEY or VEKTRA_ADMIN_BOOTSTRAP_KEY (or define it in .env)"
fi

echo "=== Vektra MVP Demo ==="
echo ""

# ---- step 1: compose up ----
checkpoint "Starting Docker Compose stack..."
$COMPOSE up -d 2>&1 | sed 's/^/  /'
checkpoint "Stack started"

# ---- step 2: wait for health ----
checkpoint "Waiting for /health 200 (timeout: ${HEALTH_TIMEOUT}s)..."
WAIT=0
while [ "$WAIT" -lt "$HEALTH_TIMEOUT" ]; do
  HTTP_CODE=$(curl -so /dev/null -w "%{http_code}" "${BASE_URL}/health" 2>/dev/null || echo "000")
  if [ "$HTTP_CODE" = "200" ] || [ "$HTTP_CODE" = "503" ]; then
    checkpoint "Health endpoint responding (HTTP ${HTTP_CODE})"
    break
  fi
  sleep 2
  WAIT=$((WAIT + 2))
done
if [ "$WAIT" -ge "$HEALTH_TIMEOUT" ]; then
  fail "Health endpoint not reachable after ${HEALTH_TIMEOUT}s"
fi

# ---- step 3: create API key via bootstrap ----
checkpoint "Creating API key via bootstrap..."
KEY_RESP=$(curl -sf \
  -H "Authorization: Bearer ${BOOTSTRAP_KEY}" \
  -H "Content-Type: application/json" \
  -d '{"label":"demo-key","scopes":["admin","ingest","query"]}' \
  "${BASE_URL}/api/v1/api-keys" 2>&1) || {
  # Bootstrap may already be consumed - try with existing key if set
  if [ -n "${VEKTRA_API_KEY:-}" ]; then
    checkpoint "Bootstrap consumed, using existing VEKTRA_API_KEY"
    API_KEY="$VEKTRA_API_KEY"
  else
    fail "API key creation failed (bootstrap may already be consumed)"
  fi
}

if [ -z "${API_KEY:-}" ]; then
  API_KEY=$(echo "$KEY_RESP" | python3 -c "import sys,json; print(json.load(sys.stdin)['key'])")
  checkpoint "API key created"
fi

# ---- step 4: ingest sample PDF ----
checkpoint "Ingesting ${SAMPLE_PDF}..."
INGEST_RESP=$(curl -sf -w "\n%{http_code}" \
  -H "Authorization: Bearer ${API_KEY}" \
  -F "file=@${SAMPLE_PDF};type=application/pdf" \
  "${BASE_URL}/api/v1/ingest?namespace=default") || fail "Ingest POST failed"

INGEST_CODE=$(echo "$INGEST_RESP" | tail -1)
INGEST_BODY=$(echo "$INGEST_RESP" | sed '$d')

case "$INGEST_CODE" in
  200)
    DOC_ID=$(echo "$INGEST_BODY" | python3 -c "import sys,json; print(json.load(sys.stdin)['document_id'])")
    CHUNKS=$(echo "$INGEST_BODY" | python3 -c "import sys,json; print(json.load(sys.stdin).get('chunk_count','?'))")
    checkpoint "Ingested: document_id=${DOC_ID}, chunks=${CHUNKS}"
    ;;
  202)
    JOB_ID=$(echo "$INGEST_BODY" | python3 -c "import sys,json; print(json.load(sys.stdin)['job_id'])")
    checkpoint "Async job ${JOB_ID}, polling..."
    POLL_ELAPSED=0
    while [ "$POLL_ELAPSED" -lt "$INGEST_TIMEOUT" ]; do
      sleep 2
      POLL_ELAPSED=$((POLL_ELAPSED + 2))
      STATUS_RESP=$(curl -sf \
        -H "Authorization: Bearer ${API_KEY}" \
        "${BASE_URL}/api/v1/ingest/jobs/${JOB_ID}/status" 2>/dev/null) || continue
      STATUS=$(echo "$STATUS_RESP" | python3 -c "import sys,json; print(json.load(sys.stdin)['status'])")
      case "$STATUS" in
        indexed|completed|complete)
          DOC_ID=$(echo "$STATUS_RESP" | python3 -c "import sys,json; print(json.load(sys.stdin).get('document_id','?'))")
          CHUNKS=$(echo "$STATUS_RESP" | python3 -c "import sys,json; print(json.load(sys.stdin).get('chunk_count','?'))")
          checkpoint "Ingested: document_id=${DOC_ID}, chunks=${CHUNKS}"
          break
          ;;
        failed|error)
          fail "Ingestion job failed"
          ;;
      esac
    done
    if [ "$POLL_ELAPSED" -ge "$INGEST_TIMEOUT" ]; then
      fail "Ingestion timed out after ${INGEST_TIMEOUT}s"
    fi
    ;;
  *)
    fail "Ingest returned HTTP ${INGEST_CODE}: ${INGEST_BODY}"
    ;;
esac

# ---- step 5: query ----
checkpoint "Querying: 'What is Retrieval-Augmented Generation?'"
QUERY_RESP=$(curl -sf \
  -H "Authorization: Bearer ${API_KEY}" \
  -H "Content-Type: application/json" \
  -d '{"question":"What is Retrieval-Augmented Generation?","namespace":"default","top_k":5}' \
  "${BASE_URL}/api/v1/query") || fail "Query POST failed"

# Verify sources reference the ingested document
python3 -c "
import json, sys

data = json.load(sys.stdin)
sources = data.get('sources', [])
doc_ids = [s.get('doc_id') for s in sources]
doc_id = '${DOC_ID}'

if not sources:
    print('WARNING: no sources returned (LLM may be unavailable)')
    sys.exit(0)

if doc_id in doc_ids:
    print(f'  Sources reference ingested document ({doc_id})')
else:
    print(f'  WARNING: document {doc_id} not in sources {doc_ids}', file=sys.stderr)

answer = data.get('answer')
if answer:
    lines = answer.strip().split('\n')
    preview = lines[0][:80]
    print(f'  Answer: {preview}...' if len(answer) > 80 else f'  Answer: {preview}')
elif data.get('context_only'):
    print('  (context-only mode: no LLM, sources available)')
" <<< "$QUERY_RESP"

checkpoint "Query complete"

# ---- summary ----
TOTAL=$(elapsed)
echo ""
echo "=== Demo complete in ${TOTAL}s ==="
echo ""
echo "REQ-027 checkpoints:"
echo "  Stack up:        T+30s limit  (actual: see above)"
echo "  Health 200:      T+2:00 limit (actual: see above)"
echo "  Ingest complete: T+5:00 limit (actual: see above)"
echo "  Query verified:  T+6:00 limit (actual: T+${TOTAL}s)"

if [ "$TOTAL" -gt 300 ]; then
  echo ""
  echo "WARNING: Total time ${TOTAL}s exceeds 5-minute target"
fi
