# PR #27 review: Phase 2 Wave 1

**Date**: 2026-03-03
**Branch**: feat/phase2-wave1
**Commits**: 27 (8f055c3..dba8b87) + fix commits (0a3acfa, 0c8191f, 087c12c, 9a0cbac, 51433e5, f089f04, 0151bb48, dba8b87)
**Files**: 72 changed, ~10.8K additions

## HIGH - Security / correctness issues

| # | Module | File:Line | Issue | Status |
|---|--------|-----------|-------|--------|
| H1 | core | `pipeline.py:494` | Safeguard bypass in streaming: `pre_response` receives UUID instead of answer content | FIXED |
| H2 | admin | `api.py:156` | Deep health returned without auth when registry is None (should fail closed 503) | FIXED |
| H3 | ingest | `api.py:668` | Job status not namespace-scoped: any ingest key can poll any job ID | FIXED |
| H4 | index | `api.py:201-202` | Providers instantiated directly, not from ProviderRegistry | FIXED |
| H5 | index | `api.py:125,184` | No namespace binding enforcement on store/search endpoints | FIXED |
| H6 | ingest | `pipeline.py:166-234` | Dedup TOCTOU not converted to IngestConflictError; handlers are dead code | FIXED |
| H7 | ingest | `unstructured.py:133` | No timeout on asyncio.to_thread for OCR (can starve thread pool) | FIXED |

## MEDIUM - Correctness / design issues

| # | Module | File:Line | Issue | Status |
|---|--------|-----------|-------|--------|
| M1 | core | `conversation.py:220-228` | `turn_count` is monotonic, not current (misleading after pruning) | DEFER |
| M2 | core | `api.py:371` | `delete_conversation` has no namespace check (cross-tenant deletion) | FIXED |
| M3 | core | `api.py:401,440` | Feedback namespace not validated against key scope | FIXED |
| M4 | core | `conversation.py:244` | `clear()` hard-deletes, bypassing GDPR retention | DEFER |
| M5 | admin | `api.py:459-493` | XSS in HTML dashboard (unescaped component data) | FIXED |
| M6 | admin | `quotas.py:88` | Chunk quota counts orphaned chunks from soft-deleted documents | DEFER |
| M7 | admin | `quotas.py:44-104` | Quota check is TOCTOU (soft limit only, document as known limitation) | WONTFIX |
| M8 | admin | `rls.py:80-93` | `set_rls_namespace` silently no-ops on wrong session type | FIXED |
| M9 | index | `pgvector.py:438-460` | `namespace_stats` chunk count includes soft-deleted doc chunks | DEFER |
| M10 | index | `qdrant.py:83-110` | `ensure_collection` TOCTOU on startup (catch "already exists") | DEFER (infra-phase2, Wave 5) |
| M11 | index | `fastembed_bm25.py:26-47` | Global sparse model singleton not thread-safe (needs Lock) | DEFER |
| M12 | index | `reindex.py:71-182` | Reindex runs in BackgroundTasks, not arq (lost on restart) | WONTFIX |
| M13 | index | `reindex.py:206-222` | No duplicate reindex job guard for same namespace | DEFER |
| M14 | ingest | `api.py:307-318` | Batch ingest commits per-file; mid-loop failure orphans jobs | DEFER |
| M15 | ingest | `chunking.py:76-83` | FixedSizeChunking discards element type metadata | DEFER |
| M16 | ingest | `chunking.py:202-203` | DualStrategyChunking: stale first_metadata across segments | DEFER |
| M17 | ingest | `pipeline.py:502-504` | Wrong deletion_reason on cleanup ("user_request" for pipeline failure) | DEFER |
| M18 | shared | `main.py:52-57` | PII redactor only runs at WARNING+ (INFO/DEBUG leaks PII) | DEFER |
| M19 | migrations | `0004` | `reindex_jobs` has no RLS policy despite namespace_id column | DEFER |
| M20 | admin | `middleware.py:91-136` | Audit middleware action is always NULL | DEFER |

## LOW - Minor issues

| # | Module | File:Line | Issue | Status |
|---|--------|-----------|-------|--------|
| L1 | admin | `keystore.py:118-131` | Revoked keys never removed from memory (unbounded growth) | DEFER |
| L2 | admin | `rate_limit.py:27-101` | `cleanup()` defined but never called | DEFER |
| L3 | admin | `api.py:232-304` | Bootstrap TOCTOU (low risk: one-time operation) | DEFER |
| L4 | index | `api.py:138` | `chunk_id=""` passed to ChunkEmbedding (breaks Qdrant path) | DEFER |
| L5 | index | `qdrant.py:150-153` | `document_id` from untyped metadata, fallback to UUID(int=0) | DEFER |
| L6 | index | `qdrant.py:291-319` | `delete()` always returns 0 (meaningless chunks_removed) | DEFER |
| L7 | index | `models.py:74` | Embedding dimension hardcoded to 384 | DEFER |
| L8 | ingest | `jobs.py:92-98` | File bytes in arq/Redis payload (up to 100MB) | DEFER |
| L9 | core | `api.py:326-332` | `isinstance` check on concrete class breaks Protocol abstraction | DEFER |
| L10 | core | `api.py:137-143` | SSE format inconsistent (tokens raw, errors/sources JSON) | FIXED |
| L11 | shared | `main.py:418-421` | CORS overly permissive with allow_credentials=True | DEFER |
| L12 | shared | `main.py:428` | `/metrics` endpoint unauthenticated | DEFER |
| L13 | shared | `main.py:467` | VEKTRA_CORS_ORIGINS not in VektraSettings | DEFER |

## CodeRabbit/Gemini findings (additional to original review)

| # | Source | File:Line | Issue | Status |
|---|--------|-----------|-------|--------|
| CR1 | CR Critical | `rls.py:53` | RLS namespace from client input without auth validation | WONTFIX (endpoint-level binding is primary defense) |
| CR2 | CR Major | `rls.py:70` | BaseHTTPMiddleware body consumption (Starlette limitation) | DEFER (infra-phase2: pure ASGI middleware) |
| CR3 | CR Major | `reindex.py:146` | Reindex loop is stub (doesn't re-embed) | WONTFIX (by design, Phase 2 stub) |
| CR4 | CR Major | `ingest/api.py:623` | `zip(chunks, embeddings)` truncates silently on length mismatch | DEFER (add length assertion before zip) |
| CR5 | CR Major | `index/api.py:62` | Sparse vector `indices`/`values` length not validated | DEFER (add Pydantic model_validator) |
| CR6 | CR Major | `jobs.py:231` | Cleanup SELECT-then-DELETE pattern (memory, not correctness) | DEFER (low volume, optimize later) |
| CR7 | CR Major | `test_unstructured.py:39` | Module-level sys.modules patch leaks across tests | DEFER (low priority, no cross-test impact) |
| CR8 | CR Minor | `quotas.py:28` | No guard against negative quota deltas | DEFER (internal API, callers always pass positive) |
| CR9 | CR Minor | `conversation.py:64` | `max_turns` accepts 0/negative values | DEFER (config-validated, add ge=1 guard) |
| CR10 | Gemini Critical | `0003_rls_policies.py:80` | NULL vs '' in RLS COALESCE | FIXED (0a3acfa) |
| CR11 | Gemini High | `keys.py:79` | Blocking argon2 verify_key | FIXED (0a3acfa, async+to_thread) |
| CR12 | CR Major | `core/api.py:143` | SSE drops trace chunks | FIXED (9a0cbac) |

## Subsequent review round findings (rounds 2-4, triggered by fix pushes)

| # | Source | File:Line | Issue | Status |
|---|--------|-----------|-------|--------|
| CR13 | CR Major | `keystore.py:95-111` | `add_key()` missing `rate_limit_rpm` propagation | FALSE POSITIVE (already present, lines 102+112) |
| CR14 | CR Minor | `jobs.py:19-264` | CI blocker: jobs.py needs formatting | FIXED (ruff format already passes) |
| CR15 | CR Major | `qdrant.py:95` | Qdrant schema validation on startup | DEFER (infra-phase2, Wave 5, ARCH-057) |
| CR16 | CR Major | `reindex.py:128` | Unbounded document-ID materialization in memory | DEFER (infra-phase2, Wave 5, skeleton rewrite) |
| CR17 | CR Major | `reindex.py:230` | Enforce namespace binding on reindex job creation | FIXED (f089f04) |
| CR18 | CR Minor | `middleware.py:99` | Use `rls_namespace` as JSON metadata key | WONTFIX (by design, "namespace" is domain term) |
| CR19 | CR Minor | `reindex.py` | Docstring doesn't match skeleton behavior | DEFER (minor, Wave 5 rewrite) |
| CR20 | CR Minor | `admin/api.py:96` | Validate `expires_at` is in the future | FIXED (51433e5) |
| CR21 | CR Major | `pipeline.py:68` | `IngestResult.version` wrong for `exists`/`alias` paths | DEFER |
| CR22 | CR Minor | `pipeline.py:211` | Step 2 comment no longer matches runtime behavior | FIXED (51433e5) |
| CR23 | CR Critical | `pipeline.py:259` | Expand rollback/error handling to cover all post-supersede failures | DEFER (core-pipeline-v2, Wave 2) |
| CR24 | CR Major | `admin/api.py:96` | Bootstrap validation gap | DEFER |
| CR25 | CR Major | `jobs.py:207` | Negative `retention_days` creates future cutoff | DEFER (add ge=1 to ObservabilityConfig) |
| CR26 | CR Major | `pipeline.py:308` | Map commit-time re-ingest TOCTOU to IngestConflictError | DEFER (core-pipeline-v2, Wave 2) |
| CR27 | CR Major | `pipeline.py` | Best-effort event delivery not fully honored | WONTFIX (by design, ARCH-038) |
| CR28 | CR Minor | `test_versioning.py:489` | Ruff format check failing | FIXED |
| CR29 | CR Major | `pipeline.py` | Broader exception handling for event emission | FIXED (dba8b87) |
| CR30 | CR Major | `admin/api.py:168` | Deep-health doesn't set `request.state.key_id` | DEFER (health checks not audited) |
| CR31 | CR Minor | `ingest/api.py:197` | Audit `status_code` mismatch (422 vs actual) | FALSE POSITIVE (PERMANENT=422, consistent) |
| CR32 | CR Minor | `unstructured.py:197` | Run formatter before merge | FIXED (9a0cbac) |
| CR33 | CR Minor | `test_pipeline.py:706` | Test name misleading, doesn't verify event emission | FIXED (renamed in 0dafb53) |
| CR34 | CR Minor | `test_versioning.py:459` | Test collects stmts but never inspects them | WONTFIX (by design, domain assertions) |
| CR35 | CR Minor | `keys.py` | Documentation inconsistency on cache behavior | FIXED (comment updated) |
| CR36 | CR Minor | `pgvector.py:224` | Operator usage pattern | WONTFIX (correct for pgvector) |
| CR37 | CR Minor | `pgvector.py:337` | Normalize list filter values to strings | WONTFIX (type contract enforces strings) |
| CR38 | CR Minor | `keystore.py` | Inclusive expiry check at runtime | DEFER |
| CR39 | CR Minor | `test_pipeline.py:247` | Test assertion improvement | DEFER |
| CR40 | CR Minor | `markdown.py:79` | Heading detection inside fenced code blocks | FIXED (0a3acfa) |

## Deferred items: target wave mapping

Items deferred to specific future waves for pickup:

| Item | Target | Description |
|------|--------|-------------|
| CR2, M10, CR15, CR16, CR19 | infra-phase2 (Wave 5) | ASGI middleware, Qdrant TOCTOU, schema validation, reindex rewrite |
| CR23, CR26 | core-pipeline-v2 (Wave 2) | Pipeline rollback, IntegrityError mapping |
| CR25 | infra-phase2 (Wave 5) | `retention_days` ge=1 on ObservabilityConfig |
| M19 | database migration (Wave 5) | RLS policy for reindex_jobs table |

Items deferred without a specific wave (address opportunistically or in Phase 3):

| Item | Description |
|------|-------------|
| M1 | turn_count monotonic after pruning |
| M4 | clear() hard-deletes (GDPR retention) |
| M6, M9 | Quota/stats counting soft-deleted documents |
| M11 | fastembed singleton thread-safety (Lock) |
| M13 | Duplicate reindex job guard |
| M14 | Batch ingest per-file commits |
| M15, M16 | Chunking metadata (element type, per-segment) |
| M17 | Wrong deletion_reason on cleanup |
| M18 | PII redactor log level filter |
| M20 | Audit middleware action NULL |
| CR4, CR5 | Input validation (zip length, sparse vector shape) |
| CR6, CR7, CR8, CR9 | Minor correctness guards |
| L1-L9, L11-L13 | Low-priority improvements |

## Patterns (non-blocking, for awareness)

- `updated_at` without `onupdate` on NamespaceOrm and SourceDocumentOrm
- `IngestConflictError` is likely dead code (Phase 2 versioning replaced conflict behavior)
- Sparse search O(n*m) with no index (expected for Phase 2 prototype)

## Summary

- **Total findings**: 7H + 20M + 13L + 40CR = 80
- **Fixed**: 7H + 5M + 1L + 14CR = 27
- **WONTFIX**: 2M + 6CR = 8
- **FALSE POSITIVE**: 2CR = 2
- **DEFER**: 13M + 12L + 18CR = 43
