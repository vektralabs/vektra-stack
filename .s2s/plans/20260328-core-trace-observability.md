# Implementation Plan: QueryTrace persistence and conversation observability

**ID**: 20260328-core-trace-observability
**Status**: completed
**Branch**: fix/query-trace-observability
**PR**: #53 (merged 2026-04-03)
**Created**: 2026-03-28T12:00:00Z
**Updated**: 2026-04-03T18:00:00Z

## Traceability

**Source**: BUG-013, DEBT-011
**Source Type**: backlog

## References

### Requirements
- REQ-060: QueryTrace for RAG observability @.s2s/requirements.md
- REQ-051: Operator privacy (QueryTrace has no query/response text) @.s2s/requirements.md

### Architecture
- ARCH-041: Audit/analytics separation via QueryTrace @.s2s/architecture.md
- ARCH-017: Audit log vs analytics separation @.s2s/architecture.md
- ARCH-031: Conversation encryption at rest @.s2s/architecture.md

### Decisions
- ADR-0005: Module boundary enforcement @.s2s/decisions/ADR-0005-module-boundary-enforcement.md
- ADR-0017: Audit/analytics separation via QueryTrace @.s2s/decisions/ADR-0017-audit-analytics-separation.md
- ADR-0022: SQLAlchemy 2.0 async with asyncpg @.s2s/decisions/ADR-0022-orm-sqlalchemy-async.md

### Dependencies
- 20260301-component-analytics.md: AnalyticsService and QueryTraceOrm already implemented
- 20260301-core-conversations.md: PersistentConversationStore and conversation_turns table

## Overview

`AnalyticsService.store_trace()` exists and is tested but is never called. Both pipelines (Simple + Advanced) build QueryTrace objects in execute() and execute_stream(), but the traces are logged to structlog and discarded. The `query_traces` table is always empty, making post-hoc diagnosis of query behavior impossible.

Additionally, the `response_id` column in `conversation_turns` is never populated (always NULL), and there is no admin API to read decrypted conversation turns. This plan wires existing components together and adds a minimal admin endpoint to close these observability gaps.

## Design notes

### Trace persistence location

Pipelines are framework-agnostic and must not import `vektra_analytics` (ADR-0005). Trace persistence is done in the API layer (vektra-core/api.py and vektra-learn/api.py), which already has access to `app.state.analytics_service` and `app.state.db_session_factory`.

BUG-013 acceptance criteria says "pipeline calls store_trace" but the intent is "traces get persisted after pipeline execution". The API layer is the correct location per module boundaries.

### Streaming path

In streaming mode, the trace is emitted as a QueryChunk with `type="trace"` near the end of the stream. The SSE generators (`_sse_generator` in core, `_learn_sse_generator` in learn) intercept this chunk and persist it. This happens after all tokens have been streamed, so there is no impact on token latency.

### trace_from_dict helper

Both core and learn API layers need to reconstruct a `QueryTrace` from the dict emitted by `_trace_to_dict()`. Since `vektra-learn` cannot import from `vektra_core` (ADR-0005), the reconstruction function lives in `vektra_shared/types.py` next to the `QueryTrace` dataclass.

### Admin endpoint duck-typing

The admin conversation turns endpoint (`vektra-admin`) cannot import from `vektra_core` (ADR-0005). It accesses the conversation store via the registry and uses `hasattr` duck-typing, following the same pattern `vektra-learn` uses for `ensure_conversation`.

### Config flag

`VEKTRA_ANALYTICS_STORE_TRACES` controls trace persistence. `None` (default) means auto-detect: on when `VEKTRA_ENV=development`, off otherwise. Explicit `true`/`false` overrides auto-detection.

### Best-effort semantics

All trace persistence is wrapped in try/except. A database failure must never turn a successful query into a 500 error. Failures are logged as warnings.

## Tasks

### Config and wiring
- [x] Add `analytics_store_traces: bool | None` to `ObservabilityConfig` and `VektraSettings` in `vektra-shared/src/vektra_shared/config.py`
- [x] Resolve flag at startup in `vektra-app/src/vektra_app/main.py` and set `app.state.store_traces_enabled`
- [x] Add `trace_from_dict()` function in `vektra-shared/src/vektra_shared/types.py`

### Trace persistence - core endpoint
- [x] Add best-effort `store_trace()` call after `pipeline.execute()` in `vektra-core/src/vektra_core/api.py` (non-streaming path)
- [x] Extend `_sse_generator` to accept trace persistence params and persist on `chunk.type == "trace"` (streaming path)

### Trace persistence - learn endpoint
- [x] Add best-effort `store_trace()` call after `pipeline.execute()` in `vektra-learn/src/vektra_learn/api.py` (non-streaming path)
- [x] Extend `_learn_sse_generator` to accept trace persistence params and persist on `chunk.type == "trace"` (streaming path)

### response_id in conversation turns
- [x] Add `response_id: UUID | None = None` keyword-only arg to `ConversationStore` Protocol, `InMemoryConversationStore`, and `PersistentConversationStore` `add_turn()` methods in `vektra-core/src/vektra_core/conversation.py`
- [x] Pass `response_id=response_id` in all 4 `add_turn()` call sites: `pipeline.py:561`, `pipeline.py:867`, `advanced_pipeline.py:530`, `advanced_pipeline.py:695`

### Admin conversation turns endpoint
- [x] Add `get_turns_detail()` method to `PersistentConversationStore` in `vektra-core/src/vektra_core/conversation.py` (decrypts question/answer, returns full metadata)
- [x] Add `GET /api/v1/admin/conversations/{conversation_id}/turns` endpoint in `vektra-admin/src/vektra_admin/api.py` (admin scope, duck-typed access to conversation store)

### Tests
- [x] New `vektra-core/tests/test_trace_persistence.py`: store_trace called when enabled, not called when disabled, failure doesn't propagate
- [x] Update `vektra-core/tests/test_conversation.py`: add_turn with response_id, get_turns_detail
- [x] New `vektra-admin/tests/test_admin_turns.py`: GET endpoint returns decrypted turns, 403 for non-admin

## Acceptance criteria

- [x] Query traces persisted to DB for all pipelines (simple + advanced, sync + stream) via both `/api/v1/query` and `/api/v1/learn/query`
- [x] `response_id` populated in `conversation_turns` on turn save
- [x] Admin endpoint to read decrypted conversation turns with full metadata
- [x] Trace persistence configurable via `VEKTRA_ANALYTICS_STORE_TRACES` (auto: on in dev, off in prod)
- [x] Trace persistence is best-effort (DB failure does not turn a successful query into a 500)
- [x] Existing `GET /api/v1/traces/{response_id}` returns persisted traces (no new endpoint needed)
- [x] `make lint` and `make test` pass

## Testing approach

1. Unit tests: mock AnalyticsService, verify store_trace called/not-called based on config flag, verify failure isolation
2. Unit tests: verify response_id written to conversation_turns ORM
3. Integration tests: admin endpoint returns decrypted conversation content
4. Manual verification: run stack, execute queries (streaming + non-streaming), verify rows in `query_traces` and `conversation_turns.response_id` via psql

## Integration notes

- No new database migrations needed. `query_traces` table and `conversation_turns.response_id` column already exist (20260301-database-phase2).
- No new Protocol interfaces. Uses existing `AnalyticsService.store_trace()` and `ConversationStore.add_turn()`.
- Existing analytics endpoints (`GET /api/v1/traces/{response_id}`, `GET /api/v1/traces`, `GET /api/v1/metrics`) become functional once traces are persisted.
