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
| M1 | core | `conversation.py:220-228` | `turn_count` is monotonic, not current (misleading after pruning) | DEFER (Wave 5) |
| M2 | core | `api.py:371` | `delete_conversation` has no namespace check (cross-tenant deletion) | FIXED |
| M3 | core | `api.py:401,440` | Feedback namespace not validated against key scope | FIXED |
| M4 | core | `conversation.py:244` | `clear()` hard-deletes, bypassing GDPR retention | WONTFIX (InMemory dev/test only) |
| M5 | admin | `api.py:459-493` | XSS in HTML dashboard (unescaped component data) | FIXED |
| M6 | admin | `quotas.py:88` | Chunk quota counts orphaned chunks from soft-deleted documents | DEFER (Wave 3) |
| M7 | admin | `quotas.py:44-104` | Quota check is TOCTOU (soft limit only, document as known limitation) | WONTFIX |
| M8 | admin | `rls.py:80-93` | `set_rls_namespace` silently no-ops on wrong session type | FIXED |
| M9 | index | `pgvector.py:438-460` | `namespace_stats` chunk count includes soft-deleted doc chunks | DEFER (Wave 3) |
| M10 | index | `qdrant.py:83-110` | `ensure_collection` TOCTOU on startup (catch "already exists") | DEFER (infra-phase2, Wave 5) |
| M11 | index | `fastembed_bm25.py:26-47` | Global sparse model singleton not thread-safe (needs Lock) | DEFER (Wave 5) |
| M12 | index | `reindex.py:71-182` | Reindex runs in BackgroundTasks, not arq (lost on restart) | WONTFIX |
| M13 | index | `reindex.py:206-222` | No duplicate reindex job guard for same namespace | DEFER (Wave 5) |
| M14 | ingest | `api.py:307-318` | Batch ingest commits per-file; mid-loop failure orphans jobs | DEFER (Wave 5) |
| M15 | ingest | `chunking.py:76-83` | FixedSizeChunking discards element type metadata | DEFER (Wave 2) |
| M16 | ingest | `chunking.py:202-203` | DualStrategyChunking: stale first_metadata across segments | FIXED |
| M17 | ingest | `pipeline.py:502-504` | Wrong deletion_reason on cleanup ("user_request" for pipeline failure) | DEFER (Wave 2) |
| M18 | shared | `main.py:52-57` | PII redactor only runs at WARNING+ (INFO/DEBUG leaks PII) | DEFER (Wave 5) |
| M19 | migrations | `0004` | `reindex_jobs` has no RLS policy despite namespace_id column | DEFER |
| M20 | admin | `middleware.py:91-136` | Audit middleware action is always NULL | DEFER (Wave 2) |

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

## CodeRabbit/Gemini inline findings

Inline comments posted directly on diff lines. Comment ID links to the thread.

| # | Severity | File:Line | Issue | Comment ID | Review ID | Status |
|---|----------|-----------|-------|------------|-----------|--------|
| CR1 | Critical | `rls.py:53` | RLS namespace from client input without auth validation | 2874743808 | 3878766241 | WONTFIX |
| CR2 | Major | `rls.py:70` | BaseHTTPMiddleware body consumption (Starlette limitation) | 2874743814 | 3878766241 | DEFER (infra-phase2) |
| CR3 | Major | `reindex.py:146` | Reindex loop is stub (doesn't re-embed) | 2874743864 | 3878766241 | WONTFIX (by design) |
| CR4 | Major | `ingest/api.py:623` | `zip(chunks, embeddings)` truncates silently on length mismatch | 2874743885 | 3878766241 | FIXED |
| CR5 | Major | `index/api.py:62` | Sparse vector `indices`/`values` length not validated | 2874773746 | 3878803063 | FIXED |
| CR6 | Major | `jobs.py:231` | Cleanup SELECT-then-DELETE pattern (memory, not correctness) | 2874743911 | 3878766241 | DEFER (Wave 5) |
| CR7 | Major | `test_unstructured.py:39` | Module-level sys.modules patch leaks across tests | 2874743944 | 3878766241 | DEFER |
| CR8 | Minor | `quotas.py:28` | No guard against negative quota deltas | 2874773736 | 3878803063 | FIXED |
| CR9 | Minor | `conversation.py:64` | `max_turns` accepts 0/negative values | 2874773745 | 3878803063 | FIXED |
| CR10 | Critical | `0003_rls_policies.py:80` | NULL vs '' in RLS COALESCE (Gemini) | 2874749003 | 3878772110 | FIXED (0a3acfa) |
| CR11 | High | `keys.py:79` | Blocking argon2 verify_key (Gemini) | 2874749014 | 3878772110 | FIXED (0a3acfa) |
| CR12 | Major | `core/api.py:143` | SSE drops trace chunks | 2874743830 | 3878766241 | FIXED (9a0cbac) |
| CR13 | Major | `rls.py:54` | Middleware not setting namespace on request state | 2874743798 | 3878766241 | FIXED |
| CR14 | Major | `conversation.py:173` | Soft-deleted conversations remain writable/readable | 2874743842 | 3878766241 | FIXED |
| CR15 | Major | `qdrant.py` | Reuse first chunk's document_id for all point payloads | 2874743850 | 3878766241 | FIXED (0a3acfa) |
| CR16 | Major | `reindex.py:98` | Reindex job updates/status not namespace-scoped | 2874743856 | 3878766241 | FIXED (f089f04) |
| CR17 | Major | `startup.py:135` | Hardcoded "default" provider name may not match | 2874743867 | 3878766241 | WONTFIX (convention) |
| CR18 | Major | `ingest/api.py` | Batch API reports `pending` when enqueue already failed | 2874743873 | 3878766241 | FIXED (0a3acfa) |
| CR19 | Critical | `ingest/api.py:407` | Commit soft-deletes before calling vector-store delete | 2874743879 | 3878766241 | FIXED (0a3acfa) |
| CR20 | Major | `chunking.py:203` | Chunk metadata pinned to first text element | 2874743889 | 3878766241 | FIXED (= M16) |
| CR21 | Major | `unstructured.py` | Empty-text guard runs before table HTML fallback | 2874743897 | 3878766241 | FIXED |
| CR22 | Major | `jobs.py:135` | Phase progress updates resetting `started_at` | 2874743904 | 3878766241 | FIXED (coalesce) |
| CR23 | Critical | `pipeline.py` | Superseding old data before successful re-index | 2874743917 | 3878766241 | FIXED (0a3acfa) |
| CR24 | Major | `pipeline.py:391` | `document.failed` emission fire-and-forget | 2874743932 | 3878766241 | FIXED (dba8b87) |
| CR25 | Major | `auth.py` | Set `request.state.key_id` before scope/rate-limit | 2874743949 | 3878766241 | FIXED |
| CR26 | Major | `quotas.py:50` | Make quota enforcement atomic per namespace | 2874773739 | 3878803063 | WONTFIX (= M7) |
| CR27 | Critical | `fastembed_bm25.py:35` | Global sparse model not thread-safe | 2874773750 | 3878803063 | DEFER (= M11) |
| CR28 | Critical | `qdrant.py:49` | Qdrant collection ensure TOCTOU | 2874773752 | 3878803063 | DEFER (= M10) |
| CR29 | Critical | `ingest/api.py:914` | Audit log await pattern | 2874773758 | 3878803063 | FIXED |
| CR30 | Major | `chunking.py` | Ruff `no-redef` on `tokens` | 2874773764 | 3878803063 | FIXED |
| CR31 | Major | `markdown.py:79` | Heading detection inside fenced code blocks | 2874773768 | 3878803063 | FIXED (0a3acfa) |
| CR32 | Minor | `test_pipeline.py:706` | Test name misleading (doesn't verify event emission) | 2874773770 | 3878803063 | FIXED (0dafb53) |
| CR33 | Minor | `test_versioning.py:459` | Test collects stmts but never inspects them | 2874773773 | 3878803063 | WONTFIX (by design) |
| CR34 | Minor | `keys.py` | Documentation inconsistency on cache behavior | 2874931209 | 3878966774 | FIXED |
| CR35 | Minor | `pgvector.py:224` | Operator usage pattern | 2874931214 | 3878966774 | WONTFIX |
| CR36 | Minor | `pgvector.py:337` | Normalize list filter values to strings | 2874931220 | 3878966774 | WONTFIX |

## Subsequent review round inline findings (rounds 2-8)

| # | Severity | File:Line | Issue | Comment ID | Review ID | Status |
|---|----------|-----------|-------|------------|-----------|--------|
| CR37 | Minor | `keystore.py` | Inclusive expiry check at runtime | 2878329959 | 3882649738 | DEFER (Wave 5) |
| CR38 | Major | `qdrant.py:95` | Qdrant schema validation on startup | 2878329967 | 3882649738 | DEFER (Wave 5) |
| CR39 | Critical | `reindex.py:230` | Enforce namespace binding on reindex job creation | 2878518595 | 3882867623 | FIXED (f089f04) |
| CR40 | Minor | `middleware.py:99` | Use `rls_namespace` as JSON metadata key | 2879238157 | 3883675871 | WONTFIX (by design) |
| CR41 | Minor | `reindex.py` | Docstring doesn't match skeleton behavior | 2879318630 | 3883765357 | DEFER (Wave 5) |
| CR42 | Major | `reindex.py:128` | Unbounded document-ID materialization in memory | 2879318643 | 3883765357 | DEFER (Wave 5) |
| CR43 | Minor | `test_pipeline.py:247` | Test assertion improvement | 2879365878 | 3883815058 | DEFER |
| CR44 | Minor | `reindex.py` | `run_reindex` docstring alignment | 2879465162 | 3883926320 | DEFER (Wave 5) |
| CR45 | Minor | `admin/api.py:96` | Validate `expires_at` is in the future | 2879566058 | 3884038356 | FIXED (51433e5) |
| CR46 | Major | `pipeline.py:68` | `IngestResult.version` wrong for `exists`/`alias` | 2879566069 | 3884038356 | FALSE POSITIVE |
| CR47 | Minor | `pipeline.py:211` | Step 2 comment no longer matches runtime | 2879566077 | 3884038356 | FIXED (51433e5) |
| CR48 | Critical | `pipeline.py:259` | Expand rollback/error handling post-supersede | 2879566085 | 3884038356 | DEFER (Wave 2) |
| CR49 | Major | `admin/api.py:96` | Bootstrap validation gap | 2880530743 | 3885099127 | DEFER (Wave 5) |
| CR50 | Major | `jobs.py:207` | Negative `retention_days` creates future cutoff | 2880530752 | 3885099127 | FIXED |
| CR51 | Major | `pipeline.py:308` | Map commit-time TOCTOU to IngestConflictError | 2880530755 | 3885099127 | DEFER (Wave 2) |
| CR52 | Major | `pipeline.py` | Best-effort event delivery not fully honored | 2880530756 | 3885099127 | WONTFIX (ARCH-038) |
| CR53 | Minor | `test_versioning.py:489` | Ruff format check failing | 2880530758 | 3885099127 | FIXED |
| CR54 | Major | `pipeline.py` | Broader exception handling for event emission | 2880658376 | 3885237193 | FIXED (dba8b87) |
| CR55 | Major | `admin/api.py:168` | Deep-health doesn't set `request.state.key_id` | 2883881771 | 3889528234 | DEFER (Wave 2) |
| CR56 | Minor | `ingest/api.py:197` | Audit `status_code` mismatch (422 vs actual) | 2883881781 | 3889528234 | FALSE POSITIVE |
| CR57 | Minor | `unstructured.py:197` | Run formatter before merge | 2883881785 | 3889528234 | FIXED (9a0cbac) |
| CR58 | Major | `index/api.py:341` | `stats()` not namespace-scoped (cross-tenant reads) | 2884559008 | 3890237157 | FIXED (d64537f) |
| CR59 | Minor | `chunking.py:276` | Parent chunk UUID not stored in metadata | 2884559016 | 3890237157 | DEFER |

## Outside-diff findings (in review bodies, not inline comments)

Items embedded in review body text (no individual comment ID, only review ID).

| # | Severity | File:Line | Issue | Review ID | Status |
|---|----------|-----------|-------|-----------|--------|
| OD1 | Minor | `pipeline.py:289-294` | Unsupported-type error text stale after Markdown support | 3878766241 | DEFER (Wave 2) |
| OD2 | Major | `pgvector.py:100-130` | `raw_filters` accepted but never applied (silent drop) | 3878766241 | DEFER (Wave 5, ARCH-051) |
| OD3 | Major | `admin/api.py:296-302` | Normalize `expires_at` to prevent naive datetime crash | 3878766241 | FIXED (= CR45, 51433e5) |
| OD4 | Major | `core/api.py:211-217` | Namespace not bound to authenticated key | 3878766241 | FIXED (= M2+M3+H5) |
| OD5 | Major | `pgvector.py:346-350` | Enforce namespace equality in source document JOIN | 3878966774 | DEFER (Wave 5) |
| OD6 | Minor | `jobs.py:19-264` | CI blocker: jobs.py needs formatting | 3882649738 | FIXED (ruff format passes) |
| OD7 | Major | `keystore.py:95-111` | `add_key()` missing `rate_limit_rpm` propagation | 3882649738 | FALSE POSITIVE (already present) |

## Minor comments (in review bodies)

Test improvements and minor code suggestions from review body sections. All low priority.

| # | File:Line | Issue | Review ID | Status |
|---|-----------|-------|-----------|--------|
| MN1 | `test_granular_api.py:92-93` | Add test case for content-type detection fallback | 3878766241 | DEFER |
| MN2 | `test_granular_api.py:46-60` | Finalize mocked DB session lifecycle in dependency override | 3878766241 | DEFER |
| MN3 | `test_cleanup.py:42-50` | Add assertion that DB is never touched when retention unset | 3878766241 | DEFER |
| MN4 | `test_markdown.py:49-74` | Test intent and assertions conflict in empty-section case | 3878766241 | DEFER |
| MN5 | `markdown.py:18` | Heading parsing misses common valid Markdown forms | 3878766241 | DEFER |
| MN6 | `test_versioning.py:360-435` | Test doesn't assert `deletion_reason='superseded'` | 3878766241 | DEFER |
| MN7 | `test_pipeline.py:619-684` | Test doesn't verify `document.failed` event emission | 3878766241 | DEFER |
| MN8 | `types.py:74-94` | Docstring mentions `document_version` not declared in TypedDict | 3878766241 | DEFER |
| MN9 | `pgvector.py:329-337` | Normalize list filter values to strings before JSONB comparison | 3878766241 | WONTFIX (= CR36) |

## Duplicate comments (in review bodies, confirming inline findings)

These are CodeRabbit re-raising inline findings across subsequent review rounds. No action needed, listed for completeness.

| # | File:Line | Issue | Review ID | Maps to |
|---|-----------|-------|-----------|---------|
| `chunking.py:188-213` | Chunk metadata provenance pinned across segments | 3878803063 | CR20, M16 |
| `conversation.py:146-183` | Block reads/writes for soft-deleted conversations | 3878803063 | CR14 |
| `qdrant.py:131-156` | Use each chunk's `document_id` in point payloads | 3878803063 | CR15 |
| `ingest/api.py:632-633` | Fail fast on embedding/chunk count mismatch | 3878803063 | CR4 |
| `ingest/api.py:404-416` | Move vector-store deletion after DB commit | 3878803063 | CR19 |
| `ingest/api.py:777-816` | Return enqueue outcome in batch item status | 3878803063 | CR18 |
| `shared/auth.py` | Set `request.state.key_id` before scope enforcement | 3878803063 | CR25 |
| `jobs.py` | Phase progress updates, cleanup pattern | 3878803063 | CR22, CR6 |
| `chunking.py:188-203` | Chunk provenance metadata pinned to first text run | 3878966774 | CR20, M16 |
| `markdown.py:42-50` | Fence state tracking lossy for mixed fence cases | 3882649738 | CR31 |
| `pipeline.py:242-252` | Critical rollback gap after supersede commit | 3882649738 | CR23, CR48 |
| `keys.py:25-27` | Cache comment mismatches actual behavior | 3882649738 | CR34 |
| `qdrant.py:131-154` | Reject chunks missing `metadata.document_id` | 3882649738 | CR15 |
| `conversation.py:155-181` | TOCTOU in `get_history` (two-step check+fetch) | 3890278302 | CR14 refinement |

## Nitpick comments (in review bodies)

Very low-priority style and test suggestions from review body sections.

| # | File | Issue | Review ID | Status |
|---|------|-------|-----------|--------|
| NP1 | `test_budget.py` | Rename test to match what it verifies | 3878766241 | DEFER |
| NP2 | `test_api.py` | Use `monkeypatch` for env overrides | 3878766241 | DEFER |
| NP3 | `test_ttlcache.py` | Thread-safety for dict assignment | 3878766241 | DEFER |
| NP4 | `keys.py` | Security implications of caching failed verifications | 3878766241 | DEFER |
| NP5 | `keystore.py` | Evict expired keys on first successful match | 3878766241 | DEFER |
| NP6 | `test_startup.py` | Mock registry doesn't validate sparse provider name | 3878766241 | DEFER |
| NP7 | `test_qdrant_provider.py` | Sparse fallback test verifies implementation detail | 3878766241 | DEFER |
| NP8 | `index/api.py` | Logger should be at module level, not inside function | 3878766241 | DEFER |
| NP9 | `test_feedback_api.py` | SSE disconnect test doesn't verify disconnect handling | 3878766241 | DEFER |
| NP10 | `markdown.py` | Simplify metadata construction | 3878803063 | DEFER |
| NP11 | `test_versioning.py` | Extract shared test helpers to conftest.py | 3878803063 | DEFER |
| NP12 | `test_dual_chunking.py` | Add regression test for metadata provenance | 3878803063 | DEFER |
| NP13 | `fastembed_bm25.py` | Thread-safe per-model cache instead of global slot | 3878803063 | DEFER (= M11) |
| NP14 | `test_qdrant_provider.py` | Add multi-chunk document_id payload regression test | 3878803063 | DEFER |
| NP15 | `test_api.py` | Add regression test for batch enqueue failure reporting | 3878803063 | DEFER |
| NP16 | `test_quotas.py` | Assert `ERR_QUOTA_001` in failure-path tests | 3878966774 | DEFER |
| NP17 | `main.py` | Good implementation (positive feedback, no action) | 3878966774 | N/A |
| NP18 | `unstructured.py` | Validate OCR runtime dependencies in health_check | 3878966774 | DEFER |
| NP19 | `ingest/api.py` | Align batch item status contract with returned values | 3882649738 | DEFER |
| NP20 | `conversation.py` | Document that `get_metadata` returns soft-deleted conversations | 3882649738 | DEFER |
| NP21 | `test_keys.py` | Rename test to TTLCache terminology | 3882649738 | DEFER |
| NP22 | `test_ttlcache.py` | Module docstring overstates TTL-expiration coverage | 3882649738 | DEFER |
| NP23 | `config.py:427-429` | Qdrant fields duplicated in root VektraSettings | 3890278302 | DEFER (Wave 5) |
| NP24 | `index/api.py:221-223` | Dense embedding computed eagerly even for SPARSE mode | 3890436911 | DEFER (Wave 2) |

## Deferred items: target wave mapping

All DEFER items now have a wave assignment or are explicitly marked opportunistic/Phase 3.

| Target | Items | Description |
|--------|-------|-------------|
| Wave 2 (core-pipeline-v2 + admin-ui) | CR48, CR51, M15, M17, M20, CR55, OD1, NP24 | Pipeline rollback, IntegrityError, chunking metadata, deletion_reason, audit action, deep-health key_id, error text, lazy dense embedding |
| Wave 3 (component-analytics) | M6, M9 | Quota/stats counting soft-deleted documents |
| Wave 5 (infra-phase2) | CR2, M10/CR28, CR38, CR41, CR42, CR44, M19, M1, M11/NP13, M13, M14, M18, CR6, CR37, CR49, OD2, OD5, NP23 | ASGI middleware, Qdrant TOCTOU/schema, reindex, RLS, turn_count, thread-safety, batch atomicity, PII redactor, cleanup pattern, auth hardening, raw_filters, JOIN namespace, Qdrant config cleanup |
| Opportunistic / Phase 3 | CR7, CR43, CR59, L1-L9, L11-L13, MN1-MN8, NP1-NP22 | Test isolation, test assertions, parent chunk UUID, LOW improvements, test/style nitpicks |

## Patterns (non-blocking, for awareness)

- `updated_at` without `onupdate` on NamespaceOrm and SourceDocumentOrm
- `IngestConflictError` is likely dead code (Phase 2 versioning replaced conflict behavior)
- Sparse search O(n*m) with no index (expected for Phase 2 prototype)

## Summary

- **Total findings**: 7H + 20M + 13L + 59CR + 7OD + 9MN + 14DUP + 24NP = 153
- **Fixed**: 7H + 6M + 1L + 33CR + 2OD = 49
- **WONTFIX**: 3M + 8CR + 1MN = 12
- **FALSE POSITIVE**: 3CR + 1OD = 4
- **DEFER (all wave-assigned)**: 11M + 12L + 15CR + 5OD + 8MN + 23NP = 74
- **DUP** (no action): 14
