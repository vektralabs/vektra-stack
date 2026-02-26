#!/usr/bin/env bash
# Vektra API key creation script
# Usage: scripts/create-key.sh LABEL [SCOPE1 SCOPE2 ...]
set -euo pipefail

BASE_URL="${VEKTRA_API_URL:-http://localhost:${VEKTRA_PORT:-8000}}"

# ---- arg parsing ----
for arg in "$@"; do
  case "$arg" in
    --dry-run)
      echo "[dry-run] Would create API key at ${BASE_URL}/api/v1/api-keys"
      exit 0
      ;;
    -h|--help)
      echo "Usage: scripts/create-key.sh LABEL [SCOPE1 SCOPE2 ...]"
      echo ""
      echo "Create a Vektra API key."
      echo ""
      echo "Arguments:"
      echo "  LABEL    Key label (required)"
      echo "  SCOPE    One or more scopes: admin, ingest, query"
      echo "           (default: admin)"
      echo ""
      echo "Authentication (one of):"
      echo "  VEKTRA_BOOTSTRAP_KEY   Bootstrap key (first key only)"
      echo "  VEKTRA_API_KEY         Existing admin-scoped key"
      echo ""
      echo "Environment:"
      echo "  VEKTRA_API_URL   Base URL (default: http://localhost:8000)"
      exit 0
      ;;
    --invalid-*|--*)
      echo "Error: unknown argument '$arg'" >&2
      echo "Usage: scripts/create-key.sh LABEL [SCOPE1 SCOPE2 ...]" >&2
      exit 1
      ;;
  esac
done

LABEL="${1:-}"
shift 2>/dev/null || true

# ---- validation ----
if [ -z "$LABEL" ]; then
  echo "Error: LABEL argument is required" >&2
  echo "Usage: scripts/create-key.sh LABEL [SCOPE1 SCOPE2 ...]" >&2
  exit 1
fi

# Determine auth token
if [ -n "${VEKTRA_BOOTSTRAP_KEY:-}" ]; then
  AUTH_TOKEN="$VEKTRA_BOOTSTRAP_KEY"
elif [ -n "${VEKTRA_API_KEY:-}" ]; then
  AUTH_TOKEN="$VEKTRA_API_KEY"
else
  echo "Error: set VEKTRA_BOOTSTRAP_KEY or VEKTRA_API_KEY" >&2
  exit 1
fi

# Build scopes array (default: admin)
if [ $# -eq 0 ]; then
  SCOPES='["admin"]'
else
  SCOPES=$(python3 -c "
import json, sys
print(json.dumps(sys.argv[1:]))
" "$@")
fi

# Build JSON body
JSON_BODY=$(python3 -c "
import json, sys
print(json.dumps({'label': sys.argv[1], 'scopes': json.loads(sys.argv[2])}))
" "$LABEL" "$SCOPES")

# ---- create key ----
echo "Creating API key '${LABEL}'..."
RESP=$(curl -sf -w "\n%{http_code}" \
  -H "Authorization: Bearer ${AUTH_TOKEN}" \
  -H "Content-Type: application/json" \
  -d "$JSON_BODY" \
  "${BASE_URL}/api/v1/api-keys") || {
  echo "Error: API key creation failed" >&2
  exit 1
}

HTTP_CODE=$(echo "$RESP" | tail -1)
BODY=$(echo "$RESP" | sed '$d')

if [ "$HTTP_CODE" != "201" ]; then
  echo "Error: HTTP ${HTTP_CODE}" >&2
  echo "$BODY" >&2
  exit 1
fi

# ---- display result ----
python3 -c "
import json, sys

data = json.load(sys.stdin)
key = data['key']
label = data.get('label', '')
scopes = ', '.join(data.get('scopes', []))
key_id = data.get('id', '?')

print()
print(f'API key created:')
print(f'  id:     {key_id}')
print(f'  label:  {label}')
print(f'  scopes: {scopes}')
print(f'  key:    {key}')
print()
print('WARNING: Save this key now. It will not be shown again.')
" <<< "$BODY"
