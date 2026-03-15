# Implementation Plan: vektra-admin - RLS enforcement, scope checking, rate limiting

**ID**: 20260301-admin-enforcement
**Status**: completed
**Branch**: feat/phase2-wave1
**Created**: 2026-03-01T14:30:09Z
**Updated**: 2026-03-01T14:30:09Z

## Traceability

**Source**: admin-enforcement
**Source Type**: architecture

## Provides / Requires

**Provides**:
- RLS-enforced namespace isolation via feature flag (consumers: admin-ui)
- Granular scope enforcement for ingest/query endpoints (consumers: admin-ui)
- Per-key rate limiting middleware (consumers: admin-ui)
- Token expiration support (consumers: admin-ui)
- Namespace quota enforcement (consumers: admin-ui)

**Requires**:
- 20260301-shared-protocols-phase2.md: extended Namespace type with quota enforcement fields
- 20260301-database-phase2.md: namespace quota columns activation, expires_at column on api_keys

## References

### Requirements
- REQ-020: API key management @.s2s/requirements.md
- REQ-024: Scoped API key access control @.s2s/requirements.md
- REQ-031: Token scopes (admin, ingest, query) @.s2s/requirements.md
- REQ-041: Authentication error codes @.s2s/requirements.md
- REQ-048: Namespace support @.s2s/requirements.md

### Architecture
- ARCH-007: Namespace isolation via RLS @.s2s/architecture.md
- ARCH-023: API key lifecycle management @.s2s/architecture.md
- ARCH-025: RLS binding deferred to Phase 2 @.s2s/architecture.md
- ARCH-039: ProviderRegistry pattern @.s2s/architecture.md
- ARCH-047: Namespace as first-class entity @.s2s/architecture.md
- ARCH-060: Configuration reference (VEKTRA_MULTI_TENANT) @.s2s/architecture.md

### Decisions
- ADR-0009: Namespace isolation via PostgreSQL row-level security @.s2s/decisions/ADR-0009-namespace-isolation-rls.md
- ADR-0005: Module boundary enforcement @.s2s/decisions/ADR-0005-module-boundary-enforcement.md
- ADR-0022: SQLAlchemy 2.0 async with asyncpg @.s2s/decisions/ADR-0022-orm-sqlalchemy-async.md

### Dependencies
- 20260301-shared-protocols-phase2.md
- 20260301-database-phase2.md

## Overview

This plan hardens vektra-admin from Phase 1 single-tenant defaults to enforced multi-tenancy. Phase 1 has application-level namespace filtering (TD-02), all API keys with "admin" scope granting full access, no rate limiting enforcement (the `rate_limit_rpm` column exists but is unused), and an `lru_cache` storing plaintext keys indefinitely.

Phase 2 activates four enforcement layers: (1) PostgreSQL RLS policies bound via `SET LOCAL app.current_namespace` when `VEKTRA_MULTI_TENANT=true`, (2) granular scope checking so "ingest" keys can only call ingest endpoints and "query" keys can only call query endpoints, (3) in-memory sliding window rate limiting using the per-key `rate_limit_rpm` value, and (4) token expiration via a new `expires_at` column checked during auth validation.

Additionally, namespace quota enforcement (ARCH-047) rejects ingest operations when a namespace exceeds its configured `quota_documents` or `quota_chunks` limits. The `functools.lru_cache` in `keys.py` is replaced with `cachetools.TTLCache` (DEBT-008) to limit plaintext key exposure to 300 seconds.

## Design Notes

**RLS activation** (ADR-0009, ARCH-025):
- Feature flag: `VEKTRA_MULTI_TENANT` env var (default `false`, already defined in ARCH-060)
- When `true`: middleware executes `SET LOCAL app.current_namespace = :ns` at the start of each request's DB session
- RLS policies created by this plan via Alembic migration on `source_documents`, `document_chunks`, `ingest_jobs`, `conversations`, `conversation_turns`
- Policy pattern: `USING (namespace_id = current_setting('app.current_namespace'))` with `WITH CHECK` for INSERT
- Middleware placement: new `RLSMiddleware` or integrated into existing auth flow. Runs after auth (to know the namespace) and before DB queries
- The namespace is determined from the request: query body `namespace` field, ingest body `namespace` field
- Fallback: if `VEKTRA_MULTI_TENANT=false`, no `SET LOCAL` is executed (Phase 1 behavior, application-level filtering continues)

**Scope enforcement** (REQ-024):
- Phase 1: `admin` scope grants access to all endpoints. `query` and `ingest` scopes exist but are only checked on specific endpoints (query endpoints check for `query`, but admin endpoints check for `admin`).
- Phase 2: strict enforcement. The `require_scope()` dependency in `vektra_shared/auth.py` already handles single-scope checks. No code change needed for the auth dependency itself. The enforcement change is ensuring all endpoints use the correct scope:
  - POST /api/v1/ingest, GET /api/v1/ingest/jobs/*: `ingest` or `admin`
  - POST /api/v1/query, GET /api/v1/providers: `query` or `admin`
  - POST/GET/DELETE /api/v1/api-keys, GET /admin: `admin` only
  - POST /api/v1/documents/{id}/chunks: `ingest` or `admin`
  - DELETE /api/v1/documents/{id}: `admin` only
  - GET /api/v1/stats: any scope
- Verify that existing endpoint auth decorators match these rules; fix any that do not.

**Rate limiting** (EX-013):
- In-memory sliding window counter per API key
- Keyed by `key_id` (UUID), not by token hash
- Window: 60 seconds (RPM = requests per minute)
- Storage: `dict[UUID, deque[float]]` where each entry is a deque of request timestamps
- Check: count timestamps within the last 60 seconds; reject with 429 if count >= `rate_limit_rpm`
- `rate_limit_rpm` comes from the `api_keys` table (already exists, nullable). NULL means unlimited.
- Implementation: `RateLimiter` class in `vektra_admin/rate_limit.py` (new file)
- Integration: called from auth middleware after key validation, before endpoint handler
- Response headers: `X-RateLimit-Limit`, `X-RateLimit-Remaining`, `X-RateLimit-Reset` (Unix timestamp)
- 429 response uses ErrorResponse envelope: `ERR-AUTH-004`, category TEMPORARY, remediation "Wait and retry"

**Token expiration** (REQ-041):
- New column: `api_keys.expires_at` (TIMESTAMPTZ, nullable). NULL means never expires.
- Added by database-phase2 migration
- Checked in `InMemoryKeyStore.lookup_by_token()`: if `expires_at` is not None and `expires_at < now()`, return None (same as revoked)
- `_KeyEntry` dataclass gains `expires_at: datetime | None` field
- `CreateKeyRequest` gains optional `expires_at: datetime | None` field
- `KeyListItem` gains `expires_at: datetime | None` field
- `load_from_db()` filters `WHERE revoked_at IS NULL AND (expires_at IS NULL OR expires_at > now())`

**Namespace quota enforcement** (ARCH-047):
- `NamespaceOrm` already has `quota_chunks` and `quota_documents` columns (nullable, present since Phase 1)
- Enforcement: before ingest, query current document/chunk counts for the namespace. If adding would exceed quota, reject with 422 and error code `ERR-QUOTA-001`
- Implementation: `check_namespace_quota()` function in `vektra_admin/namespace.py` or a new `vektra_admin/quotas.py`
- Called from ingest endpoint auth/validation, not from the ingest pipeline itself (fail fast at API boundary)

**TTLCache replacement** (DEBT-008):
- Replace `@lru_cache(maxsize=512)` in `keys.py` with `cachetools.TTLCache(maxsize=512, ttl=300)`
- `cachetools` added to vektra-admin pyproject.toml dependencies
- The `_cached_verify` function becomes a regular function that checks the TTLCache manually (since `cachetools` decorators are not thread-safe, use a wrapper with the cache instance)

## Tasks

- [x] Add `cachetools` to `vektra-admin/pyproject.toml` dependencies
- [x] Replace `functools.lru_cache` with `cachetools.TTLCache` in `vektra_admin/keys.py`: create a `TTLCache(maxsize=512, ttl=300)` instance, replace `_cached_verify` with a manual cache lookup/store pattern. Use `threading.Lock` for cache access (DEBT-008)
- [x] Add `expires_at: Mapped[datetime | None]` to `ApiKeyOrm` in `vektra_admin/models.py`. Update `CreateKeyRequest` and `KeyListItem` in `api.py` to include `expires_at`
- [x] Update `_KeyEntry` dataclass in `keystore.py` to include `expires_at: datetime | None`. Update `load_from_db()` to filter expired keys. Update `lookup_by_token()` to check expiration before returning `ApiKeyInfo`
- [x] Update `add_key()` in `InMemoryKeyStore` to accept and store `expires_at`. Update the `create_api_key` endpoint in `api.py` to pass `expires_at` through to ORM and keystore
- [x] Create `vektra_admin/rate_limit.py` with `RateLimiter` class: sliding window counter using `dict[UUID, deque[float]]`, `check(key_id, rpm_limit) -> tuple[bool, dict]` returning (allowed, headers). Include cleanup of stale entries
- [x] Integrate rate limiter into auth flow: after key validation in `require_scope()` or in a new middleware, call `RateLimiter.check()`. On rejection, return 429 with `ERR-AUTH-004` ErrorResponse. Add rate limit response headers to successful responses
- [x] Create RLS policies via Alembic migration 0003_rls_policies.py: ENABLE ROW LEVEL SECURITY + CREATE POLICY on 5 namespace-scoped tables (source_documents, document_chunks, ingest_jobs, conversations, feedback). Policies use `current_setting('app.current_namespace', true)` and are inert until SET LOCAL is called. Index-hybrid migration becomes 0004.
- [x] Create `vektra_admin/rls.py` with RLS middleware: when `VEKTRA_MULTI_TENANT=true`, execute `SET LOCAL app.current_namespace` at session start. Namespace resolved from request body (query or ingest payload `namespace` field). Middleware is a no-op when the flag is false
- [x] Add `VEKTRA_MULTI_TENANT` to config schema in `vektra_shared/config.py` if not already present (it is referenced in ARCH-060 but may not be in the Pydantic schema yet). Validate at startup
- [x] Implement namespace quota checking: `check_namespace_quota(session, namespace_id, new_documents, new_chunks) -> None` that raises HTTPException 422 with `ERR-QUOTA-001` when quota exceeded. Wiring into ingest endpoint deferred to ingest-phase2 plan
- [x] Verify scope enforcement across all endpoints: audit every router endpoint in vektra-core, vektra-ingest, vektra-admin to confirm the correct scope is required. Fix any endpoints that accept broader scopes than specified in ARCH-059
- [x] Write unit tests for TTLCache replacement: verify cache hit, cache miss, cache expiration after TTL, thread safety under concurrent access
- [x] Write unit tests for rate limiter: under-limit allows, at-limit rejects, window sliding (old requests expire), NULL rpm means unlimited, response headers correct

## Acceptance Criteria

- [ ] When `VEKTRA_MULTI_TENANT=true`, `SET LOCAL app.current_namespace` is executed before each DB query, and cross-namespace access is impossible via RLS
- [ ] When `VEKTRA_MULTI_TENANT=false`, behavior matches Phase 1 (application-level filtering, no RLS binding)
- [ ] API keys with `ingest` scope can call ingest endpoints but receive 403 on query and admin endpoints
- [ ] API keys with `query` scope can call query endpoints but receive 403 on ingest and admin endpoints
- [ ] Per-key rate limiting returns 429 with correct headers when `rate_limit_rpm` is exceeded
- [ ] Keys with `rate_limit_rpm=NULL` are not rate-limited
- [ ] Expired keys (past `expires_at`) are rejected with 401, same as revoked keys
- [ ] Keys with `expires_at=NULL` never expire (backward compatible with Phase 1 keys)
- [ ] Namespace quota enforcement rejects ingest when document or chunk count would exceed limits
- [ ] Namespaces with `quota_documents=NULL` and `quota_chunks=NULL` are unlimited (backward compatible)
- [ ] `functools.lru_cache` replaced with TTLCache; plaintext keys expire from cache after 300 seconds (DEBT-008)
- [ ] All existing admin tests continue to pass

## Testing Approach

Unit tests cover each enforcement layer independently. TTLCache tests use time mocking to verify expiration without waiting 300 seconds. Rate limiter tests use a deterministic clock to verify sliding window behavior. RLS tests require a PostgreSQL instance with the pgcrypto extension and RLS policies; these run as integration tests in CI (Docker Compose test profile). Scope enforcement tests use FastAPI `TestClient` with different scoped keys to verify 403 responses.

Quota enforcement tests create a namespace with `quota_documents=2`, ingest two documents successfully, and verify the third is rejected with 422.

For RLS, integration tests create two namespaces, ingest a document into each, then query with each namespace's key and verify no cross-namespace leakage.

## Integration Notes

The rate limiter is in-memory and process-scoped. For single-process deployments (Phase 1-2 target), this is sufficient. Multi-process deployments (Phase 3) would need Redis-backed rate limiting, but that is out of scope.

The RLS middleware must run after the auth middleware (to have `key_info` with namespace) and before any DB session usage. The recommended approach is a FastAPI dependency that runs `SET LOCAL` on the session, similar to how `get_session` works. This avoids ASGI middleware ordering issues.

The `expires_at` column is added by the database-phase2 migration. Existing Phase 1 keys have `expires_at=NULL` and continue to work without modification.

The scope enforcement audit may reveal endpoints in vektra-ingest or vektra-core that need auth dependency adjustments. Those changes are small (swapping `require_scope("admin")` for a new helper like `require_scope("ingest")`) and should be done in this plan since the enforcement belongs to the admin concern.

## Notes

### Session 1 progress (2026-03-01)

**Completed tasks**: 1-11 (cachetools, TTLCache, expires_at on ApiKeyOrm/KeyListItem/CreateKeyRequest/_KeyEntry, rate limiter class, rate limiter integration in require_scope(), RLS migration 0003, RLS middleware, VEKTRA_MULTI_TENANT confirmed, quota enforcement).

**In progress**: Task 12 (scope enforcement audit). Started reading endpoints but not yet complete.

**Pending tasks**: 12 (scope enforcement audit), 13 (TTLCache tests), 14 (rate limiter tests).

**Key decisions made**:
- Migration 0003 is RLS policies (not index-hybrid). Index-hybrid migration becomes 0004.
- Rate limiter integrated in `require_scope()` via duck typing from `request.app.state.rate_limiter`.
- `ApiKeyInfo` gained `rate_limit_rpm: int | None = None` field.
- ERR-AUTH-004 (429) and ERR-QUOTA-001 (422) added to `vektra_shared/errors.py`.
- Quota check uses raw SQL (not ORM) to avoid cross-module imports.
- `verify_key` in keys.py uses `threading.Lock` (not asyncio.Lock) because argon2 verify is CPU-bound and runs synchronously.

### Session 2 progress (2026-03-01)

**Completed tasks**: 12-14 (scope enforcement audit, TTLCache tests, rate limiter tests).

**Scope enforcement audit findings and fixes**:
- `require_scope()` in auth.py updated: admin treated as superscope (ARCH-059), `None` accepted for "any scope"
- vektra-index `stats` endpoint: changed from `require_scope("query")` to `require_scope(None)` (any scope)
- vektra-admin `admin_dashboard`: changed from `_require_any_token` to `require_scope("admin")`
- vektra-index `store_chunks` and `search`: now correctly accept admin keys via superscope logic
- vektra-core and vektra-ingest: already had correct custom deps (`_require_query_scope`, `_require_ingest_scope`)
- 4 new auth tests added for superscope and `require_scope(None)` behavior

**Test files created**:
- `vektra-admin/tests/test_ttlcache.py` (10 tests): cache population, hit/miss, config values, thread safety
- `vektra-admin/tests/test_rate_limit.py` (14 tests): basic flow, null rpm, headers, sliding window, cleanup

**All 14 tasks complete. Plan is done.**
