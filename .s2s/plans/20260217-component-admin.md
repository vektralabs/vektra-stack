# Implementation Plan: vektra-admin - Health, API key management, audit log, namespace bootstrap

**ID**: 20260217-component-admin
**Status**: active
**Branch**: N/A
**Created**: 2026-02-17T22:42:39Z
**Updated**: 2026-02-17T22:42:39Z

## Traceability

**Source**: component-admin
**Source Type**: architecture

## References

### Requirements
- REQ-004: WF-ADMIN-001: System Health Check workflow @.s2s/requirements.md
- REQ-006: Phase 1 admin capabilities @.s2s/requirements.md
- REQ-019: API key authentication for all components @.s2s/requirements.md
- REQ-020: API key management via admin component @.s2s/requirements.md
- REQ-021: Admin bootstrap authentication @.s2s/requirements.md
- REQ-022: API key audit logging @.s2s/requirements.md
- REQ-023: API key table schema with scope support @.s2s/requirements.md
- REQ-024: Phase 1 scope enforcement @.s2s/requirements.md
- REQ-025: Health endpoint authentication: two-tier model @.s2s/requirements.md
- REQ-031: API scope definitions @.s2s/requirements.md
- REQ-036: Bootstrap key single-use enforcement @.s2s/requirements.md
- REQ-051: Operator privacy: no conversation content access @.s2s/requirements.md
- NFR-007: Audit log completeness (100%) @.s2s/requirements.md
- NFR-008: Configurable audit log retention @.s2s/requirements.md

### Architecture
- ARCH-007: Admin component design @.s2s/architecture.md
- ARCH-014: Prometheus metrics @.s2s/architecture.md
- ARCH-020: Single trust boundary @.s2s/architecture.md
- ARCH-022: Health endpoint architecture @.s2s/architecture.md
- ARCH-023: API key management @.s2s/architecture.md
- ARCH-025: Bootstrap key enforcement @.s2s/architecture.md
- ARCH-027: Memory health endpoint @.s2s/architecture.md
- ARCH-047: Namespace as first-class entity @.s2s/architecture.md

### Decisions
- ADR-0009: Namespace isolation via PostgreSQL RLS @.s2s/decisions/ADR-0009-namespace-isolation-rls.md
- ADR-0010: Authentication gateway @.s2s/decisions/ADR-0010-authentication-gateway.md

### Dependencies
- 20260217-component-shared
- 20260217-infra-database

## Overview

Implements the administration layer: two-tier health endpoints, API key CRUD with argon2id hashing, single-use bootstrap key enforcement, audit log writes, and a minimal read-only health dashboard. Also exposes Prometheus metrics. This component is the keyholder for the auth middleware - the key store used by vektra_shared's auth middleware is populated by this component at startup.

## Design Notes

- Phase 1 scope enforcement is permissive: all keys created via POST /api-keys get `['admin']` scope by default, granting full access. The scopes array is stored and returned but enforcement only validates that unknown scope values are rejected (REQ-024). Full granular enforcement deferred to Phase 2.
- Bootstrap key (VEKTRA_ADMIN_BOOTSTRAP_KEY): single-use, enforced via `system_state` table row `('bootstrap_key_consumed', 'false')`. On first successful POST /api-keys with the bootstrap key, atomically set to 'true'. Subsequent requests with bootstrap key return 401 (REQ-036).
- Key hash: argon2id via `argon2-cffi` library. Verification on every authenticated request is rate-limited by argon2 work factor - consider caching recently verified key hashes in a short-lived LRU cache to avoid per-request hashing overhead.
- Audit log writes are fire-and-forget (do not block API response). Use FastAPI `BackgroundTasks` for async write. On audit log write failure, log a structured ERROR to application log (audit log integrity is NFR-007 HARD gate).
- Health dashboard at GET /admin: returns minimal HTML page showing /health?detail=full JSON response rendered in a table. No JavaScript framework - plain HTML with inline CSS. Requires any valid Bearer token.
- Prometheus metrics: starlette-prometheus middleware registered at application level, exposes GET /metrics. No authentication on /metrics (standard pattern for Prometheus scraping).
- Audit function re-export: `vektra_admin/audit.py` implements `log_event()`. vektra_shared re-exports it as `vektra_shared.audit.log_event()` so that other components (vektra_ingest, vektra_core) can call it without importing vektra_admin directly (ADR-0005). The re-export is a thin import alias — no logic duplication.
- Middleware ownership: vektra_admin exports `AuditMiddleware` class; infra-app-entrypoint registers it. This pattern separates the implementation (here) from assembly (there), consistent with the modular monolith approach.

## Tasks

- [ ] Create `vektra_admin/models.py`: SQLAlchemy ORM for `api_keys`, `audit_log`, `namespaces`, `system_state` tables
- [ ] Implement `vektra_admin/keys.py`: key generation (32-byte random → URL-safe base64), argon2id hashing via `argon2-cffi`, key_preview extraction (last 4 chars), LRU cache for recently verified hashes
- [ ] Implement `vektra_admin/bootstrap.py`: startup check and enforcement of VEKTRA_ADMIN_BOOTSTRAP_KEY; loads current bootstrap_key_consumed state from system_state table; exposes `is_bootstrap_key(token)` and `consume_bootstrap_key()` used by POST /api-keys handler
- [ ] Implement `vektra_admin/audit.py`: `log_request(key_id, endpoint, method, status_code, request_id, action=None)` writes to audit_log table; used as FastAPI middleware and from named event callsites; structured ERROR log on write failure (not exception propagation)
- [ ] Implement `vektra_admin/health.py`: health check aggregator calling each component's health_check callable registered in ProviderRegistry under the `"health"` category key (e.g., `"health/embedding"`, `"health/llm"`, `"health/vector_store"`). Do NOT call component modules directly (ADR-0005 violation). Components register their health_check at startup in infra-app-entrypoint. Returns ComponentHealth(name, status, latency_ms, remediation_hint?) per registered check; assembles shallow response {status, timestamp} and deep response {components: [...], version}.
- [ ] Create `vektra_admin/api.py` with FastAPI router:
  - `GET /health`: unauthenticated shallow; returns {status: healthy|degraded|unhealthy, timestamp}; HTTP 200 for healthy/degraded, 503 for unhealthy
  - `GET /health?detail=full`: requires any valid Bearer token; returns full component breakdown
  - `GET /health/{component}`: requires any valid Bearer token; returns single component status
  - `GET /health/memory`: requires any valid Bearer token; returns process memory stats
  - `POST /api/v1/api-keys`: create API key; accepts {label?, scopes?} defaulting to ['admin']; validates bootstrap key or admin-scoped key; returns {id, key (plaintext, ONCE), key_preview, label, scopes, created_at}; emits audit log 'apikey_created'
  - `GET /api/v1/api-keys`: list keys with metadata (no key values); requires admin scope; returns [{id, key_preview, label, scopes, created_at, last_used_at, revoked}]
  - `DELETE /api/v1/api-keys/{id}`: soft-delete (set revoked_at); requires admin scope; emits audit log 'apikey_revoked'; revoked keys rejected within 60 seconds (LRU cache TTL)
  - `GET /admin`: minimal HTML health dashboard; requires any valid Bearer token
  - `GET /metrics`: Prometheus metrics; unauthenticated
- [ ] Populate ProviderRegistry key store at startup: load all non-revoked api_keys from database into in-memory cache (key_hash → {id, scopes, revoked_at}). Runtime cache maintenance: when POST /api-keys creates a key, add it to the cache immediately; when DELETE /api-keys/{id} revokes a key, mark it revoked in the cache immediately (do not wait for TTL). Cache TTL (60s) is a fallback for external revocation only, not the primary invalidation mechanism for operations performed via this service.
- [ ] Implement `vektra_admin/middleware.py`: `AuditMiddleware(BaseHTTPMiddleware)` class that intercepts every request after auth completes, writes key_id + endpoint + method + status_code + request_id to audit_log via `audit.log_event()`, skips /health and /metrics paths. Do NOT register this middleware on the FastAPI app here — export `AuditMiddleware` class only. infra-app-entrypoint is responsible for calling `app.add_middleware(AuditMiddleware)`.
- [ ] Log startup warning if VEKTRA_ADMIN_BOOTSTRAP_KEY is set and VEKTRA_ENV=production (REQ-021)
- [ ] Write unit tests: key generation and hash verification, bootstrap key single-use enforcement, scope validation, audit log fire-and-forget (verify background task called), health aggregation with mocked component checks
- [ ] Write integration tests: full API key lifecycle (create, list, use, revoke, verify rejected), bootstrap key consumed after first use, deep health check returns all components, GET /admin returns valid HTML

## Acceptance Criteria

- [ ] `POST /api-keys` returns full key value exactly once; subsequent GET /api-keys shows key_preview only
- [ ] Bootstrap key returns 401 on second use; consumed state persists across restarts (stored in system_state table)
- [ ] `GET /health` (unauthenticated) returns only {status, timestamp} - no component names, versions, or internal hostnames
- [ ] `GET /health?detail=full` requires Bearer token; returns component array with name, status, latency_ms
- [ ] 100% of authenticated requests produce an audit_log entry (NFR-007)
- [ ] Revoked keys rejected within 60 seconds (LRU cache TTL <= 60s)
- [ ] GET /admin returns valid HTML with current health status; requires Bearer token
- [ ] Startup warning logged if VEKTRA_ADMIN_BOOTSTRAP_KEY set and VEKTRA_ENV=production

## Testing Approach

Unit tests for key management (argon2id hash/verify) and bootstrap enforcement (state machine). Integration tests use testcontainers PostgreSQL and a full FastAPI test client. Audit log completeness test: make N authenticated requests, count audit_log rows, assert count matches. Health endpoint tests verify both tiers (unauthenticated shallow vs authenticated deep).

## Integration Notes

vektra_shared's auth middleware depends on the key store populated by vektra_admin. At startup, admin module loads keys and registers them into ProviderRegistry (key cache); auth middleware reads from this shared in-memory store. The cache is also updated at runtime when keys are created or revoked via this component's own API endpoints.

Other components write audit log entries via `vektra_shared.audit.log_event()` — not by importing `vektra_admin.audit` directly. `vektra_shared.audit` re-exports the function from `vektra_admin.audit`.

Health aggregator reads health-check callables from ProviderRegistry (category `"health"`), not from direct module imports. Components register their health checks in infra-app-entrypoint.

`AuditMiddleware` is exported from `vektra_admin.middleware`; infra-app-entrypoint registers it on the FastAPI app.
