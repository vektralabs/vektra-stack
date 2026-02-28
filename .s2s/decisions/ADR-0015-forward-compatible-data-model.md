# ADR-0015: Forward-compatible data model with Phase 2 fields from Phase 1

**Status**: accepted
**Date**: 2026-02-06
**Context**: Architectural review 2026-02-06

## Context

Vektra Phase 1 is an MVP. Phase 2 adds features that touch the data model: feedback loops, document versioning, soft delete, metadata filtering, zero-downtime reindex, PDF highlighting. If these features require schema migrations on a production system with data, the cost is:

- Alembic migrations that must handle existing data
- Client API changes (new fields in responses)
- Loss of historical correlation (no response_id on old responses, no version on old documents)
- Potential downtime during migration

The alternative: add nullable/defaulted fields in Phase 1 that cost almost nothing at design time but prevent all of the above.

**Guiding principle**: "Fields, types, and no-op interfaces today avoid migrations and refactoring tomorrow."

## Decision

Phase 1 database schema and Pydantic types include fields for Phase 2 features. All added fields are either nullable or have sensible defaults. Phase 1 behavior is unchanged; the fields exist but are not actively used.

### QueryResponse

| Field | Type | Phase 1 value | Phase 2 use |
|-------|------|---------------|-------------|
| `response_id` | UUID | Generated, returned to client | Feedback endpoint, analytics correlation |
| `confidence_tier` | str or None | None | HIGH/MEDIUM/LOW from confidence scoring |
| `context_only` | bool | False | True when LLM unavailable (ARCH-043) |

### SourceRef (citation)

| Field | Type | Phase 1 value | Phase 2 use |
|-------|------|---------------|-------------|
| `citation_id` | UUID | Generated, returned to client | Granular citation feedback |
| `document_version` | int | 1 | Correlate citation with document version |

### SourceDocument

| Field | Type | Phase 1 value | Phase 2 use |
|-------|------|---------------|-------------|
| `version` | int (NOT NULL, default 1) | 1 | Document versioning on re-ingestion |
| `supersedes_id` | UUID or None | None | FK to previous version |
| `deleted_at` | datetime or None | None | Soft delete timestamp |
| `deletion_reason` | str or None | None | "user_request", "superseded", "expired" |

### DocumentChunk

| Field | Type | Phase 1 value | Phase 2 use |
|-------|------|---------------|-------------|
| `element_type` | ElementType enum | TEXT | Unstructured classification (TABLE, TITLE, LIST) |
| `parent_id` | str or None | None | Parent-child chunk hierarchy |
| `coordinates` | BoundingBox or None | None | PDF highlighting (page, x0, y0, x1, y1) |
| `index_version` | int (NOT NULL, default 1) | 1 | Zero-downtime reindex (ARCH-045) |
| `metadata` | JSONB | {page, position, source_file} | Filterable fields (course_id, content_type, language) |

### Namespace

| Field | Type | Phase 1 value | Phase 2 use |
|-------|------|---------------|-------------|
| Full table | See ARCH-047 | "default" record, all nullable fields | Multi-tenant with quota, config, retention |

### API keys

| Field | Type | Phase 1 value | Phase 2 use |
|-------|------|---------------|-------------|
| `rate_limit_rpm` | int or None | None (unlimited) | Per-key rate limiting |

### Ingest jobs

| Field | Type | Phase 1 value | Phase 2 use |
|-------|------|---------------|-------------|
| `idempotency_key` | str or None (UNIQUE) | None unless client provides header | Safe retry for n8n workflows |

## Options Considered

### Minimal Phase 1 schema (add fields in Phase 2)

**Pros**:
- Fewer columns in Phase 1
- Simpler initial schema

**Cons**:
- Every Phase 2 feature requires Alembic migration on production data
- Historical data lacks correlation fields (response_id, citation_id, document version)
- Client SDK/integration updates needed for new response fields
- Potential downtime during schema migration

### Forward-compatible schema from Phase 1 (chosen)

**Pros**:
- Zero schema migrations for Phase 2 features that use these fields
- Historical data is traceable from day one
- Client integrations receive stable response shapes
- Cost: a few nullable columns and one extra table

**Cons**:
- Slightly larger schema in Phase 1
- Fields that are "unused" may confuse contributors (mitigated by documentation)

## Consequences

### Positive

- Phase 2 features activate by changing implementation, not schema
- Every response from day one has response_id and citation_id for future feedback
- Document versioning history preserved from first ingestion
- Soft delete available immediately for compliance
- Zero-downtime reindex possible from first deployment
- Metadata filtering index (GIN) ready for vektra-learn

### Negative

- Schema documentation must explain which fields are "Phase 2 placeholders"
- Nullable fields add minor complexity to queries (IS NULL checks)
- Contributors may question unused fields without context
