# Error code registry

All API errors follow the REQ-010 envelope schema and include actionable remediation guidance (REQ-009).

## Error response envelope (REQ-010)

Every error response uses this shape:

```json
{
  "error": {
    "category": "PERMANENT",
    "code": "ERR-AUTH-001",
    "message": "Authentication token is missing, malformed, unrecognized, or revoked.",
    "remediation": "Include a valid API key as a Bearer token in the Authorization header.",
    "request_id": "550e8400-e29b-41d4-a716-446655440000",
    "details": {}
  }
}
```

**Fields**:
- `category`: one of `TRANSIENT`, `PERMANENT`, `CONFIGURATION`, `UPSTREAM` (BR-001)
- `code`: stable identifier in the format `ERR-{COMPONENT}-{NUMBER}`
- `message`: diagnostic, what happened
- `remediation`: prescriptive, what to do next (never empty per NFR-009)
- `request_id`: correlation ID matching the `X-Request-ID` response header
- `retry_after`: seconds to wait before retrying (only for TRANSIENT errors)
- `details`: additional structured context (optional)

## Error categories (BR-001)

| Category | HTTP status (default) | Meaning |
|----------|----------------------|---------|
| TRANSIENT | 503 | Retry may succeed |
| PERMANENT | 422 | Request needs modification |
| CONFIGURATION | 500 | System misconfiguration |
| UPSTREAM | 502 | External dependency failure |

## Normative error codes (REQ-011)

### Authentication errors

| Code | Category | HTTP | Message | Remediation |
|------|----------|------|---------|-------------|
| ERR-AUTH-001 | PERMANENT | 401 | Token is missing, malformed, unrecognized, or revoked | Include a valid API key as Bearer token. Create a new key via POST /api/v1/api-keys if revoked. |
| ERR-AUTH-002 | PERMANENT | 401 | Token has expired | Refresh or create a new API key. (Reserved, not yet enforced.) |
| ERR-AUTH-003 | PERMANENT | 403 | Insufficient scope for requested operation | Use an API key with the required scope, or request one from your administrator. |

### Ingest errors

| Code | Category | HTTP | Message | Remediation |
|------|----------|------|---------|-------------|
| ERR-INGEST-001 | PERMANENT | 422 | Invalid file (unsupported type, corrupt, or unrecognized MIME) | Use a supported format (PDF, DOCX, PPTX) and ensure the file is not corrupted. |
| ERR-INGEST-002 | PERMANENT | 413 | File exceeds maximum size | Reduce file size below the configured limit, or split into smaller documents. |
| ERR-INGEST-003 | PERMANENT | 422 | Scanned PDF detected (no extractable text layer) | Use a PDF with selectable text, or run OCR before ingesting. |
| ERR-INGEST-004 | TRANSIENT | 503 | Vector store write failed during indexing | Retry the ingestion. If the problem persists, check database connectivity and disk space. |

### Query errors

| Code | Category | HTTP | Message | Remediation |
|------|----------|------|---------|-------------|
| ERR-QUERY-001 | PERMANENT | 422 | No documents indexed in the requested namespace | Ingest documents into the namespace before querying. |
| ERR-QUERY-002 | UPSTREAM | 502 | LLM provider is unavailable | Verify the LLM provider is running and VEKTRA_LLM_PROVIDER is configured correctly. Retry in a few seconds. |
| ERR-QUERY-003 | PERMANENT | 422 | Query text exceeds the maximum length | Shorten the query or split it into multiple smaller queries. |
| ERR-QUERY-004 | TRANSIENT | 503 | Vector store read failed during search | Retry the query. If the problem persists, check database connectivity. |

### Configuration errors

| Code | Category | HTTP | Message | Remediation |
|------|----------|------|---------|-------------|
| ERR-CONFIG-001 | CONFIGURATION | 500 | Missing or invalid configuration variable | Check server logs. Verify all required environment variables are set. |
| ERR-CONFIG-002 | CONFIGURATION | 500 | Invalid provider configuration | Verify VEKTRA_LLM_PROVIDER and related provider settings are correct. |

## Non-normative error codes

These codes are used by specific components and are not part of the REQ-011 registry. They follow the same envelope format and actionability requirements.

| Code | Component | HTTP | Message |
|------|-----------|------|---------|
| ERR-ADMIN-001 | vektra-admin | 422 | Invalid scopes in API key creation request |
| ERR-ADMIN-002 | vektra-admin | 404 | API key not found |
| ERR-ADMIN-003 | vektra-admin | 409 | API key already revoked |
| ERR-ADMIN-004 | vektra-admin | 422 | API key `expires_at` must be in the future |
| ERR-ADMIN-005 | vektra-admin | 404 | Namespace not found (config PATCH) |
| ERR-ADMIN-006 | vektra-admin | 400 | Unknown config key in namespace PATCH body |
| ERR-ADMIN-007 | vektra-admin | 400 | Invalid value for namespace config key |
| ERR-SAFEGUARD-001 | vektra-core | 400 | Query blocked by safeguard pre-check |
| ERR-LEARN-001 | vektra-learn | 503 / 500 | Learn service unavailable (503, registry not initialized) or misconfigured (500, conversation store does not support decryption — `VEKTRA_CONVERSATION_KEY` missing) |
| ERR-LEARN-002 | vektra-learn | 404 | No enrollment found for student in course |
| ERR-LEARN-003 | vektra-learn | 401 | Dashboard token missing required course_id claim |
| ERR-LEARN-004 | vektra-learn | 409 | Duplicate enrollment (student already enrolled in course) |
| ERR-LEARN-005 | vektra-learn | 404 | Conversation not found (GET conversations/turns) |
| ERR-LEARN-006 | vektra-learn | 403 | Conversation belongs to a different course/namespace |
| ERR-INDEX-001 | vektra-index | 409 | Refused: the index version named for deletion is the one currently being served. Nothing was deleted (REQ-064) |
| ERR-INDEX-002 | vektra-index | 400 | Invalid index version (below 1) |

## Adding new error codes

Follow this procedure (REQ-011 convention):

1. **Define the constant** in `vektra_shared/errors.py` with a comment explaining the trigger condition.
2. **Add the HTTP status mapping** in `_CODE_STATUS_OVERRIDE` if the default category mapping is not appropriate.
3. **Document the code** in this file (add a row to the appropriate table).
4. **Implement the handler** in the component that raises this error, using `ErrorResponse` and `http_status_for()`.
5. **Write a test** that triggers the error via HTTP and asserts the response matches the REQ-010 envelope.

Convention: define the code in the registry *before* implementing the handler.
