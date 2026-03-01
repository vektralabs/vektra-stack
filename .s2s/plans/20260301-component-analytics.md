# Implementation Plan: vektra-analytics - QueryTrace storage, metrics, and reporting API

**ID**: 20260301-component-analytics
**Status**: pending
**Branch**: N/A
**Created**: 2026-03-01T14:30:09Z
**Updated**: 2026-03-01T14:30:09Z

## Traceability

**Source**: component-analytics
**Source Type**: architecture

## Provides / Requires

**Provides**:
- QueryTrace dedicated storage and query API (consumers: component-learn, infra-phase2)
- Metrics aggregation endpoints (consumers: component-learn, infra-phase2)
- Reporting API for operator dashboards (consumers: infra-phase2)

**Requires**:
- 20260301-database-phase2.md: query_traces table exists with Alembic migration
- 20260301-core-pipeline-v2.md: QueryTrace emission to dedicated storage (not just structlog)

## References

### Requirements
- REQ-060: QueryTrace for RAG observability @.s2s/requirements.md
- REQ-051: Operator privacy (QueryTrace has no query/response text) @.s2s/requirements.md

### Architecture
- ARCH-041: Audit/analytics separation via QueryTrace @.s2s/architecture.md
- ARCH-003: Module boundary enforcement @.s2s/architecture.md

### Decisions
- ADR-0001: Hybrid monorepo strategy @.s2s/decisions/ADR-0001-hybrid-monorepo-strategy.md
- ADR-0005: Module boundary enforcement @.s2s/decisions/ADR-0005-module-boundary-enforcement.md
- ADR-0017: Audit/analytics separation via QueryTrace @.s2s/decisions/ADR-0017-audit-analytics-separation.md
- ADR-0022: SQLAlchemy 2.0 async with asyncpg @.s2s/decisions/ADR-0022-orm-sqlalchemy-async.md

### Dependencies
- 20260301-database-phase2.md
- 20260301-core-pipeline-v2.md

## Overview

vektra-analytics is a new component that provides dedicated QueryTrace storage and a query/metrics API for RAG observability. In Phase 1, QueryTrace data is emitted via structlog as JSON events. This plan transitions to persistent storage in the `query_traces` table (created by database-phase2) and exposes REST endpoints for querying traces, aggregating metrics, and generating reports.

The component follows the same scaffold pattern as existing components: a workspace member with its own `pyproject.toml`, `src/vektra_analytics/` package, import-linter boundaries, and unit tests. It depends only on `vektra-shared` for types and Protocols. The `query_traces` table is owned by this component at the ORM level (models defined here), even though the migration is created by database-phase2.

QueryTrace data is GDPR-safe by design (ARCH-041, ADR-0017): it contains no query text, no response text, and no chunk content. Only chunk IDs, relevance scores, per-step timing, model identifiers, and prompt version hashes. This separation allows shorter retention than audit logs, controlled via `VEKTRA_ANALYTICS_RETENTION_DAYS`.

## Design Notes

### Package structure

```
vektra-analytics/
  pyproject.toml
  README.md
  src/vektra_analytics/
    __init__.py
    models.py        # SQLAlchemy ORM: QueryTraceOrm, StepTraceOrm
    service.py       # AnalyticsService: store, query, aggregate
    api.py           # FastAPI router: /api/v1/traces, /api/v1/metrics
  tests/
    __init__.py
    test_service.py
    test_api.py
```

### pyproject.toml

Follow the pattern from `vektra-core/pyproject.toml`:

```toml
[project]
name = "vektra-analytics"
version = "0.2.0-dev"
description = "QueryTrace storage, metrics aggregation, and reporting API"
readme = "README.md"
requires-python = ">=3.12"
dependencies = [
    "vektra-shared",
    "fastapi>=0.115",
    "sqlalchemy>=2.0",
    "structlog>=24.0",
]

[build-system]
requires = ["hatchling"]
build-backend = "hatchling.build"

[tool.hatch.build.targets.wheel]
packages = ["src/vektra_analytics"]

[tool.pytest.ini_options]
testpaths = ["tests"]
asyncio_mode = "auto"
asyncio_default_fixture_loop_scope = "module"

[tool.uv.sources]
vektra-shared = { workspace = true }

[dependency-groups]
dev = [
    "pytest>=8.0",
    "pytest-asyncio>=1.0",
    "httpx>=0.27",
]
```

### ORM models (models.py)

```python
class QueryTraceOrm(Base):
    __tablename__ = "query_traces"
    id: Mapped[uuid.UUID] = mapped_column(primary_key=True, default=uuid.uuid4)
    response_id: Mapped[uuid.UUID] = mapped_column(unique=True, index=True)
    total_duration_ms: Mapped[int]
    llm_model: Mapped[str]
    prompt_version: Mapped[str]
    chunks_retrieved: Mapped[dict] = mapped_column(JSONB)  # list[ChunkRef] serialized
    steps: Mapped[dict] = mapped_column(JSONB)  # list[StepTrace] serialized
    namespace: Mapped[str] = mapped_column(default="default", index=True)
    created_at: Mapped[datetime] = mapped_column(server_default=func.now(), index=True)
```

Store steps and chunks_retrieved as JSONB columns for query flexibility. No separate `step_traces` table: the step data is always read with the parent trace and the overhead of a join is unnecessary for this use case.

### AnalyticsService

```python
class AnalyticsService:
    async def store_trace(self, trace: QueryTrace, namespace: str = "default") -> None: ...
    async def get_trace(self, response_id: UUID) -> QueryTrace | None: ...
    async def list_traces(
        self,
        namespace: str | None = None,
        from_dt: datetime | None = None,
        to_dt: datetime | None = None,
        llm_model: str | None = None,
        min_duration_ms: int | None = None,
        limit: int = 50,
        offset: int = 0,
    ) -> list[QueryTrace]: ...
    async def get_metrics(
        self,
        namespace: str | None = None,
        from_dt: datetime | None = None,
        to_dt: datetime | None = None,
    ) -> MetricsResponse: ...
```

### MetricsResponse type

Defined as a Pydantic model in `service.py` (API-facing, not a shared type):

```python
class MetricsResponse(BaseModel):
    total_queries: int
    avg_latency_ms: float
    p95_latency_ms: float
    avg_retrieval_score: float    # mean of max chunk score per trace
    queries_per_hour: float       # rate in the selected window
    model_distribution: dict[str, int]  # model name -> query count
    period_start: datetime
    period_end: datetime
```

### API endpoints

All endpoints require `admin` scope (analytics data is operational, not user-facing).

| Method | Path | Description |
|--------|------|-------------|
| GET | /api/v1/traces | List traces with filters (namespace, from, to, model, min_duration_ms, limit, offset) |
| GET | /api/v1/traces/{response_id} | Get single trace by response_id |
| GET | /api/v1/metrics | Aggregated metrics (avg/p95 latency, retrieval quality, throughput) |

### Import-linter boundary

vektra_analytics can import from vektra_shared only. Add to root `pyproject.toml`:

```toml
[[tool.importlinter.contracts]]
name = "vektra_analytics must not import from other vektra components"
type = "forbidden"
source_modules = ["vektra_analytics"]
forbidden_modules = ["vektra_core", "vektra_ingest", "vektra_index", "vektra_admin", "vektra_app"]
```

Also update the "No component shall import the app entrypoint" contract to include vektra_analytics.

### Retention

`VEKTRA_ANALYTICS_RETENTION_DAYS` controls how long traces are stored. When set, an arq cleanup job (registered in infra-phase2) deletes traces older than the configured period. This plan does not implement the cleanup job itself; it exposes a `delete_before(cutoff: datetime)` method on AnalyticsService for the job to call.

## Tasks

- [ ] Create `vektra-analytics/` directory with `pyproject.toml`, `README.md`, `src/vektra_analytics/__init__.py`
- [ ] Add `vektra-analytics` to root `pyproject.toml` workspace members list (`[tool.uv.workspace]`)
- [ ] Add `vektra_analytics` to root `pyproject.toml` import-linter contracts (new forbidden contract + update existing contracts to include vektra_analytics)
- [ ] Add `vektra_analytics` to `[tool.ruff.lint.isort] known-first-party` and `[tool.coverage.run] source` in root pyproject.toml
- [ ] Implement `vektra_analytics/models.py` with QueryTraceOrm (SQLAlchemy ORM model for `query_traces` table)
- [ ] Implement `vektra_analytics/service.py` with AnalyticsService: store_trace, get_trace, list_traces, get_metrics, delete_before
- [ ] Implement p95 latency computation in get_metrics using SQL percentile_cont or in-memory calculation from recent traces
- [ ] Implement `vektra_analytics/api.py` with FastAPI router: GET /api/v1/traces, GET /api/v1/traces/{response_id}, GET /api/v1/metrics
- [ ] Add admin scope auth dependency to all analytics endpoints (reuse pattern from vektra_admin/api.py)
- [ ] Write unit tests for AnalyticsService: store, retrieve, list with filters, metrics aggregation (mock database session)
- [ ] Write unit tests for API router: auth, response format, query parameter validation
- [ ] Verify import-linter passes with new boundaries: `uv run lint-imports`

## Acceptance Criteria

- [ ] `vektra-analytics` is a workspace member in root pyproject.toml
- [ ] Import-linter enforces that vektra_analytics imports only from vektra_shared
- [ ] GET /api/v1/traces returns paginated traces with namespace, time range, model, and duration filters
- [ ] GET /api/v1/traces/{response_id} returns a single trace or 404
- [ ] GET /api/v1/metrics returns avg latency, p95 latency, avg retrieval score, queries per hour, and model distribution for the requested time window
- [ ] All endpoints require admin scope authentication
- [ ] QueryTrace data contains no query text, no response text, no chunk content (ARCH-041 privacy)
- [ ] AnalyticsService.delete_before() removes traces older than a given cutoff date
- [ ] Unit tests pass with 80%+ coverage on vektra_analytics

## Testing Approach

Unit tests with mocked SQLAlchemy async sessions cover AnalyticsService methods: storage, retrieval, filtering, aggregation, and deletion. API tests use FastAPI TestClient with httpx to verify endpoint routing, auth enforcement, query parameter parsing, and response format. No integration tests in this plan; integration with the actual database and core-pipeline-v2 QueryTrace emission is verified during infra-phase2.

## Integration Notes

core-pipeline-v2 emits QueryTrace objects after each query execution. The integration point is in vektra-app's lifespan: AnalyticsService is instantiated and registered in ProviderRegistry. The query pipeline (or a middleware) calls `analytics_service.store_trace(trace)` after pipeline execution completes. This wiring happens in infra-phase2, not in this plan.

The admin-ui plan (Wave 2) may render analytics data in dashboard pages. It does so by calling GET /api/v1/metrics via REST, consistent with the ADR-0024 constraint (no business logic in templates).

## Notes

<!-- Progress notes during implementation -->
