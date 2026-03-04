# PR #27 review: Phase 2 Wave 1

**Date**: 2026-03-03
**Branch**: feat/phase2-wave1
**Commits**: 27 (8f055c3..dba8b87)
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
| M1 | core | `conversation.py:220-228` | `turn_count` is monotonic, not current (misleading after pruning) | |
| M2 | core | `api.py:371` | `delete_conversation` has no namespace check (cross-tenant deletion) | FIXED |
| M3 | core | `api.py:401,440` | Feedback namespace not validated against key scope | FIXED |
| M4 | core | `conversation.py:244` | `clear()` hard-deletes, bypassing GDPR retention | |
| M5 | admin | `api.py:459-493` | XSS in HTML dashboard (unescaped component data) | FIXED |
| M6 | admin | `quotas.py:88` | Chunk quota counts orphaned chunks from soft-deleted documents | |
| M7 | admin | `quotas.py:44-104` | Quota check is TOCTOU (soft limit only, document as known limitation) | |
| M8 | admin | `rls.py:80-93` | `set_rls_namespace` silently no-ops on wrong session type | FIXED |
| M9 | index | `pgvector.py:438-460` | `namespace_stats` chunk count includes soft-deleted doc chunks | |
| M10 | index | `qdrant.py:83-110` | `ensure_collection` TOCTOU on startup (catch "already exists") | |
| M11 | index | `fastembed_bm25.py:26-47` | Global sparse model singleton not thread-safe (needs Lock) | |
| M12 | index | `reindex.py:71-182` | Reindex runs in BackgroundTasks, not arq (lost on restart) | |
| M13 | index | `reindex.py:206-222` | No duplicate reindex job guard for same namespace | |
| M14 | ingest | `api.py:307-318` | Batch ingest commits per-file; mid-loop failure orphans jobs | |
| M15 | ingest | `chunking.py:76-83` | FixedSizeChunking discards element type metadata | |
| M16 | ingest | `chunking.py:202-203` | DualStrategyChunking: stale first_metadata across segments | |
| M17 | ingest | `pipeline.py:502-504` | Wrong deletion_reason on cleanup ("user_request" for pipeline failure) | |
| M18 | shared | `main.py:52-57` | PII redactor only runs at WARNING+ (INFO/DEBUG leaks PII) | |
| M19 | migrations | `0004` | `reindex_jobs` has no RLS policy despite namespace_id column | |
| M20 | admin | `middleware.py:91-136` | Audit middleware action is always NULL | |

## LOW - Minor issues

| # | Module | File:Line | Issue | Status |
|---|--------|-----------|-------|--------|
| L1 | admin | `keystore.py:118-131` | Revoked keys never removed from memory (unbounded growth) | |
| L2 | admin | `rate_limit.py:27-101` | `cleanup()` defined but never called | |
| L3 | admin | `api.py:232-304` | Bootstrap TOCTOU (low risk: one-time operation) | |
| L4 | index | `api.py:138` | `chunk_id=""` passed to ChunkEmbedding (breaks Qdrant path) | |
| L5 | index | `qdrant.py:150-153` | `document_id` from untyped metadata, fallback to UUID(int=0) | |
| L6 | index | `qdrant.py:291-319` | `delete()` always returns 0 (meaningless chunks_removed) | |
| L7 | index | `models.py:74` | Embedding dimension hardcoded to 384 | |
| L8 | ingest | `jobs.py:92-98` | File bytes in arq/Redis payload (up to 100MB) | |
| L9 | core | `api.py:326-332` | `isinstance` check on concrete class breaks Protocol abstraction | |
| L10 | core | `api.py:137-143` | SSE format inconsistent (tokens raw, errors/sources JSON) | |
| L11 | shared | `main.py:418-421` | CORS overly permissive with allow_credentials=True | |
| L12 | shared | `main.py:428` | `/metrics` endpoint unauthenticated | |
| L13 | shared | `main.py:467` | VEKTRA_CORS_ORIGINS not in VektraSettings | |

## Patterns (non-blocking, for awareness)

- `updated_at` without `onupdate` on NamespaceOrm and SourceDocumentOrm
- `IngestConflictError` is likely dead code (Phase 2 versioning replaced conflict behavior)
- Sparse search O(n*m) with no index (expected for Phase 2 prototype)
