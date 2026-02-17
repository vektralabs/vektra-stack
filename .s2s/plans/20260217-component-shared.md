# Implementation Plan: vektra_shared - Protocols, types, config, auth, ProviderRegistry

**ID**: 20260217-component-shared
**Status**: active
**Branch**: N/A
**Created**: 2026-02-17T22:42:39Z
**Updated**: 2026-02-17T22:42:39Z

## Traceability

**Source**: component-shared
**Source Type**: architecture

## References

### Requirements
- REQ-009: API error responses include remediation guidance @.s2s/requirements.md
- REQ-010: Error response envelope schema @.s2s/requirements.md
- REQ-011: Phase 1 error code registry @.s2s/requirements.md
- REQ-019: API key authentication for all components @.s2s/requirements.md
- REQ-023: API key table schema with scope support @.s2s/requirements.md
- REQ-024: Phase 1 scope enforcement @.s2s/requirements.md
- REQ-030: Authentication error response contracts @.s2s/requirements.md
- REQ-031: API scope definitions @.s2s/requirements.md
- REQ-041: Unified authentication error codes @.s2s/requirements.md
- REQ-044: Safeguard hook interface @.s2s/requirements.md
- REQ-050: Pluggable vector store backend @.s2s/requirements.md
- REQ-052: EmbeddingProvider Protocol @.s2s/requirements.md
- REQ-053: QueryPipeline Protocol @.s2s/requirements.md
- REQ-054: ChunkingStrategy Protocol @.s2s/requirements.md
- REQ-058: Content type detection via magic bytes @.s2s/requirements.md
- REQ-059: LLM graceful degradation @.s2s/requirements.md
- REQ-061: EventEmitter interface @.s2s/requirements.md
- REQ-062: ProviderRegistry pattern @.s2s/requirements.md

### Architecture
- vektra_shared: shared library, zero internal dependencies @.s2s/architecture.md
- ARCH-003: Module boundary enforcement @.s2s/architecture.md
- ARCH-029: Protocol-based extensibility @.s2s/architecture.md
- ARCH-035 to ARCH-039: 9 Protocol interface definitions @.s2s/architecture.md
- ARCH-039: ProviderRegistry @.s2s/architecture.md
- ARCH-049: SafeguardResult.modified_content @.s2s/architecture.md
- ARCH-053: SparseEmbeddingProvider Protocol @.s2s/architecture.md

### Decisions
- ADR-0005: Module boundary enforcement @.s2s/decisions/ADR-0005-module-boundary-enforcement.md
- ADR-0007: Technology stack selection @.s2s/decisions/ADR-0007-tech-stack.md
- ADR-0013: EmbeddingProvider Protocol @.s2s/decisions/ADR-0013-embedding-provider-protocol.md
- ADR-0014: QueryPipeline abstraction @.s2s/decisions/ADR-0014-query-pipeline-abstraction.md

### Dependencies
none

## Overview

vektra_shared is the foundation layer imported by every other component. It has no internal dependencies. Everything defined here becomes the stable contract that the rest of the codebase builds on. This plan covers all 9 Protocol interfaces, all shared domain types, Pydantic config schemas for all 37 environment variables, centralized auth middleware, error types, and ProviderRegistry.

## Design Notes

- ORM models (SQLAlchemy) live inside each component module, not in vektra_shared. vektra_shared owns only Pydantic API types.
- SparseEmbeddingProvider is defined here but not registered in Phase 1 (ARCH-053).
- NoOpEventEmitter and PassthroughSafeguard (no-op implementations) belong here as Phase 1 defaults.
- Config schemas use Pydantic BaseSettings with env var prefix `VEKTRA_`.
- import-linter configuration enforces that vektra_shared imports nothing from other components.

## Tasks

- [ ] Create `vektra_shared/protocols.py` with all 9 Protocol interfaces: LLMProvider, EmbeddingProvider, SparseEmbeddingProvider, VectorStoreProvider, DocumentExtractor, ChunkingStrategy, QueryPipeline, SafeguardHook, EventEmitter
- [ ] Define all shared domain types in `vektra_shared/types.py`: DocumentChunk, QueryResponse, QueryTrace, StepTrace, SourceRef, SourceDocument, SearchResult, SearchFilters, ChunkMetadata, QueryRequest, QueryEmbedding, SearchMode (DENSE/SPARSE/HYBRID), ElementType (10 values), SafeguardContext, SafeguardResult (with modified_content), Namespace, IngestJobStatus
- [ ] Define error types in `vektra_shared/errors.py`: ErrorResponse envelope matching REQ-010, error category enum (TRANSIENT/PERMANENT/CONFIGURATION/UPSTREAM per BR-001), all 13 normative error code constants (ERR-INGEST-xxx, ERR-QUERY-xxx, ERR-CONFIG-xxx, ERR-AUTH-xxx)
- [ ] Create Pydantic settings schemas in `vektra_shared/config.py`: VektraSettings (root, all 37 env vars with VEKTRA_ prefix), LLMConfig (including fallback_model, fallback_timeout_ms, context_only_enabled per REQ-059), EmbeddingConfig, VectorStoreConfig, IngestConfig (VEKTRA_MAX_FILE_SIZE_MB, VEKTRA_CHUNK_SIZE, VEKTRA_CHUNK_OVERLAP), QueryPipelineConfig, SecurityConfig
- [ ] Implement ProviderRegistry in `vektra_shared/registry.py`: generic dict-based registry with register(category, name, instance), get(category, name), list(category); raises ValueError with available options on unknown name
- [ ] Implement auth middleware in `vektra_shared/auth.py`: FastAPI dependency that extracts Bearer token, validates against ProviderRegistry-held key store, checks scope, returns key metadata; raises 401 (ERR-AUTH-001) or 403 (ERR-AUTH-003) as appropriate
- [ ] Implement NoOpEventEmitter in `vektra_shared/events.py` implementing EventEmitter Protocol (all events silently discarded, <1ms overhead)
- [ ] Implement PassthroughSafeguard in `vektra_shared/safeguards.py` implementing SafeguardHook Protocol (pre_query, post_retrieval, pre_response all pass through unchanged)
- [ ] Configure import-linter in `.importlinter` with contract: vektra_shared cannot import from vektra_core, vektra_ingest, vektra_index, vektra_admin
- [ ] Write unit tests for ProviderRegistry (register, get, list, unknown name error), auth middleware (valid token, missing token, revoked, wrong scope), and error type serialization matching REQ-010 schema
- [ ] Verify all Protocol interfaces have correct Python `typing.Protocol` signatures with async methods where required (embed_documents, embed_query, store, search, delete, execute, execute_stream, chunk, extract, emit)

## Acceptance Criteria

- [ ] All 9 Protocol interfaces importable from `vektra_shared.protocols`
- [ ] ProviderRegistry.get() raises ValueError with available options on unknown provider name
- [ ] Auth middleware returns 401 with ERR-AUTH-001 for missing/invalid/revoked tokens, 403 with ERR-AUTH-003 for insufficient scope
- [ ] ErrorResponse serializes to the exact JSON envelope defined in REQ-010
- [ ] NoOpEventEmitter.emit() completes in <1ms under test conditions
- [ ] import-linter passes: vektra_shared has no imports from other vektra components
- [ ] All 37 VEKTRA_* env vars are represented in config schemas with correct types and documented defaults

## Testing Approach

Unit tests only (no external dependencies needed). Test ProviderRegistry with mock implementations. Test auth middleware with a mock key store. Test error serialization with snapshot tests. Run import-linter as part of the test suite. No integration tests required at this stage.

## Integration Notes

Every other component imports from vektra_shared. Changes to Protocol signatures or type definitions here cascade to all consumers. Treat this module as a published API: add fields with defaults, never remove or rename without deprecation. The auth middleware implemented here is the single trust boundary wired into FastAPI at startup.
