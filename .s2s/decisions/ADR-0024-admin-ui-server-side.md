# ADR-0024: Admin UI with server-side rendering (HTMX + Jinja2)

**Status**: accepted
**Date**: 2026-03-01
**Context**: OQ-018 resolution (admin-ui architecture)

## Context

Phase 1 provides a minimal `GET /admin` health dashboard (REQ-006) rendered as server-side HTML. Phase 2 expands admin capabilities to include namespace management, full API key management, audit log viewer, and configuration. The question is how to build the Phase 2 admin UI.

Vektra is infrastructure, not a consumer application. The primary user persona is the Platform Operator (DevOps/platform teams). The admin UI is an internal operations tool, not a product interface.

Three options were evaluated.

## Decision

Use **server-side rendering with HTMX + Jinja2** for the Phase 2 admin UI. The admin pages are Jinja2 templates served by vektra-admin endpoints, with HTMX for partial page updates (table pagination, form submissions, status polling) without full page reloads.

### Design constraints for Phase 3 migration

Phase 3 may introduce a separate SPA frontend (React/Vue) in its own repository. To ensure a clean migration path:

1. **No business logic in Jinja2 routes.** Every admin page must call existing REST API endpoints (`/api/v1/api-keys`, `/api/v1/namespaces`, etc.) and render the response. If a page needs aggregated data, create the REST endpoint first, then render from it.

2. **Admin routes are a presentation layer only.** The routes under `GET /admin/*` do: authenticate, call REST API, render template. No database queries, no service calls, no business rules.

3. **REST API completeness.** Every operation available in the admin UI must also be available via the REST API. The UI is a convenience layer, not a gate.

When Phase 3 introduces a SPA:
- The REST API already exists and does not change
- The Jinja2 routes are removed from vektra-admin
- Zero backend refactoring required

### Technology choices

| Component | Choice | Rationale |
|-----------|--------|-----------|
| Templating | Jinja2 | Already in the project (prompt templates use Jinja2) |
| Interactivity | HTMX ~14KB | Partial updates, form submissions, polling without JS framework |
| CSS | Pico CSS or classless CSS | Minimal, no build pipeline, responsive by default |
| Charts (if needed) | Chart.js via CDN | Lightweight, no npm required |

### Scope for Phase 2

| Page | REST API source | Features |
|------|----------------|----------|
| Health dashboard | `GET /health?detail=full` | Component status, latency, memory (already exists in Phase 1) |
| API keys | `GET/POST/DELETE /api/v1/api-keys` | List, create, revoke, scope display |
| Namespaces | `GET/POST/DELETE /api/v1/namespaces` | List, create, delete, document count |
| Audit log | `GET /api/v1/audit-log` | Filterable table, pagination, date range |
| System config | `GET /health/memory`, env var display | Read-only configuration viewer |

## Options considered

### Option 1: Server-side rendering (HTMX + Jinja2) - chosen

- Pro: zero JS build pipeline, unified deployment, consistent with "configuration over fork", HTMX covers all CRUD use cases, minimal footprint (~14KB HTMX + templates)
- Pro: Phase 1 precedent exists (`GET /admin`)
- Con: less fluid UX for complex interactions (drag-and-drop, real-time charts)
- Con: fewer frontend developers familiar with HTMX vs React

### Option 2: Separate SPA (React or Vue)

- Pro: rich UX, mature component ecosystem, independent frontend development
- Con: introduces JS/TS stack in a 100% Python project, separate build pipeline, separate CI
- Con: overkill for an internal operations tool used by platform teams
- Con: doubles the skill set required for contributors

### Option 3: API-only (no UI)

- Pro: zero UI development effort
- Con: audit log browsing via curl is impractical
- Con: Phase 1 already has `GET /admin`, removing it is a regression
- Con: higher barrier for less technical operators

## Consequences

- Admin pages are served from the same container, no additional deployment
- No node/npm dependency introduced in the project
- HTMX added as a static asset (~14KB), no CDN dependency
- Phase 3 migration to SPA requires only removing Jinja2 routes (backend unchanged)
- If vektra-analytics needs interactive dashboards beyond HTMX capability, Chart.js can be included via CDN in specific templates

## Traceability

- Resolves: OQ-018 (admin-ui architecture)
- References: REQ-006 (admin capabilities), ARCH-059 (API contract), ARCH-062
- Phase 3 migration: remove Jinja2 routes, REST API unchanged
