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

### GET /api/v1/health

Vector store health as seen by `vektra-index` (unauthenticated). Distinct from `GET /health`: this one checks only the **active** vector store, so it turns unhealthy when the store actually backing the index is unreachable.

```bash
curl -s http://localhost:8000/api/v1/health | python3 -m json.tool
```

```json
{
    "status": "healthy",
    "component": "vektra-index",
    "latency_ms": 1
}
```

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
| `scopes` | string[] | no | Permissions: `admin`, `ingest`, `query`. Defaults to `["admin"]`. Invalid scopes are rejected with `422 ERR-ADMIN-001`. |
| `expires_at` | datetime | no | Expiry timestamp. Must be timezone-aware and in the future, otherwise `422 ERR-ADMIN-004`. Omit for a non-expiring key. |

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
| `citations_enabled` | bool or null | `true`, `false`, `null` | Inline source citations (FEAT-021, advanced pipeline only). When `true`, the LLM is instructed to add `[n]` markers matching the context sources, and each returned source carries a `title` ("filename, p.N") for tooltip rendering. No env var: `null` (or absent) means disabled. Default-off leaves prompts unchanged. |

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

Supported formats: PDF, DOCX, PPTX, Markdown (`text/markdown`). Maximum file size: 50 MB (configurable via `VEKTRA_MAX_FILE_SIZE_MB`).

Sync response (HTTP 200, files <= 10 MB):

```json
{
    "document_id": "550e8400-...",
    "chunk_count": 24,
    "status": "new"
}
```

Sync status values: `new` (extracted, embedded and stored), `exists` (exact duplicate: same content hash and same filename, no work done), `alias` (same content under a different filename: an alias is added to the existing document).

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

### POST /api/v1/ingest/batch

Ingest multiple documents in one multipart request. Every file is processed asynchronously, so the response is always HTTP 202 — there is no synchronous fast path here, unlike `POST /api/v1/ingest`.

**Scopes**: `ingest`, `admin`

```bash
curl -s \
  -H "Authorization: Bearer $VEKTRA_API_KEY" \
  -F "files=@lecture-07.pdf;type=application/pdf" \
  -F "files=@lecture-08.pdf;type=application/pdf" \
  "http://localhost:8000/api/v1/ingest/batch?namespace=default" | python3 -m json.tool
```

Query parameters: `namespace` (default `default`). Files must be sent as repeated `files` fields.

Response (HTTP 202): one entry per submitted file, in submission order.

```json
[
    {"job_id": "e5f6a7b8-...", "filename": "lecture-07.pdf", "status": "pending", "error": null},
    {"job_id": null, "filename": "huge.pdf", "status": "rejected", "error": "File size 73400320 bytes exceeds maximum 52428800 bytes."}
]
```

Per-file status: `pending` (job created, poll it via `GET /api/v1/ingest/jobs/{job_id}/status`), `rejected` (over `VEKTRA_MAX_FILE_SIZE_MB`, no job created), `failed` (job creation failed). A rejected or failed file does not fail the request: the batch still returns 202, so callers must inspect each entry rather than trust the status code. An empty file list returns 422.

### DELETE /api/v1/documents/batch

Delete several documents in one call. Chunks are hard-deleted from the active vector store; the document records are soft-deleted with `deletion_reason='user_request'`.

**Scopes**: `admin`

```bash
curl -s -X DELETE \
  -H "Authorization: Bearer $VEKTRA_API_KEY" \
  -H "Content-Type: application/json" \
  -d '{"document_ids":["550e8400-...","6f1c2d3e-..."],"namespace":"default"}' \
  http://localhost:8000/api/v1/documents/batch | python3 -m json.tool
```

Request body:

| Field | Type | Default | Description |
|-------|------|---------|-------------|
| `document_ids` | UUID[] | (required) | Documents to delete |
| `namespace` | string | `default` | Namespace the documents belong to |

Response (HTTP 200):

```json
{
    "deleted": ["550e8400-..."],
    "not_found": ["6f1c2d3e-..."]
}
```

Unknown ids are reported in `not_found` rather than failing the request.

### Granular pipeline endpoints

`POST /api/v1/ingest/extract`, `POST /api/v1/ingest/chunk` and `POST /api/v1/ingest/embed` run the ingestion pipeline **without storing anything**. They exist to inspect and tune each stage in isolation: what the extractor produced, how the chunker split it, what got embedded. Nothing reaches the vector store, so they are safe to run against production data.

**Scopes**: `ingest`, `admin` (all three)

Each takes a single `file` field (multipart), is synchronous, and rejects files above 10 MB with HTTP 413. Unsupported content types return 422.

| Endpoint | Runs | Returns (JSON array) |
|----------|------|----------------------|
| `POST /api/v1/ingest/extract` | extraction | `{text, element_type, content_format, metadata}` per element |
| `POST /api/v1/ingest/chunk` | extraction + chunking | the same shape, after the chunking strategy |
| `POST /api/v1/ingest/embed` | extraction + chunking + embedding | `{text, dense, element_type, content_format, metadata}` per chunk, `dense` being the embedding vector |

```bash
curl -s \
  -H "Authorization: Bearer $VEKTRA_API_KEY" \
  -F "file=@lecture-07.pdf;type=application/pdf" \
  http://localhost:8000/api/v1/ingest/chunk | python3 -m json.tool
```

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
| `sources[].title` | FEAT-021: human-readable citation label ("filename, p.N") matching the `[n]` markers in the answer. Set only when the namespace has `citations_enabled`; `null` otherwise. |
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

## Conversations

Conversation **content** is never served by these endpoints (REQ-051): they return metadata only. The decrypted turns are available to students through `GET /api/v1/learn/conversations/{id}/turns` (JWT, own course only) and to operators through `GET /api/v1/admin/conversations/{id}/turns` (admin).

Both endpoints require persistent conversation storage. With the in-memory fallback store they return `503`.

### GET /api/v1/conversations/{conversation_id}

Conversation metadata.

**Scopes**: `query`, `admin`

```bash
curl -s \
  -H "Authorization: Bearer $VEKTRA_API_KEY" \
  http://localhost:8000/api/v1/conversations/550e8400-... | python3 -m json.tool
```

Response (HTTP 200):

```json
{
    "id": "550e8400-...",
    "namespace_id": "default",
    "created_at": "2026-07-14T10:00:00Z",
    "updated_at": "2026-07-14T10:05:00Z",
    "turn_count": 3,
    "title": null
}
```

Errors: `404` if the conversation does not exist **or has been soft-deleted**; `503` if conversations are not persisted.

### DELETE /api/v1/conversations/{conversation_id}

Soft-delete a conversation and all its turns (REQ-057). Returns HTTP 204 (no body).

**Scopes**: `query`, `admin`

```bash
curl -s -X DELETE \
  -H "Authorization: Bearer $VEKTRA_API_KEY" \
  http://localhost:8000/api/v1/conversations/550e8400-...
```

Errors: `404` if the conversation does not exist or was already deleted.

A namespace-bound key can only read and delete conversations in its own namespace.

## Feedback

Feedback on an answer (REQ-055). Two levels: the whole response, or one citation within it. Both record the submitting key and namespace.

The `namespace` field in the body is ignored for namespace-bound keys, which always write to their own namespace.

### POST /api/v1/feedback/{response_id}

Rate a response. `response_id` is the value returned by `POST /api/v1/query`.

**Scopes**: `query`, `admin`

```bash
curl -s \
  -H "Authorization: Bearer $VEKTRA_API_KEY" \
  -H "Content-Type: application/json" \
  -d '{"rating":5,"comment":"Accurate and well sourced"}' \
  http://localhost:8000/api/v1/feedback/9f8e7d6c-... | python3 -m json.tool
```

Request body:

| Field | Type | Default | Description |
|-------|------|---------|-------------|
| `rating` | int | (required) | 1 to 5. Outside that range: 422. |
| `comment` | string | `null` | Free-text comment |
| `namespace` | string | `default` | Namespace to attribute the feedback to |

Response (HTTP 201):

```json
{"id": "b7c8d9e0-..."}
```

### POST /api/v1/feedback/citation/{citation_id}

Rate a single citation. `citation_id` is `sources[].citation_id` from the query response; `response_id` is required in the **body** here (the path carries the citation, not the response).

**Scopes**: `query`, `admin`

```bash
curl -s \
  -H "Authorization: Bearer $VEKTRA_API_KEY" \
  -H "Content-Type: application/json" \
  -d '{"response_id":"9f8e7d6c-...","rating":2,"comment":"Wrong page"}' \
  http://localhost:8000/api/v1/feedback/citation/d4e5f6-... | python3 -m json.tool
```

Request body:

| Field | Type | Default | Description |
|-------|------|---------|-------------|
| `response_id` | UUID | (required) | The response the citation belongs to |
| `rating` | int | (required) | 1 to 5 |
| `comment` | string | `null` | Free-text comment |
| `namespace` | string | `default` | Namespace to attribute the feedback to |

Response (HTTP 201): `{"id": "<feedback-id>"}`.

## Learn

The e-learning vertical (`vektra-learn`) exposes course-scoped endpoints authenticated via short-lived JWT dashboard tokens issued by `POST /api/v1/learn/tokens`. The `course_id` claim drives namespace isolation; the LMS-side integration (e.g. the Moodle plugin) is responsible for upstream auth before requesting a token.

Two trust tiers:
- **API key** (called server-side by the LMS): `/tokens`, `/enrollments`, `/content/ingest`. Each endpoint declares the minimum required scope below; `admin` is a project-wide superscope and always satisfies any required scope.
- **JWT** (called from the browser by the widget, with the token issued by `/tokens`): `/query`, `/conversations/{id}/turns`. Namespace is derived from the JWT claims.

### POST /api/v1/learn/tokens

Issue a short-lived JWT dashboard token bound to a `(student_id, course_id)` pair. Called server-side by the LMS once it has authenticated the student.

**Scopes**: `admin`

```bash
curl -s \
  -H "Authorization: Bearer $VEKTRA_API_KEY" \
  -H "Content-Type: application/json" \
  -d '{"student_id":"u123","course_id":"CS101","expires_in":3600}' \
  http://localhost:8000/api/v1/learn/tokens | python3 -m json.tool
```

Request body:

| Field | Type | Default | Description |
|-------|------|---------|-------------|
| `student_id` | string | (required) | Stable student identifier (1–255 chars). |
| `course_id` | string | (required) | Stable course identifier (1–255 chars). |
| `namespace` | string | (course_id) | Namespace embedded in the JWT. Defaults to the course id when omitted. |
| `expires_in` | int | `3600` | Token lifetime in seconds (max `86400`, i.e. 24h). |

Response (HTTP 201):

```json
{
    "token": "eyJhbGciOi...",
    "expires_at": "2026-04-25T15:00:00Z"
}
```

### POST /api/v1/learn/enrollments

Register a student in a course. Optional when `VEKTRA_LEARN_REQUIRE_ENROLLMENT=false` (the LMS is the source of truth for enrollment).

**Scopes**: `ingest`

```bash
curl -s \
  -H "Authorization: Bearer $VEKTRA_API_KEY" \
  -H "Content-Type: application/json" \
  -d '{"student_id":"u123","course_id":"CS101","namespace":"CS101"}' \
  http://localhost:8000/api/v1/learn/enrollments | python3 -m json.tool
```

Request body:

| Field | Type | Description |
|-------|------|-------------|
| `student_id` | string | Stable student identifier (1–255 chars). |
| `course_id` | string | Stable course identifier (1–255 chars). |
| `namespace` | string | Target namespace for course content (1–64 chars). Auto-created if absent. |
| `metadata` | object | Optional free-form metadata. |

Response (HTTP 201): full enrollment record (`id`, `student_id`, `course_id`, `namespace`, `enrolled_at`, `metadata`).

Errors: `409 ERR-LEARN-004` if the same `(student_id, course_id)` is already enrolled.

### GET /api/v1/learn/enrollments

List enrollments. Filter by `course_id`, `student_id`, or both via query parameters.

**Scopes**: `admin`

```bash
curl -s \
  -H "Authorization: Bearer $VEKTRA_API_KEY" \
  "http://localhost:8000/api/v1/learn/enrollments?course_id=CS101&limit=50" | python3 -m json.tool
```

Query parameters: `course_id`, `student_id`, `limit` (1–500, default 50), `offset` (default 0).

Response: `{"items": [...], "count": <int>}`.

### DELETE /api/v1/learn/enrollments/{enrollment_id}

Remove a single enrollment.

**Scopes**: `admin`

Returns HTTP 204 (no body). `404` if the enrollment id does not exist.

### POST /api/v1/learn/content/ingest

Trigger course-scoped ingestion. Used by automation (e.g. an n8n workflow watching a course folder) to push new material into the course namespace without going through the generic `/api/v1/ingest` endpoint.

**Scopes**: `ingest`

```bash
curl -s \
  -H "Authorization: Bearer $VEKTRA_API_KEY" \
  -H "Content-Type: application/json" \
  -d '{"course_id":"CS101","namespace":"CS101","document_url":"https://lms.example/files/lecture-07.pdf"}' \
  http://localhost:8000/api/v1/learn/content/ingest | python3 -m json.tool
```

Request body: `course_id`, `namespace`, optional `document_url`, optional `metadata`. Response echoes the ingestion `status`, `namespace`, `course_id`, `metadata`, and (when synchronous) `document_id` + `chunk_count`.

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
| `sources[].document_name` | Filename of the source document. Soft-deleted documents (REQ-057) keep an `(archived)` suffix so traceability is preserved. The field is `null` when the document join returns no row. |
| `sources[].title` | FEAT-021: citation label ("filename, p.N") for the `[n]` markers, `null` unless the namespace has `citations_enabled`. |

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

### GET /api/v1/documents/{document_id}/chunks

List a document's stored chunks at the active index version, ordered by position. Reads from the
active vector store, which is the only source of truth for chunk text. Includes parent chunks
(FEAT-017), which search excludes. Accepts any valid scope.

`namespace` is optional: a namespace-bound key resolves to its own namespace when it is omitted,
and naming a different one returns 403.

**Scopes**: any

```bash
curl -s \
  -H "Authorization: Bearer $VEKTRA_API_KEY" \
  "http://localhost:8000/api/v1/documents/550e8400-.../chunks?namespace=default" | python3 -m json.tool
```

```json
{
    "document_id": "550e8400-...",
    "namespace": "default",
    "chunks": [
        {
            "chunk_id": "9f1c...",
            "text": "Example chunk text",
            "position": 0,
            "parent_id": null,
            "metadata": {"chunk_level": "parent"}
        }
    ],
    "total": 1
}
```

### DELETE /api/v1/documents/{document_id}

Remove a document's chunks from the active vector store, then soft-delete the document record.
`chunks_removed` is the number of chunks actually removed. Requires `admin` scope.

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

Document and chunk counts, at the **active index version**. Accepts any valid scope.

**Scopes**: any

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

## Reindex

Re-embed a namespace under a **new index version** without downtime (REQ-064, ARCH-045, [ADR-0026](../../.s2s/decisions/ADR-0026-document-chunks-pgvector-internal.md)). Use it after changing the embedding model, the chunking strategy, or any setting that invalidates existing vectors.

The mechanism: chunks are versioned. Every read (search, stats, chunk listing) filters on the **active** index version, set by `VEKTRA_ACTIVE_INDEX_VERSION` (default `1`). A reindex writes a second copy of the chunks under the target version, **alongside** the live ones, which keep serving traffic. The new version becomes live only when the operator changes the env var and restarts. Nothing is deleted along the way, so a bad reindex is rolled back by simply not switching.

The switch is manual: there is **no** API to activate a version, it is an env var plus a restart. The cleanup afterwards is an API call, `DELETE /api/v1/index-versions/{version}` (see [Step 5](#step-5-clean-up-the-old-version)), which refuses to delete whichever version is currently being served.

### POST /api/v1/reindex

Start a reindex job. Returns immediately (HTTP 202); the work runs in the background.

**Scopes**: `admin`

```bash
curl -s \
  -H "Authorization: Bearer $VEKTRA_API_KEY" \
  -H "Content-Type: application/json" \
  -d '{"namespace":"default","target_index_version":2}' \
  http://localhost:8000/api/v1/reindex | python3 -m json.tool
```

Request body:

| Field | Type | Default | Description |
|-------|------|---------|-------------|
| `namespace` | string | `default` | Namespace to reindex. Ignored for a namespace-bound key, which always reindexes its own. |
| `target_index_version` | int | (required) | Version to write. Must be `>= 1` and **differ** from the active version. |

Response (HTTP 202):

```json
{
    "job_id": "0500a873-0c7a-4e42-a8d2-0bd7d77591be",
    "status": "pending",
    "namespace": "default",
    "target_index_version": 2
}
```

The source version is not a parameter: the job always reads the active version, since that is the only version the store exposes for reading.

Errors: `400` if `target_index_version` equals the active version (reindexing a version onto itself would overwrite the live chunks instead of writing beside them).

### GET /api/v1/reindex/{job_id}/status

Poll a reindex job.

**Scopes**: `admin`

```bash
curl -s \
  -H "Authorization: Bearer $VEKTRA_API_KEY" \
  http://localhost:8000/api/v1/reindex/0500a873-.../status | python3 -m json.tool
```

Response (HTTP 200):

```json
{
    "job_id": "0500a873-0c7a-4e42-a8d2-0bd7d77591be",
    "status": "completed",
    "namespace": "default",
    "source_index_version": 1,
    "target_index_version": 2,
    "total_documents": 3,
    "processed_documents": 3,
    "chunks_reindexed": 12,
    "error_message": null,
    "created_at": "2026-07-14T16:13:21.085723+00:00",
    "completed_at": "2026-07-14T16:13:21.248801+00:00"
}
```

| Field | Description |
|-------|-------------|
| `status` | `pending`, `running`, `completed`, `failed` |
| `source_index_version` | Version read from (the active one at trigger time) |
| `total_documents` | Documents in the namespace, excluding soft-deleted ones |
| `processed_documents` | Progress counter |
| **`chunks_reindexed`** | **Chunks actually written under the target version. This is the number that tells a real reindex from an empty one — check it, not just `status`.** |
| `error_message` | Failure reason, `null` on success |

Errors: `404` if the job id is unknown, or belongs to a different namespace than a namespace-bound key.

**Why `chunks_reindexed` matters.** Until BUG-023 the job read chunks from a Postgres table that is empty in Qdrant mode, re-embedded nothing, wrote nothing, and still reported `completed`. An operator polling `status` alone would have seen a green result and switched the active version to an **empty index**. The job now refuses to report success when it wrote nothing: a namespace with documents but zero reindexed chunks fails with `error_message` explaining that the store returned no chunks at the source version. `chunks_reindexed` makes the distinction visible rather than implied — treat a completed job whose `chunks_reindexed` is 0 (against a non-empty namespace) as a bug, not a no-op.

### DELETE /api/v1/index-versions/{index_version}

Reclaim the storage held by a superseded index version (REQ-064). Reindex leaves both versions in the store; this is the only thing that removes one.

**Scopes**: `admin`

```bash
curl -s -X DELETE \
  -H "Authorization: Bearer $VEKTRA_API_KEY" \
  "http://localhost:8000/api/v1/index-versions/1?namespace=default" | python3 -m json.tool
```

| Parameter | In | Default | Description |
|-----------|-----|---------|-------------|
| `index_version` | path | (required) | The version to delete. Must be `>= 1`, and must **not** be the active one. |
| `namespace` | query | `default` | Namespace to clean up. A namespace-bound key may only name its own; naming another returns `403`. |

```json
{
  "namespace": "default",
  "index_version": 1,
  "chunks_removed": 12
}
```

Errors (all in the REQ-010 envelope):

- **`409` `ERR-INDEX-001`** if `index_version` is the version the system is currently serving. **This is the guard that matters.** The store refuses before deleting anything, so the request is a no-op, not a partial wipe. The failure mode it exists to prevent is not "an old version survives", it is "the live index is emptied": get the version number wrong by one and every query stops finding anything. `details` carries the `namespace` and `index_version` that were refused.
- `400` `ERR-INDEX-002` if `index_version` is below 1.
- `403` `ERR-AUTH-003` for a namespace-bound key naming another namespace. The delete is by filter, so a silently retargeted namespace would drop an entire version of a namespace the caller never named.

The call is **idempotent**: deleting a version that is already gone returns `200` with `chunks_removed: 0`. That is the cheapest way to confirm a cleanup really happened, and it is safe to repeat.

It is also the one **irreversible** step in the whole flow. Everything before it can be undone by not switching, or by switching back. Once the old version is deleted, rolling back means reindexing again from scratch.

### End-to-end operator flow

`scripts/reindex.sh TARGET_VERSION [NAMESPACE]` automates steps 1 and 2; `scripts/reindex.sh --cleanup OLD_VERSION [NAMESPACE]` does step 5. They are two separate runs on purpose: between them sits the switch, which only a human can do, and until it happens the API will (correctly) refuse the cleanup.

#### Step 1: trigger

Note the chunk count you expect to move, so you can check it afterwards:

```bash
curl -s -H "Authorization: Bearer $VEKTRA_API_KEY" \
  "http://localhost:8000/api/v1/stats?namespace=default"
# {"document_count":3,"chunk_count":12,"namespace":"default"}

JOB_ID=$(curl -s \
  -H "Authorization: Bearer $VEKTRA_API_KEY" \
  -H "Content-Type: application/json" \
  -d '{"namespace":"default","target_index_version":2}' \
  http://localhost:8000/api/v1/reindex | python3 -c "import sys,json; print(json.load(sys.stdin)['job_id'])")
```

#### Step 2: poll until it settles

```bash
curl -s -H "Authorization: Bearer $VEKTRA_API_KEY" \
  "http://localhost:8000/api/v1/reindex/$JOB_ID/status" | python3 -m json.tool
```

Poll until `status` is `completed` or `failed`. Live traffic is still being served by the old version throughout.

#### Step 3: verify `chunks_reindexed` before switching

This is the gate. **Do not switch on `status: completed` alone.**

```bash
curl -s -H "Authorization: Bearer $VEKTRA_API_KEY" \
  "http://localhost:8000/api/v1/reindex/$JOB_ID/status" \
  | python3 -c "import sys,json; d=json.load(sys.stdin); print(d['status'], d['chunks_reindexed'], 'chunks')"
# completed 12 chunks
```

`chunks_reindexed` should be consistent with the `chunk_count` you noted in step 1. If it is `0`, or far below what you expected, the new version is empty or partial: **do not switch**, investigate. The old version is untouched, so there is nothing to roll back.

#### Step 4: switch the active version

Set the env var and recreate the container (a restart does not reload `.env`):

```bash
# .env
VEKTRA_ACTIVE_INDEX_VERSION=2
```

```bash
docker compose up -d vektra
```

Every read now resolves to version 2. Confirm with a query and with `GET /api/v1/stats`, whose counts are version-scoped and should now reflect the new index.

To roll back, set the variable back to `1` and recreate again: version 1 is still in the store.

#### Step 5: clean up the old version

Until this step, the old version is still in the store: invisible to reads, but occupying exactly as much space as the live one. Every reindex you never clean up doubles the namespace again.

Do it **after** step 4, once the new version has served real traffic and you are no longer going to roll back:

```bash
curl -s -X DELETE \
  -H "Authorization: Bearer $VEKTRA_API_KEY" \
  "http://localhost:8000/api/v1/index-versions/1?namespace=default"
# {"namespace":"default","index_version":1,"chunks_removed":12}
```

Or: `scripts/reindex.sh --cleanup 1 default`.

Ordering is not left to your memory. If you run this **before** the switch, version 1 is still the active one and the API refuses with `409` — the request deletes nothing:

```json
{
  "error": {
    "category": "PERMANENT",
    "code": "ERR-INDEX-001",
    "message": "Refusing to delete index version 1 of namespace 'default': it is the version currently being served. Switch VEKTRA_ACTIVE_INDEX_VERSION to the new version and restart before cleaning up the old one.",
    "remediation": "Switch VEKTRA_ACTIVE_INDEX_VERSION to the new version and restart, then delete the old one. To check which version is live, see VEKTRA_ACTIVE_INDEX_VERSION in the running configuration.",
    "request_id": "550e8400-e29b-41d4-a716-446655440000",
    "details": {"namespace": "default", "index_version": 1}
  }
}
```

That refusal is the whole reason this is an endpoint rather than a store-level delete. Reclaiming space is the small half of the job; the large half is that the obvious hand-written version of it — a Qdrant filter delete, or `DELETE FROM document_chunks WHERE index_version = 1` — has no idea which version is live, and is run by an operator at precisely the moment they are least sure. One wrong number and the live index is empty, with no error and nothing to roll back to.

To confirm it is really gone, run it again: a second call returns `chunks_removed: 0`.

This is the one irreversible step in the flow. Everything before it is undone by not switching, or by switching back.

## Analytics

Operational data, not user-facing (ARCH-041, [ADR-0017](../../.s2s/decisions/ADR-0017-audit-analytics-separation.md)). Every endpoint here requires `admin`.

A `QueryTrace` records what the RAG pipeline did for one response: each step with its duration, the chunks it retrieved with their scores, the model and prompt version. It is written by the query pipeline and is the tool for answering "why was this answer bad?".

### GET /api/v1/traces

List traces, most recent first.

**Scopes**: `admin`

```bash
curl -s \
  -H "Authorization: Bearer $VEKTRA_API_KEY" \
  "http://localhost:8000/api/v1/traces?namespace=default&min_duration_ms=3000&limit=20" \
  | python3 -m json.tool
```

Query parameters:

| Param | Type | Default | Description |
|-------|------|---------|-------------|
| `namespace` | string | all | Filter by namespace |
| `from` | datetime | - | Start of the time window (ISO 8601) |
| `to` | datetime | - | End of the time window (ISO 8601) |
| `model` | string | all | Filter by LLM model |
| `min_duration_ms` | int | - | Only traces slower than this. Useful for hunting latency outliers. |
| `limit` | int | `50` | Page size (1 to 500) |
| `offset` | int | `0` | Pagination offset |

Note the parameter names: `from`, `to` and `model` (not `from_dt`, `to_dt`, `llm_model` — those are the internal Python names).

Response (HTTP 200):

```json
{
    "items": [
        {
            "response_id": "c1d7ffce-...",
            "steps": [
                {"name": "pre_query_safeguard", "duration_ms": 0, "metadata": {"allowed": true}},
                {"name": "embed_query", "duration_ms": 11, "metadata": {}},
                {"name": "llm_call", "duration_ms": 3421, "metadata": {}}
            ],
            "total_duration_ms": 3652,
            "chunks_retrieved": [{"chunk_id": "9f1c...", "score": 0.912}],
            "llm_model": "openai/qwen35-27b-fp8",
            "prompt_version": "v1",
            "created_at": "2026-07-12T09:17:40.010407Z"
        }
    ],
    "count": 1
}
```

`count` is the number of items **on the current page**, not the total number of matching traces. Paginate with `offset` until a page comes back short; do not treat `count` as a total.

### GET /api/v1/traces/{response_id}

A single trace, by the `response_id` returned from `POST /api/v1/query`. Same object as an entry in `items` above.

**Scopes**: `admin`

```bash
curl -s \
  -H "Authorization: Bearer $VEKTRA_API_KEY" \
  http://localhost:8000/api/v1/traces/c1d7ffce-... | python3 -m json.tool
```

Errors: `404 ERR-ANALYTICS-002` if no trace exists for that response id (traces are stored only when trace storage is enabled). `503 ERR-ANALYTICS-001` if the analytics service is still starting.

### GET /api/v1/metrics

Aggregated metrics over a time window. Not to be confused with `GET /metrics`, which is the unauthenticated Prometheus endpoint at the root.

**Scopes**: `admin`

```bash
curl -s \
  -H "Authorization: Bearer $VEKTRA_API_KEY" \
  "http://localhost:8000/api/v1/metrics?namespace=default" | python3 -m json.tool
```

Query parameters: `namespace`, `from`, `to` (all optional; default is all namespaces over all time).

Response (HTTP 200):

```json
{
    "total_queries": 67,
    "avg_latency_ms": 3652.23,
    "p95_latency_ms": 5985.2,
    "avg_retrieval_score": 0.6675,
    "queries_per_hour": 0.03,
    "model_distribution": {"openai/qwen35-27b-fp8": 7, "qwen36-35b-a3b-fp8": 38},
    "period_start": "2026-03-28T10:38:28.227883Z",
    "period_end": "2026-07-12T09:17:40.010407Z"
}
```

| Field | Description |
|-------|-------------|
| `avg_retrieval_score` | Mean similarity score of retrieved chunks. A drop here is a retrieval-quality regression, and it moves before answer quality visibly does. |
| `queries_per_hour` | Throughput over the window |
| `model_distribution` | Query count per model. Entries can look duplicated (`qwen35-27b-fp8` and `openai/qwen35-27b-fp8`) because the model string is recorded as configured; a provider prefix change produces a new bucket. |
| `period_start` / `period_end` | Actual window covered by the data, not the requested one |

## Admin

### GET /api/v1/admin/conversations/{conversation_id}/turns

Decrypted conversation turns with full metadata (DEBT-011). Conversation content is encrypted at rest (pgcrypto, [ADR-0011](../../.s2s/decisions/ADR-0011-conversation-encryption.md)); this is the operator path to read it. Students read their own history through the JWT-authenticated `GET /api/v1/learn/conversations/{id}/turns`, which returns no metadata.

**Scopes**: `admin`

```bash
curl -s \
  -H "Authorization: Bearer $VEKTRA_API_KEY" \
  http://localhost:8000/api/v1/admin/conversations/550e8400-.../turns | python3 -m json.tool
```

Response (HTTP 200): a JSON array (not an object).

```json
[
    {
        "turn_number": 1,
        "question": "What is RAG?",
        "answer": "RAG is ...",
        "response_id": "9f8e7d6c-...",
        "model": null,
        "prompt_tokens": null,
        "completion_tokens": null,
        "created_at": "2026-07-14T10:00:00Z"
    }
]
```

`answer` is `null` for a turn still in flight (the question is recorded before the LLM responds). `model`, `prompt_tokens` and `completion_tokens` are currently always `null`: the columns exist but nothing populates them yet (DEBT-012).

Errors: `404` if the conversation does not exist. `501` if the conversation store cannot decrypt (in-memory store: set `VEKTRA_CONVERSATION_KEY` for a real deployment). `503` if the store is unavailable.

**Audit (NFR-007)**: every read writes a `conversation_turns_read` audit row with the namespace, conversation id and turn count.

### GET /admin

Redirects (HTTP 308) to `/admin/`, the HTMX dashboard ([ADR-0024](../../.s2s/decisions/ADR-0024-admin-ui-server-side.md)).

The dashboard and its routes (`/admin/keys`, `/admin/namespaces`, `/admin/audit`, `/admin/config`) are a **browser UI, not an API**: they authenticate with a session cookie obtained from `/admin/login`, not with a Bearer token. Do not script against them — use the `/api/v1/*` endpoints above.

## Observability

### GET /metrics

Prometheus metrics (unauthenticated). For aggregated RAG analytics as JSON, see [`GET /api/v1/metrics`](#get-apiv1metrics) instead.

```bash
curl -s http://localhost:8000/metrics
```

## Error responses

Most errors follow a standard envelope (REQ-010):

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

Not every endpoint uses the envelope. `POST /api/v1/reindex`, `GET /api/v1/reindex/{job_id}/status`, conversations and admin conversation turns return FastAPI's bare shape instead:

```json
{"detail": "Reindex job not found"}
```

Clients that parse errors must handle both. The envelope is the intended contract; the bare form is a gap (DEBT-033), not a second contract to rely on. `DELETE /api/v1/index-versions/{version}` uses the envelope throughout, including its `409`.
