#!/usr/bin/env bash
# Vektra batch document ingestion script
# Usage: scripts/batch-ingest.sh DIR [NAMESPACE]
set -euo pipefail

BASE_URL="${VEKTRA_API_URL:-http://localhost:${VEKTRA_PORT:-8000}}"

# ---- arg parsing ----
for arg in "$@"; do
  case "$arg" in
    --dry-run)
      echo "[dry-run] Would batch-ingest files to ${BASE_URL}/api/v1/ingest"
      exit 0
      ;;
    -h|--help)
      echo "Usage: scripts/batch-ingest.sh DIR [NAMESPACE]"
      echo ""
      echo "Ingest all supported documents in a directory."
      echo ""
      echo "Arguments:"
      echo "  DIR         Directory containing documents (required)"
      echo "  NAMESPACE   Target namespace (default: 'default')"
      echo ""
      echo "Supported formats: .pdf, .docx, .pptx"
      echo ""
      echo "Environment:"
      echo "  VEKTRA_API_URL   Base URL (default: http://localhost:8000)"
      echo "  VEKTRA_API_KEY   Bearer token (required)"
      exit 0
      ;;
    --invalid-*|--*)
      echo "Error: unknown argument '$arg'" >&2
      echo "Usage: scripts/batch-ingest.sh DIR [NAMESPACE]" >&2
      exit 1
      ;;
  esac
done

DIR="${1:-}"
NAMESPACE="${2:-default}"

# ---- validation ----
if [ -z "$DIR" ]; then
  echo "Error: DIR argument is required" >&2
  echo "Usage: scripts/batch-ingest.sh DIR [NAMESPACE]" >&2
  exit 1
fi

if [ ! -d "$DIR" ]; then
  echo "Error: directory not found: $DIR" >&2
  exit 1
fi

if [ -z "${VEKTRA_API_KEY:-}" ]; then
  echo "Error: VEKTRA_API_KEY environment variable is required" >&2
  exit 1
fi

# ---- find supported files ----
SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
FILES=()
while IFS= read -r -d '' file; do
  FILES+=("$file")
done < <(find "$DIR" -maxdepth 1 -type f \( -name "*.pdf" -o -name "*.docx" -o -name "*.pptx" \) -print0 | sort -z)

if [ ${#FILES[@]} -eq 0 ]; then
  echo "No supported files (.pdf, .docx, .pptx) found in $DIR"
  exit 0
fi

echo "Found ${#FILES[@]} file(s) in '$DIR', ingesting into namespace '${NAMESPACE}'..."
echo ""

# ---- ingest each file ----
SUCCESS=0
FAILED=0
for file in "${FILES[@]}"; do
  FILENAME=$(basename "$file")
  printf "  %-40s " "$FILENAME"
  if output=$("$SCRIPT_DIR/ingest.sh" "$file" "$NAMESPACE" 2>&1); then
    echo "OK"
    SUCCESS=$((SUCCESS + 1))
  else
    echo "FAILED"
    echo "    ${output//$'\n'/$'\n'    }" >&2
    FAILED=$((FAILED + 1))
  fi
done

echo ""
echo "Batch complete: ${SUCCESS} succeeded, ${FAILED} failed (${#FILES[@]} total)"
[ "$FAILED" -eq 0 ] || exit 1
