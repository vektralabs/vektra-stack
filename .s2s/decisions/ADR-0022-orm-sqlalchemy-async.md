# ADR-0022: SQLAlchemy 2.0 async with asyncpg for ORM layer

**Status**: accepted
**Date**: 2026-02-09
**Context**: OQ-017 resolution, pre-implementation decision

## Context

OQ-017 required a decision on the ORM layer before implementation: SQLAlchemy 2.0 async (with asyncpg) or SQLModel. This decision affects every database model, query, and migration in the system.

### Vektra's database requirements

The persistence layer must support:

1. **pgvector**: `Vector(384)` columns with HNSW/IVFFlat indexes for semantic search
2. **pgcrypto**: `pgp_sym_encrypt/decrypt` for conversation encryption at rest (ARCH-031)
3. **JSONB with GIN indexes**: generic metadata filtering on chunks (REQ-063)
4. **Row-Level Security (RLS)**: namespace isolation via PostgreSQL policies (ADR-0009)
5. **Async throughout**: FastAPI async handlers, arq async workers, async database driver
6. **Complex queries**: vector search combined with metadata filtering and namespace scoping in a single query
7. **Alembic migrations**: schema evolution with forward-compatible data model (ARCH-040)

### The "single model" question

SQLModel's primary value proposition is that one class serves as both SQLAlchemy ORM model and Pydantic schema. However, Vektra's architecture already defines API types as Pydantic models (QueryResponse, QueryTrace, DocumentChunk, StepTrace) with shapes that differ from database tables:

- `QueryResponse` aggregates data from multiple tables
- `ApiKey` API schema does not expose `key_hash` (stored in DB)
- `QueryTrace` is a nested structure while DB storage is normalized

The API layer and persistence layer have different shapes by design. Unifying them into a single model would be an architectural compromise, not a simplification.

## Decision

**SQLAlchemy 2.0 async** with the **asyncpg** driver for all database operations.

### Technology choices

| Component | Choice | Version |
|-----------|--------|---------|
| ORM | SQLAlchemy | 2.0.x (currently 2.0.46) |
| Async driver | asyncpg | 0.31.x |
| Migrations | Alembic | latest stable |
| pgvector integration | pgvector-python | latest stable |

### Declarative style

Use SQLAlchemy 2.0 declarative mapping with `Mapped[]` type annotations:

```python
from sqlalchemy.orm import DeclarativeBase, Mapped, mapped_column, relationship
from sqlalchemy.dialects.postgresql import JSONB, UUID
from pgvector.sqlalchemy import Vector

class Base(DeclarativeBase):
    pass

class Chunk(Base):
    __tablename__ = "chunks"

    id: Mapped[uuid.UUID] = mapped_column(UUID, primary_key=True, default=uuid.uuid4)
    document_id: Mapped[uuid.UUID] = mapped_column(ForeignKey("documents.id"))
    content: Mapped[str] = mapped_column(Text)
    embedding: Mapped[list] = mapped_column(Vector(384))
    metadata_: Mapped[dict] = mapped_column("metadata", JSONB, nullable=False, server_default="{}")
    position: Mapped[int] = mapped_column()
    created_at: Mapped[datetime] = mapped_column(server_default=func.now())
    deleted_at: Mapped[Optional[datetime]] = mapped_column(nullable=True)

    document: Mapped["Document"] = relationship(back_populates="chunks")

    __table_args__ = (
        Index("ix_chunks_metadata_gin", "metadata", postgresql_using="gin"),
        Index("ix_chunks_embedding_hnsw", "embedding",
              postgresql_using="hnsw",
              postgresql_with={"m": 16, "ef_construction": 64},
              postgresql_ops={"embedding": "vector_cosine_ops"}),
    )
```

### Session management pattern

```python
from sqlalchemy.ext.asyncio import create_async_engine, async_sessionmaker, AsyncSession

engine = create_async_engine("postgresql+asyncpg://...", pool_size=10)
async_session = async_sessionmaker(engine, expire_on_commit=False)

# FastAPI dependency
async def get_session() -> AsyncGenerator[AsyncSession, None]:
    async with async_session() as session:
        yield session
```

### Conversion between ORM models and API types

Explicit mapping functions at module boundaries (not automatic):

```python
# In vektra-core query module
def to_query_response(trace_row: QueryTraceRow, step_rows: list[StepTraceRow]) -> QueryResponse:
    ...

# In vektra-index search module
def to_search_result(chunk_row: Chunk) -> SearchResult:
    ...
```

This preserves the separation between persistence (ORM models, internal to each module) and API (Pydantic models, defined in vektra_shared).

## Options considered

### SQLAlchemy 2.0 async with asyncpg (chosen)

**Pros**:
- Native support for all PostgreSQL-specific features (pgvector, JSONB/GIN, pgcrypto, RLS)
- `Mapped[]` type annotations provide IDE type safety without plugins
- Complex query API: joins, subqueries, CTEs, window functions
- Alembic is the same project (first-class migration support)
- asyncpg: fastest async PostgreSQL driver under concurrency
- Industry standard since 2006, SQLAlchemy 2.0 stable since 2023
- Natural separation: ORM models are persistence concerns, Pydantic models are API concerns

**Cons**:
- Separate ORM model and Pydantic schema definitions (more code)
- Manual conversion between ORM rows and API types
- Slightly more boilerplate for simple CRUD operations

### SQLModel 0.0.32

**Pros**:
- Single class as both ORM model and Pydantic schema (for simple fields)
- Created by FastAPI author, seamless FastAPI integration
- Simpler code for basic CRUD operations
- Wraps SQLAlchemy 2.0 (can drop down when needed)

**Cons**:
- Pre-1.0 after 4+ years (v0.0.32, roadmap is "tentative")
- Every PostgreSQL-specific feature requires `sa_column` fallback to SQLAlchemy, negating the single-model benefit
- No native JSONB field type (open issue #42 since 2021)
- Performance overhead: Pydantic validation on every DB read (19x slower documented for large result sets)
- Alembic edge cases: UUID server_default failures, GIN index autogenerate issues, Pydantic 2.12+ regressions
- Async documentation missing (listed as roadmap TODO)
- Complex queries (joins, subqueries, CTEs) drop to raw SQLAlchemy API
- "Single model" breaks down for Vektra: API types and DB models have intentionally different shapes
- `metadata` attribute name conflicts with SQLAlchemy reserved attribute

### SQLModel as transition path

Start with SQLModel, migrate to SQLAlchemy if needed.

**Rejected**: Starting with SQLModel and migrating later means rewriting all models and queries. Since Vektra needs PostgreSQL-specific features from day one, the migration would be immediate.

## Consequences

### Positive

- Full access to PostgreSQL features without escape hatches
- Predictable performance (no hidden Pydantic validation overhead on DB reads)
- Alembic works without workarounds
- Stable foundation (SQLAlchemy 2.0 is the de facto Python ORM standard)
- Clear architectural boundary: ORM models (persistence) vs Pydantic models (API/shared types)
- asyncpg provides optimal performance for Vektra's async-throughout architecture

### Negative

- More code: separate ORM model + Pydantic schema + conversion functions per entity
- Developers must understand SQLAlchemy's 2.0 declarative style (learning curve)
- No "one class serves all" shortcut for simple models

### Implementation notes

- ORM models are internal to each module (vektra-core, vektra-ingest, vektra-index). They are NOT part of vektra_shared.
- Pydantic types in vektra_shared remain the public contract between modules.
- Each module owns its Alembic migration branch (modular monolith boundary per ADR-0005).
- RLS policies are managed via raw SQL in dedicated Alembic migrations, not via ORM.
- pgcrypto encryption uses SQLAlchemy `TypeDecorator` pattern for transparent encrypt/decrypt.
