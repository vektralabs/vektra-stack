# Quick start

Get Vektra running and execute your first RAG query. Total time: under 30 minutes.

## Prerequisites

- Docker Engine 24+ with Docker Compose v2
- GNU Make (for `make health`, `make demo`, etc.) or run the equivalent commands directly
- 4 GB free RAM (embedding model loads at startup)
- An LLM provider: either [Ollama](https://ollama.com) running locally, or an OpenAI / Anthropic API key

## 1. Clone and configure

```bash
git clone https://github.com/vektralabs/vektra-stack.git
cd vektra-stack
cp .env.example .env
```

Edit `.env` and set the two required variables:

```bash
# Pick ONE LLM provider:
VEKTRA_LLM_PROVIDER=ollama/llama3           # local Ollama
# VEKTRA_LLM_PROVIDER=openai/gpt-4o         # OpenAI (set OPENAI_API_KEY below)
# VEKTRA_LLM_PROVIDER=anthropic/claude-sonnet-4-20250514  # Anthropic (set ANTHROPIC_API_KEY below)

# Set the bootstrap key (used once to create your first API key):
VEKTRA_ADMIN_BOOTSTRAP_KEY=change-me-on-first-deploy
```

If using a cloud LLM, also set the matching API key:

```bash
OPENAI_API_KEY=sk-...
# or
ANTHROPIC_API_KEY=sk-ant-...
```

## 2. Start the stack

```bash
docker compose up -d
```

With Ollama (local LLM):

```bash
docker compose --profile local-llm up -d
```

First start downloads the embedding model (~80 MB) and may take a few minutes.

### Alternative: pull a published image

The steps above build the image from your git checkout. For a production host, pull a
versioned release instead of building locally:

```bash
cp deploy/docker-compose.image.yml.example docker-compose.image.yml
echo "VEKTRA_VERSION=0.7.0" >> .env   # pick a published tag; add -ocr for the OCR variant
echo "COMPOSE_FILE=docker-compose.yml:docker-compose.image.yml" >> .env
docker compose pull
docker compose up -d
```

`COMPOSE_FILE` makes every compose command merge both files, so `pull`, `up`, `ps` and
`logs` all work without repeating `-f`.

To update later: `docker compose pull && docker compose up -d`.
Published tags: [ghcr.io/vektralabs/vektra](https://github.com/vektralabs/vektra-stack/pkgs/container/vektra).

## 3. Wait for health

```bash
make health
```

Or manually:

```bash
curl -s http://localhost:8000/health | python3 -m json.tool
```

Expected output:

```json
{
    "status": "healthy",
    "timestamp": "2026-01-01T00:00:00Z"
}
```

If you see `"status": "degraded"`, the LLM provider may still be starting. The ingest and query APIs are available once the health endpoint responds (even with status `degraded`).

## 4. Create your first API key

```bash
curl -s \
  -H "Authorization: Bearer change-me-on-first-deploy" \
  -H "Content-Type: application/json" \
  -d '{"label":"my-first-key","scopes":["admin","ingest","query"]}' \
  http://localhost:8000/api/v1/api-keys | python3 -m json.tool
```

Save the `key` value from the response. This is the only time the full key is shown.

```bash
export VEKTRA_API_KEY=<paste-key-here>
```

The bootstrap key is consumed after the first successful use and cannot be reused.

## 5. Ingest a document

```bash
curl -s \
  -H "Authorization: Bearer $VEKTRA_API_KEY" \
  -F "file=@tests/fixtures/sample.pdf;type=application/pdf" \
  "http://localhost:8000/api/v1/ingest?namespace=default" | python3 -m json.tool
```

The response includes a `document_id` and `chunk_count`:

```json
{
    "document_id": "550e8400-e29b-41d4-a716-446655440000",
    "chunk_count": 12,
    "status": "new"
}
```

Files larger than 10 MB are processed asynchronously and return a `job_id` instead. See [ingest API reference](../reference/api.md#post-apiv1ingest) for polling.

## 6. Query

```bash
curl -s \
  -H "Authorization: Bearer $VEKTRA_API_KEY" \
  -H "Content-Type: application/json" \
  -d '{"question":"What is Retrieval-Augmented Generation?","namespace":"default","top_k":5}' \
  http://localhost:8000/api/v1/query | python3 -m json.tool
```

The response includes the answer and source citations:

```json
{
    "response_id": "...",
    "answer": "Retrieval-Augmented Generation (RAG) is ...",
    "sources": [
        {
            "doc_id": "550e8400-...",
            "chunk_id": "c1a2b3...",
            "score": 0.847,
            "snippet": "RAG combines retrieval...",
            "citation_id": "d4e5f6-...",
            "document_version": 1
        }
    ],
    "conversation_id": null,
    "context_only": false,
    "no_relevant_context": false
}
```

## Automated demo

Run all the above steps automatically:

```bash
make demo
```

This executes the full workflow: start stack, wait for health, create API key, ingest `tests/fixtures/sample.pdf`, and verify a query returns sources referencing the ingested document.

## Using the shell scripts

The `scripts/` directory provides operator scripts for common tasks:

```bash
scripts/health.sh                               # Check health
scripts/create-key.sh my-key admin ingest query  # Create an API key
scripts/ingest.sh document.pdf                   # Ingest a file
scripts/query.sh "What is RAG?"                  # Query
```

All scripts read `VEKTRA_API_KEY` from the environment. Run any script with `--help` for details.

## Troubleshooting

### `ERR-CONFIG-001`: Missing or invalid configuration variable

The server logs show which variable is missing. At minimum, `VEKTRA_LLM_PROVIDER` must be set. Check your `.env` file.

### `ERR-CONFIG-002`: Invalid provider configuration

The LLM provider string is not recognized by litellm. Use the format `provider/model`, for example `ollama/llama3` or `openai/gpt-4o`.

### Health endpoint returns 503

The stack is starting. Wait up to 60 seconds for the embedding model to load. If it persists, check `docker compose logs vektra` for startup errors.

### Bootstrap key rejected

The bootstrap key can only be used once. If you already created a key, use that key with admin scope to create additional keys. Set `VEKTRA_API_KEY` to your existing admin key.

### Ingest returns 413

The file exceeds the configured maximum (default: 50 MB). Split the file or increase `VEKTRA_MAX_FILE_SIZE_MB` in `.env`.

### Port already in use

Another process is using port 8000. Either stop it or set `VEKTRA_PORT=8001` in `.env` and restart.
