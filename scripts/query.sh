#!/usr/bin/env bash
# Vektra query script
# Usage: scripts/query.sh "QUESTION" [CONVERSATION_ID]
set -euo pipefail

BASE_URL="${VEKTRA_API_URL:-http://localhost:${VEKTRA_PORT:-8000}}"

# ---- arg parsing ----
for arg in "$@"; do
  case "$arg" in
    --dry-run)
      echo "[dry-run] Would query ${BASE_URL}/api/v1/query"
      exit 0
      ;;
    -h|--help)
      echo "Usage: scripts/query.sh \"QUESTION\" [CONVERSATION_ID]"
      echo ""
      echo "Query Vektra RAG pipeline."
      echo ""
      echo "Arguments:"
      echo "  QUESTION          The question to ask (required)"
      echo "  CONVERSATION_ID   Continue an existing conversation (optional)"
      echo ""
      echo "Environment:"
      echo "  VEKTRA_API_URL   Base URL (default: http://localhost:8000)"
      echo "  VEKTRA_API_KEY   Bearer token (required)"
      exit 0
      ;;
    --invalid-*|--*)
      echo "Error: unknown argument '$arg'" >&2
      echo "Usage: scripts/query.sh \"QUESTION\" [CONVERSATION_ID]" >&2
      exit 1
      ;;
  esac
done

QUESTION="${1:-}"
CONVERSATION_ID="${2:-}"

# ---- validation ----
if [ -z "$QUESTION" ]; then
  echo "Error: QUESTION argument is required" >&2
  echo "Usage: scripts/query.sh \"QUESTION\" [CONVERSATION_ID]" >&2
  exit 1
fi

if [ -z "${VEKTRA_API_KEY:-}" ]; then
  echo "Error: VEKTRA_API_KEY environment variable is required" >&2
  exit 1
fi

# ---- build JSON body ----
# The server echoes conversation_id back (does not generate one).
# If the caller didn't provide one, generate a new UUID so the
# conversation can be continued in follow-up calls.
if [ -z "$CONVERSATION_ID" ]; then
  CONVERSATION_ID=$(python3 -c "import uuid; print(uuid.uuid4())")
fi

JSON_BODY=$(python3 -c "
import json, sys
print(json.dumps({
    'question': sys.argv[1],
    'conversation_id': sys.argv[2],
    'namespace': 'default',
    'top_k': 5
}))
" "$QUESTION" "$CONVERSATION_ID")

# ---- query ----
echo "Querying: ${QUESTION}"
RESP=$(curl -sf \
  -H "Authorization: Bearer ${VEKTRA_API_KEY}" \
  -H "Content-Type: application/json" \
  -d "$JSON_BODY" \
  "${BASE_URL}/api/v1/query") || {
  echo "Error: query request failed" >&2
  exit 1
}

# ---- display answer ----
python3 -c "
import json, sys

data = json.load(sys.stdin)

# Answer
answer = data.get('answer')
if answer:
    print()
    print(answer)
elif data.get('context_only'):
    print()
    print('(context-only mode: sources available, no LLM synthesis)')
elif data.get('no_relevant_context'):
    print()
    print('No relevant context found for this question.')
else:
    print()
    print('(no answer)')

# Sources
sources = data.get('sources', [])
if sources:
    print()
    print(f'Sources ({len(sources)}):')
    print(f'  {\"doc_id\":<38} {\"score\":>6}  snippet')
    print(f'  {\"-\"*38} {\"-\"*6}  {\"-\"*40}')
    for s in sources:
        doc_id = s.get('doc_id', '?')
        score  = s.get('score', 0)
        snip   = (s.get('snippet') or '')[:60].replace('\n', ' ')
        print(f'  {doc_id:<38} {score:6.3f}  {snip}')

# Conversation ID
cid = data.get('conversation_id')
if cid:
    print()
    print(f'conversation_id: {cid}')
" <<< "$RESP"
