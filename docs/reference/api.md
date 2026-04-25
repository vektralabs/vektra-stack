# API reference

All examples use `$VEKTRA_API_KEY` as the Bearer token. Replace with your actual API key.

Base URL: `http://localhost:8000` (configurable via `VEKTRA_PORT`).

Interactive API documentation is available at `/docs` (Swagger UI).

## Authentication

All endpoints except `GET /health` (shallow) require a Bearer token:

```text
Authorization: Bearer <api-key>
```

API keys have scopes: `admin`, `ingest`, `query`. Each endpoint requires specific scopes as noted below.

## Health

### GET /health

Shallow health check (unauthenticated).

```bash
curl -s http://localhost:8000/health | python3 -m json.tool
```

```json
{
    "status": "healthy",
    "timestamp": "2026-01-01T00:00:00Z"
}
```

Status values: `healthy`, `degraded`, `unhealthy`. Returns HTTP 200 for healthy/degraded, 503 for unhealthy.

### GET /health?detail=full

Deep health check with component breakdown. Requires any valid Bearer token.

```bash
curl -s \
  -H "Authorization: Bearer $VEKTRA_API_KEY" \
  "http://localhost:8000/health?detail=full" | python3 -m json.tool
```

### GET /health/memory

Process memory statistics. Requires any valid Bearer token.

```bash
curl -s \
  -H "Authorization: Bearer $VEKTRA_API_KEY" \
  http://localhost:8000/health/memory | python3 -m json.tool
```

### GET /health/{component}

Single component health. Requires any valid Bearer token.

```bash
curl -s \
  -H "Authorization: Bearer $VEKTRA_API_KEY" \
  http://localhost:8000/health/llm | python3 -m json.tool
```

```json
{
    "name": "llm",
    "status": "healthy",
    "latency_ms": 42,
    "message": null
}
```

Components: `llm`, `embedding`, `vector_store`.

## API keys

### POST /api/v1/api-keys

Create a new API key. Requires `admin` scope or the bootstrap key (first use only).

**Scopes**: `admin` (or bootstrap key)

```bash
curl -s \
  -H "Authorization: Bearer $VEKTRA_API_KEY" \
  -H "Content-Type: application/json" \
  -d '{"label":"my-key","scopes":["admin","ingest","query"]}' \
  http://localhost:8000/api/v1/api-keys | python3 -m json.tool
```

Request body:

| Field | Type | Required | Description |
|-------|------|----------|-------------|
| `label` | string | no | Human-readable label |
| `scopes` | string[] | no | Permissions: `admin`, `ingest`, `query`. Defaults to `["admin"]`. |

Response (HTTP 201):

```json
{
    "id": "550e8400-...",
    "key": "vk_abc123...",
    "key_preview": "vk_abc...xyz",
    "label": "my-key",
    "scopes": ["admin", "ingest", "query"],
    "created_at": "2026-01-01T00:00:00Z"
}
```

The `key` field is shown only once. Store it securely.

### GET /api/v1/api-keys

List all API keys (metadata only). Requires `admin` scope.

**Scopes**: `admin`

```bash
curl -s \
  -H "Authorization: Bearer $VEKTRA_API_KEY" \
  http://localhost:8000/api/v1/api-keys | python3 -m json.tool
```

### DELETE /api/v1/api-keys/{key_id}

Revoke an API key. Requires `admin` scope.

**Scopes**: `admin`

```bash
curl -s -X DELETE \
  -H "Authorization: Bearer $VEKTRA_API_KEY" \
  http://localhost:8000/api/v1/api-keys/550e8400-...
```

Returns HTTP 204 (no body).

## Namespaces

### GET /api/v1/admin/namespaces/{namespace_id}/config

Read a namespace's stored config and the effective values after env-default fallback. Symmetric to the PATCH endpoint below.

**Scopes**: `admin`

```bash
curl -s -H "Authorization: Bearer $VEKTRA_API_KEY" \
  http://localhost:8000/api/v1/admin/namespaces/default/config | python3 -m json.tool
```

Response (HTTP 200):

```json
{
    "namespace_id": "default",
    "config": {"grounding_mode": "hybrid"},
    "resolved": {
        "grounding_mode": "hybrid",
        "show_sources": true
    }
}
```

- `config` mirrors the raw JSONB stored in `namespaces.config` (`{}` when no key has ever been set).
- `resolved` is the value queries actually observe at runtime, after the namespace > env > hardcoded-default chain. Always includes every key in the whitelist. Use this for "Use default" / "Override" form rendering in upstream plugins (e.g. the Moodle block edit form).

Errors:
- `404 ERR-ADMIN-005` if the namespace does not exist.

### PATCH /api/v1/admin/namespaces/{namespace_id}/config

Partially update a namespace's behavioral config (JSONB). Requires `admin` scope.

**Scopes**: `admin`

Used by upstream plugins (e.g. the Moodle block) to toggle per-course settings such as the grounding mode without touching environment variables. The read path is `resolve_grounding_mode()` in `vektra-shared/src/vektra_shared/namespace.py`.

```bash
curl -s -X PATCH \
  -H "Authorization: Bearer $VEKTRA_API_KEY" \
  -H "Content-Type: application/json" \
  -d '{"grounding_mode":"hybrid"}' \
  http://localhost:8000/api/v1/admin/namespaces/default/config | python3 -m json.tool
```

Request body (flat dict, one entry per config key):

| Field | Type | Allowed values | Description |
|-------|------|----------------|-------------|
| `grounding_mode` | string or null | `"strict"`, `"hybrid"`, `null` | RAG grounding policy. `null` removes the key and falls back to `VEKTRA_PROMPT_GROUNDING_MODE`. |
| `show_sources` | bool or null | `true`, `false`, `null` | Widget citation visibility (FEAT-014). `null` removes the key and falls back to `VEKTRA_LEARN_SHOW_SOURCES`. The API always returns the full sources list; the flag only instructs the widget whether to render them. Resolution chain: client `data-show-sources` attr > `namespaces.config.show_sources` > `VEKTRA_LEARN_SHOW_SOURCES` env > hardcoded `true`. |

Behavior:
- **Partial update**: keys not present in the body are preserved.
- **Unknown keys rejected** with `400 ERR-ADMIN-006` (not silently ignored — surfaces typos early).
- **Invalid values rejected** with `400 ERR-ADMIN-007` (includes type mismatches, e.g. a JSON string passed for a boolean key).
- **Missing namespace** returns `404 ERR-ADMIN-005`.

Response (HTTP 200):

```json
{
    "namespace_id": "default",
    "config": {"grounding_mode": "hybrid", "show_sources": false}
}
```

## Ingest

### POST /api/v1/ingest

Upload and process a document. Requires `ingest` or `admin` scope.

**Scopes**: `ingest`, `admin`

```bash
curl -s \
  -H "Authorization: Bearer $VEKTRA_API_KEY" \
  -F "file=@document.pdf;type=application/pdf" \
  "http://localhost:8000/api/v1/ingest?namespace=default" | python3 -m json.tool
```

Query parameters:

| Param | Type | Default | Description |
|-------|------|---------|-------------|
| `namespace` | string | `default` | Target namespace for the document |

Supported formats: PDF, DOCX, PPTX. Maximum file size: 50 MB (configurable via `VEKTRA_MAX_FILE_SIZE_MB`).

Sync response (HTTP 200, files <= 10 MB):

```json
{
    "document_id": "550e8400-...",
    "chunk_count": 24,
    "status": "indexed"
}
```

Async response (HTTP 202, files > 10 MB):

```json
{
    "job_id": "e5f6a7b8-...",
    "status": "processing"
}
```

Error responses: 409 (duplicate filename with different content), 413 (file too large), 422 (unsupported format or scanned PDF).

### GET /api/v1/ingest/jobs/{job_id}/status

Poll async ingest job status. Requires `ingest` or `admin` scope.

**Scopes**: `ingest`, `admin`

```bash
curl -s \
  -H "Authorization: Bearer $VEKTRA_API_KEY" \
  http://localhost:8000/api/v1/ingest/jobs/e5f6a7b8-.../status | python3 -m json.tool
```

```json
{
    "id": "e5f6a7b8-...",
    "status": "indexed",
    "phase": "complete",
    "document_id": "550e8400-...",
    "chunk_count": 24,
    "error_code": null,
    "error_message": null,
    "percentage": 100
}
```

Status values: `processing`, `indexed`, `failed`.

## Query

### POST /api/v1/query

RAG query with LLM-generated answer. Requires `query` or `admin` scope.

**Scopes**: `query`, `admin`

```bash
curl -s \
  -H "Authorization: Bearer $VEKTRA_API_KEY" \
  -H "Content-Type: application/json" \
  -d '{
    "question": "What is RAG?",
    "namespace": "default",
    "top_k": 5
  }' \
  http://localhost:8000/api/v1/query | python3 -m json.tool
```

Request body:

| Field | Type | Default | Description |
|-------|------|---------|-------------|
| `question` | string | (required) | Query text (max 10,000 characters) |
| `namespace` | string | `default` | Namespace to search |
| `top_k` | int | `5` | Number of chunks to retrieve |
| `stream` | bool | `false` | Enable Server-Sent Events streaming |
| `conversation_id` | UUID | - | Continue an existing conversation |

Response:

```json
{
    "response_id": "9f8e7d6c-...",
    "answer": "RAG is ...",
    "sources": [
        {
            "doc_id": "550e8400-...",
            "chunk_id": "c1a2b3...",
            "score": 0.912,
            "snippet": "...",
            "citation_id": "d4e5f6-...",
            "document_version": 1,
            "document_name": "lecture-07.pdf"
        }
    ],
    "conversation_id": null,
    "context_only": false,
    "no_relevant_context": false
}
```

| Field | Description |
|-------|-------------|
| `response_id` | Unique response identifier |
| `answer` | LLM-generated answer grounded in sources |
| `sources` | Ranked list of source chunks |
| `sources[].document_name` | Filename of the source document (e.g. `lecture-07.pdf`), or `null` when the document join returns no row. Soft-deleted documents (REQ-057) keep their citation with an `(archived)` suffix so traceability is preserved. |
| `conversation_id` | Echoed back if provided in request |
| `context_only` | `true` if LLM failed and raw sources returned |
| `no_relevant_context` | `true` if no chunks exceeded relevance threshold |

#### Streaming

Set `stream: true` or send `Accept: text/event-stream`:

```bash
curl -s -N \
  -H "Authorization: Bearer $VEKTRA_API_KEY" \
  -H "Content-Type: application/json" \
  -H "Accept: text/event-stream" \
  -d '{"question":"What is RAG?","namespace":"default","stream":true}' \
  http://localhost:8000/api/v1/query
```

### GET /api/v1/providers

List registered LLM providers with health status. Requires `query` or `admin` scope.

**Scopes**: `query`, `admin`

```bash
curl -s \
  -H "Authorization: Bearer $VEKTRA_API_KEY" \
  http://localhost:8000/api/v1/providers | python3 -m json.tool
```

## Learn

The e-learning vertical (`vektra-learn`) exposes course-scoped endpoints authenticated via short-lived JWT dashboard tokens issued by `POST /api/v1/learn/tokens`. The `course_id` claim drives namespace isolation; the LMS-side integration (e.g. the Moodle plugin) is responsible for upstream auth before requesting a token.

### POST /api/v1/learn/query

Course-scoped RAG query. Authenticated via JWT (not API key). The namespace is derived from the token's `namespace` (or `course_id` fallback) claim.

```bash
curl -s \
  -H "Authorization: Bearer $LEARN_JWT" \
  -H "Content-Type: application/json" \
  -d '{"question":"What did we cover in lecture 7?"}' \
  http://localhost:8000/api/v1/learn/query | python3 -m json.tool
```

Request body:

| Field | Type | Default | Description |
|-------|------|---------|-------------|
| `question` | string | (required) | Query text |
| `conversation_id` | UUID | - | Continue an existing conversation |
| `top_k` | int | `5` | Number of chunks to retrieve |
| `stream` | bool | `false` | Enable Server-Sent Events streaming |

Response (HTTP 200, JSON):

```json
{
    "response_id": "9f8e7d6c-...",
    "answer": "Lecture 7 covered ...",
    "sources": [
        {
            "doc_id": "550e8400-...",
            "chunk_id": "c1a2b3...",
            "score": 0.912,
            "snippet": "...",
            "document_name": "lecture-07.pdf"
        }
    ],
    "conversation_id": "550e8400-...",
    "no_relevant_context": false,
    "show_sources": true
}
```

| Field | Description |
|-------|-------------|
| `show_sources` | Server-resolved citation-visibility hint for the widget (FEAT-014). The full `sources` list is always returned regardless; the widget uses the flag to decide whether to render the citations block. See the resolution chain in [Namespaces PATCH](#patch-apiv1adminnamespacesnamespace_idconfig). |
| `sources[].document_name` | Filename of the source document, with `(archived)` suffix for soft-deleted documents. May be `null` when the document join returns no row. |

#### Streaming

Set `stream: true` or send `Accept: text/event-stream`:

```bash
curl -s -N \
  -H "Authorization: Bearer $LEARN_JWT" \
  -H "Content-Type: application/json" \
  -H "Accept: text/event-stream" \
  -d '{"question":"What did we cover in lecture 7?","stream":true}' \
  http://localhost:8000/api/v1/learn/query
```

The `sources` SSE event payload carries the same `show_sources` flag alongside the sources list.

### GET /api/v1/learn/conversations/{conversation_id}/turns

Return the decrypted turns of a conversation belonging to the token's course. Used by the widget to restore history after a page reload (FEAT-004).

**Auth**: JWT dashboard token (same as `/learn/query`). The endpoint enforces `conversation.namespace_id == jwt.namespace`; a mismatch returns 403 (so the widget can distinguish "wrong course" from "deleted conversation" and reset its local state).

```bash
curl -s \
  -H "Authorization: Bearer $LEARN_JWT" \
  http://localhost:8000/api/v1/learn/conversations/550e8400-.../turns | python3 -m json.tool
```

Response (HTTP 200):

```json
{
    "conversation_id": "550e8400-...",
    "namespace": "course-101",
    "turns": [
        {
            "turn_number": 1,
            "question": "What is RAG?",
            "answer": "RAG is ...",
            "created_at": "2026-04-25T14:00:00Z",
            "sources": []
        }
    ]
}
```

The `turns[].sources` array is empty in v0.5.0 (admin-only metadata such as model, prompt tokens, and source enrichment from `query_traces` is intentionally not exposed to students). The `turns[].answer` field may be `null` for a turn that is still in-flight (the question is recorded immediately, the answer is filled in when the LLM completes).

Errors:
- `404 ERR-LEARN-005` if the conversation does not exist or has been deleted.
- `403 ERR-LEARN-006` if the conversation belongs to a different course/namespace.
- `503 ERR-LEARN-001` if the conversation store is not yet initialized or does not support decryption (e.g. in-memory store; production deployments must set `VEKTRA_CONVERSATION_KEY`).

**Audit (NFR-007)**: every successful read writes a `learn_conversation_turns_read` audit row carrying `namespace`, `conversation_id`, `turn_count`, `student_id`, and `course_id`. The audit row is generated even if the upstream middleware did not set a request id.

## Index

### POST /api/v1/search

Semantic search over indexed chunks (vector store only, no LLM). Requires `query` scope.

**Scopes**: `query`

```bash
curl -s \
  -H "Authorization: Bearer $VEKTRA_API_KEY" \
  -H "Content-Type: application/json" \
  -d '{
    "query": "machine learning",
    "namespace": "default",
    "top_k": 10
  }' \
  http://localhost:8000/api/v1/search | python3 -m json.tool
```

Request body:

| Field | Type | Default | Description |
|-------|------|---------|-------------|
| `query` | string | (required) | Search text |
| `namespace` | string | `default` | Namespace to search |
| `top_k` | int | `5` | Number of results (1-100) |
| `search_mode` | string | `dense` | Search mode |
| `filters` | object | - | JSONB metadata filters |

Response:

```json
{
    "results": [
        {
            "chunk_id": "...",
            "document_id": "550e8400-...",
            "score": 0.912,
            "text_snippet": "...",
            "document_version": 1,
            "metadata": {}
        }
    ],
    "total": 10
}
```

### POST /api/v1/documents/{document_id}/chunks

Store embeddings for document chunks (used by ingestion pipeline). Requires `ingest` scope.

**Scopes**: `ingest`

```bash
curl -s \
  -H "Authorization: Bearer $VEKTRA_API_KEY" \
  -H "Content-Type: application/json" \
  -d '{
    "chunks": [
      {
        "chunk_id": "c1",
        "text": "Example chunk text",
        "dense": [0.1, 0.2, 0.3],
        "metadata": {}
      }
    ],
    "namespace": "default"
  }' \
  http://localhost:8000/api/v1/documents/550e8400-.../chunks | python3 -m json.tool
```

### DELETE /api/v1/documents/{document_id}

Delete all chunks for a document and soft-delete the document record. Requires `admin` scope.

**Scopes**: `admin`

```bash
curl -s -X DELETE \
  -H "Authorization: Bearer $VEKTRA_API_KEY" \
  "http://localhost:8000/api/v1/documents/550e8400-...?namespace=default" | python3 -m json.tool
```

```json
{
    "document_id": "550e8400-...",
    "chunks_removed": 24
}
```

### GET /api/v1/stats

Document and chunk counts. Requires `query` scope.

**Scopes**: `query`

```bash
curl -s \
  -H "Authorization: Bearer $VEKTRA_API_KEY" \
  "http://localhost:8000/api/v1/stats?namespace=default" | python3 -m json.tool
```

```json
{
    "document_count": 3,
    "chunk_count": 72,
    "namespace": "default"
}
```

Omit `namespace` to get totals across all namespaces.

## Admin

### GET /admin

Minimal HTML dashboard showing health status. Requires any valid Bearer token.

```bash
curl -s \
  -H "Authorization: Bearer $VEKTRA_API_KEY" \
  http://localhost:8000/admin
```

## Observability

### GET /metrics

Prometheus metrics (unauthenticated).

```bash
curl -s http://localhost:8000/metrics
```

## Error responses

All errors follow a standard envelope (REQ-010):

```json
{
    "error": {
        "category": "PERMANENT",
        "code": "ERR-AUTH-001",
        "message": "Token is missing, malformed, unrecognized, or revoked.",
        "remediation": "Include a valid API key as Bearer token.",
        "request_id": "550e8400-...",
        "details": {}
    }
}
```

See [error codes reference](error-codes.md) for the complete list.
