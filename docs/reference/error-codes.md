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

The `error` object is at the JSON document root for **every** error response — both explicit errors and uncaught internal faults. Clients read `error.code` at the top level; there is no `detail` wrapper.

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
| ERR-AUTH-004 | TRANSIENT | 429 | Rate limit exceeded | Wait and retry after the rate limit window resets. The response carries `X-RateLimit-*` headers; the per-key limit is set on the API key (`rate_limit_rpm`). |
| ERR-AUTH-005 | TRANSIENT | 503 | Auth service unavailable | Retry shortly. (Reserved by REQ-041, not yet raised by any code path.) |

### Ingest errors

| Code | Category | HTTP | Message | Remediation |
|------|----------|------|---------|-------------|
| ERR-INGEST-001 | PERMANENT | 422 | Invalid file (unsupported type, corrupt, or unrecognized MIME) | Use a supported format (PDF, DOCX, PPTX) and ensure the file is not corrupted. |
| ERR-INGEST-002 | PERMANENT | 413 | File exceeds maximum size | Reduce file size below the configured limit, or split into smaller documents. |
| ERR-INGEST-003 | PERMANENT | 422 | Scanned PDF detected (no extractable text layer) | Use a PDF with selectable text, or run OCR before ingesting. |
| ERR-INGEST-004 | TRANSIENT | 503 | Vector store write failed during indexing | Retry the ingestion. If the problem persists, check database connectivity and disk space. |
| ERR-INGEST-005 | PERMANENT | 422 | Invalid `metadata` form field | Send a flat JSON object (scalar values only, keys `[a-z][a-z0-9_]{0,63}`, 4 KB max). `hidden_from_students` must be a JSON boolean, not the string `"true"`. |

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
| ERR-LEARN-007 | vektra-learn | 403 | Conversation belongs to another student, or predates per-student ownership |
| ERR-CONV-001 | vektra-core / vektra-admin | 404 | Conversation not found (core `GET`/`DELETE conversations/{id}`, admin turns) |
| ERR-CONV-002 | vektra-core / vektra-admin | 503 | Persistent conversation store not available (starting up, or none configured) |
| ERR-CONV-003 | vektra-admin | 501 | Configured conversation store cannot return decrypted turns (in-memory store) |
| ERR-QUERY-005 | vektra-core | 503 | Query pipeline provider not available (starting up, or none configured) |
| ERR-ADMIN-008 | vektra-admin | 503 | Service initializing (deep health check auth: registry/key store not ready) |
| ERR-INDEX-001 | vektra-index | 409 | Refused: the index version named for deletion is the one currently being served. Nothing was deleted (REQ-064) |
| ERR-INDEX-002 | vektra-index | 400 | Invalid index version (below 1) |
| ERR-INDEX-003 | vektra-index | 400 | Reindex target must differ from the active version |
| ERR-INDEX-004 | vektra-index | 404 | Reindex job not found |
| ERR-ANALYTICS-001 | vektra-analytics | 503 | Analytics service or database session not available (starting up, or none configured) |
| ERR-ANALYTICS-002 | vektra-analytics | 404 | Trace not found for the given `response_id` |
| ERR-ASSESS-001 | vektra-assess | 503 | Assessment discussion service unavailable; the remediation names the failing readiness item (see `GET /health/assess`) |
| ERR-ASSESS-002 | vektra-assess | 429 | Setup check requested too soon (one per site per 5 minutes); `Retry-After` gives the wait |
| ERR-ASSESS-003 | vektra-assess | 404 | Site or setup-check report not found (also returned for a key bound to another site) |
| ERR-ASSESS-004 | vektra-assess | 422 | Model activation refused: the eval report does not pass the NFR-023 gate (hash, HARD classes, judge calibration, data basis); the remediation lists the failing check codes |
| ERR-ASSESS-005 | vektra-assess | 401 | Discussion token missing, malformed, expired, of the wrong stage, already used, or revoked (the session's epoch moved on) |
| ERR-ASSESS-006 | vektra-assess | 403 | Refused: the request comes from a Moodle "login as" session, or does not assert `loginas: false` and `realuser_id: null` (REQ-084). Audited `loginas_rejected`, nothing written |
| ERR-ASSESS-007 | vektra-assess | 403 | The API key is not bound to an assess site for this purpose (`assess_site_keys`) |
| ERR-ASSESS-008 | vektra-assess | 404 | Session not found: unknown, rolled back, or belonging to another student, activity or site (the cases are indistinguishable on purpose) |
| ERR-ASSESS-009 | vektra-assess | 409 | The session is CLOSED: no further action, and no token is re-minted for it |
| ERR-ASSESS-010 | vektra-assess | 409 | A reply to the previous message is still pending ("attendi la risposta") |
| ERR-ASSESS-011 | vektra-assess | 409 | The session's state does not allow this action (for example a turn while the session is STARTING or in OUTAGE) |
| ERR-ASSESS-012 | vektra-assess | 429 | Too many requests: a per-token or per-session rate limit, or the Start cap; `retry_after` gives the wait |
| ERR-ASSESS-013 | vektra-assess | 503 | Capacity reached (NFR-020 admission limit K): the Start was refused and nothing was consumed |
| ERR-ASSESS-014 | vektra-assess | 400 | Malformed or over-length request body; the explicit envelope, never the framework 422 (DEBT-035) |
| ERR-ASSESS-015 | vektra-assess | 409 | Start refused: the discussion is not available for this student and activity now (`details.reason` carries the content-free reason code) |
| ERR-ASSESS-016 | vektra-assess | 403 | The plugin version sent at mint is below the required minimum (`VEKTRA_ASSESS_MIN_PLUGIN_VERSION` or the site's own minimum) |

### Deliberately not listed

`ERR-QUOTA-001` (namespace quota exceeded) is defined in `vektra_shared/errors.py` and
raised in `vektra_admin/quotas.py`, but `check_namespace_quota` has no production
caller: the ingest wiring is still open as FEAT-025. Documenting it today would
describe a response no client can receive. Add the row when the enforcement lands.

## Adding new error codes

Follow this procedure (REQ-011 convention):

1. **Define the constant** in `vektra_shared/errors.py` with a comment explaining the trigger condition.
2. **Add the HTTP status mapping** in `_CODE_STATUS_OVERRIDE` if the default category mapping is not appropriate.
3. **Document the code** in this file (add a row to the appropriate table).
4. **Implement the handler** in the component that raises this error, using `ErrorResponse` and `http_status_for()`.
5. **Write a test** that triggers the error via HTTP and asserts the response matches the REQ-010 envelope.

Convention: define the code in the registry *before* implementing the handler.

**`ERR-ASSESS-NNN` namespace.** Assess codes (`vektra_assess`, the assessment discussion feature) are
numbered in the order they are added: a new code takes the next free number, whichever plan adds
it, and a code is never reused or renumbered, even after the condition it named is removed (a
Moodle plugin release in the field may still branch on it). The constant lives in
`vektra_shared/errors.py`; `vektra_assess.errors` only re-exports it.
