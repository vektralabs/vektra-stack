# Implementation Plan: vektra-learn - LMS-agnostic e-learning API and chatbot widget

**ID**: 20260301-component-learn
**Status**: pending
**Branch**: N/A
**Created**: 2026-03-01T14:30:09Z
**Updated**: 2026-03-01T14:30:09Z

## Traceability

**Source**: component-learn
**Source Type**: architecture

## Provides / Requires

**Provides**:
- LMS-agnostic enrollment, content ingestion, and dashboard token APIs (consumers: infra-phase2)
- Course-scoped query pipeline integration (consumers: infra-phase2)
- Chatbot widget JS bundle at /static/vektra-chat.js (consumers: infra-phase2)

**Requires**:
- 20260301-core-pipeline-v2.md: AdvancedQueryPipeline with conversation context and hybrid search
- 20260301-component-analytics.md: QueryTrace storage for course-level analytics access

## References

### Requirements
- REQ-050: Pluggable vector store backend (namespace-based course isolation) @.s2s/requirements.md
- REQ-048: Namespace support for document isolation @.s2s/requirements.md

### Architecture
- ARCH-044: Chunk metadata filtering (course_id as domain-specific field) @.s2s/architecture.md
- ARCH-063: Learn chatbot widget @.s2s/architecture.md
- ARCH-047: Namespace as first-class entity @.s2s/architecture.md

### Decisions
- ADR-0001: Hybrid monorepo strategy @.s2s/decisions/ADR-0001-hybrid-monorepo-strategy.md
- ADR-0005: Module boundary enforcement @.s2s/decisions/ADR-0005-module-boundary-enforcement.md
- ADR-0025: Learn chatbot widget as backend-served JS bundle @.s2s/decisions/ADR-0025-learn-chatbot-widget.md

### Dependencies
- 20260301-core-pipeline-v2.md
- 20260301-component-analytics.md

## Overview

vektra-learn is the e-learning vertical backend. It is LMS-agnostic: it provides REST APIs for enrollment registration (binding students to courses), content ingestion triggers (with course metadata), dashboard token generation, and course-scoped RAG queries. Any LMS adapter (such as vektra-moodle, a separate PHP repository) calls these APIs to integrate Vektra into a learning management system.

The component also includes the chatbot widget, the first JavaScript artifact in the project. The widget is a self-contained JS bundle served at `/static/vektra-chat.js` (ADR-0025, ARCH-063). It renders a chat interface (floating button + expandable panel), communicates with the backend exclusively via REST API, and accepts configuration via `data-*` attributes on the script tag. The widget has no server-side coupling: no cookies, no injected globals, no Jinja2 dependencies. This is a hard constraint to preserve the Phase 3 migration path to an npm package.

The build tooling for the widget uses esbuild invoked via npx (no global Node.js dependency beyond what npx provides). The widget source is vanilla JavaScript (no React, no Preact) targeting ES2020. The output is a single minified JS file.

## Design Notes

### Package structure

```
vektra-learn/
  pyproject.toml
  README.md
  src/vektra_learn/
    __init__.py
    models.py         # SQLAlchemy ORM: EnrollmentOrm, DashboardTokenOrm
    service.py         # LearnService: enrollment, content trigger, token generation
    api.py             # FastAPI router: /api/v1/learn/*
    query.py           # Course-scoped query wrapper
  widget/
    src/
      index.js         # Widget entry point
      chat-ui.js       # Chat panel rendering (vanilla DOM)
      api-client.js    # REST API client (fetch-based)
      styles.js        # Inline CSS (injected as <style> tag)
    esbuild.config.mjs # esbuild build configuration
    package.json       # Minimal: esbuild dev dependency only
  static/
    vektra-chat.js     # Built output (committed or built in Docker)
  tests/
    __init__.py
    test_service.py
    test_api.py
    test_query.py
```

### pyproject.toml

```toml
[project]
name = "vektra-learn"
version = "0.2.0-dev"
description = "E-learning vertical: LMS-agnostic API and chatbot widget"
readme = "README.md"
requires-python = ">=3.12"
dependencies = [
    "vektra-shared",
    "fastapi>=0.115",
    "sqlalchemy>=2.0",
    "structlog>=24.0",
    "pyjwt>=2.8",
]

[build-system]
requires = ["hatchling"]
build-backend = "hatchling.build"

[tool.hatch.build.targets.wheel]
packages = ["src/vektra_learn"]

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

### Database tables

Two new tables (created by database-phase2 migration or a migration in this plan if needed):

```sql
-- Enrollment: student-course binding
CREATE TABLE enrollments (
    id          UUID PRIMARY KEY DEFAULT gen_random_uuid(),
    student_id  VARCHAR(255) NOT NULL,
    course_id   VARCHAR(255) NOT NULL,
    namespace   VARCHAR(255) NOT NULL REFERENCES namespaces(id),
    enrolled_at TIMESTAMP WITH TIME ZONE DEFAULT now(),
    metadata    JSONB DEFAULT '{}',
    UNIQUE (student_id, course_id)
);

-- Dashboard token: short-lived JWT for widget auth
CREATE TABLE dashboard_tokens (
    id          UUID PRIMARY KEY DEFAULT gen_random_uuid(),
    student_id  VARCHAR(255) NOT NULL,
    course_id   VARCHAR(255) NOT NULL,
    token_hash  VARCHAR(128) NOT NULL,
    expires_at  TIMESTAMP WITH TIME ZONE NOT NULL,
    created_at  TIMESTAMP WITH TIME ZONE DEFAULT now()
);
CREATE INDEX idx_dashboard_tokens_hash ON dashboard_tokens(token_hash);
```

### API endpoints

| Method | Path | Auth | Description |
|--------|------|------|-------------|
| POST | /api/v1/learn/enrollments | admin or ingest scope | Register a student enrollment |
| GET | /api/v1/learn/enrollments | admin scope | List enrollments (filter by course_id, student_id) |
| DELETE | /api/v1/learn/enrollments/{id} | admin scope | Remove an enrollment |
| POST | /api/v1/learn/content/ingest | admin or ingest scope | Trigger content ingestion with course metadata |
| POST | /api/v1/learn/tokens | admin scope | Generate a dashboard token (JWT) |
| POST | /api/v1/learn/query | query scope (via token) | Course-scoped RAG query |
| GET | /static/vektra-chat.js | public (no auth) | Chatbot widget JS bundle |

### Enrollment API

```python
class EnrollmentRequest(BaseModel):
    student_id: str
    course_id: str
    namespace: str  # maps course to a Vektra namespace
    metadata: dict[str, Any] = {}

class EnrollmentResponse(BaseModel):
    id: UUID
    student_id: str
    course_id: str
    namespace: str
    enrolled_at: datetime
```

### Content ingestion trigger

Wraps the existing `POST /api/v1/ingest` endpoint. Adds course metadata (`course_id`, `module_id`) to the chunk metadata so that course-scoped queries filter correctly via ARCH-044 JSONB filtering.

```python
class ContentIngestRequest(BaseModel):
    course_id: str
    namespace: str
    document_url: str | None = None  # URL to fetch
    metadata: dict[str, Any] = {}    # merged into chunk metadata
```

The service calls the ingest pipeline internally (Python call, not HTTP) with the namespace set to the course namespace and metadata including `course_id`.

### Dashboard token generation

Generates a short-lived JWT containing `student_id`, `course_id`, and `namespace`. The widget uses this token in the Authorization header when calling `/api/v1/learn/query`.

```python
class TokenRequest(BaseModel):
    student_id: str
    course_id: str
    expires_in: int = 3600  # seconds, default 1 hour

class TokenResponse(BaseModel):
    token: str
    expires_at: datetime
```

JWT signing uses a configurable secret (`VEKTRA_LEARN_JWT_SECRET`). The token is not an API key; it is a scoped, short-lived credential for the widget.

### Course-scoped query

`POST /api/v1/learn/query` accepts a JWT token (from dashboard token generation), validates it, extracts `course_id` and `namespace`, and delegates to the query pipeline with:
- `namespace` set to the course namespace
- `filters` including `course_id` in metadata filter
- `conversation_id` for multi-turn context within the course

This is a thin wrapper that adds course scoping to the existing query pipeline. No new pipeline logic.

### Chatbot widget

The widget is vanilla JavaScript (no framework). It creates a floating chat button and an expandable chat panel using DOM APIs. CSS is injected as a `<style>` tag from a JavaScript string (no external CSS file).

#### Configuration via data-* attributes

```html
<script
  src="https://vektra.example.com/static/vektra-chat.js"
  data-api-url="https://vektra.example.com"
  data-course-id="CS101"
  data-token="eyJ..."
  data-theme="light"
  data-language="en"
></script>
```

| Attribute | Required | Default | Description |
|-----------|----------|---------|-------------|
| data-api-url | yes | - | Base URL of the Vektra API |
| data-course-id | yes | - | Course identifier for scoped queries |
| data-token | yes | - | JWT token from dashboard token generation |
| data-theme | no | "light" | "light" or "dark" |
| data-language | no | "en" | UI language (en, it) |

#### Build tooling

```json
// vektra-learn/widget/package.json
{
  "private": true,
  "scripts": {
    "build": "node esbuild.config.mjs"
  },
  "devDependencies": {
    "esbuild": "^0.24"
  }
}
```

```javascript
// vektra-learn/widget/esbuild.config.mjs
import * as esbuild from "esbuild";

await esbuild.build({
  entryPoints: ["src/index.js"],
  bundle: true,
  minify: true,
  target: "es2020",
  outfile: "../static/vektra-chat.js",
  format: "iife",
});
```

Build command: `cd vektra-learn/widget && npm install && npm run build`

The built `static/vektra-chat.js` is NOT committed to the repository (avoid bloat and merge conflicts). It is generated by the Docker build (Node.js builder stage) and by CI. Developers building locally run `cd widget && npm install && npm run build`. The `static/` directory is gitignored. The Python package at runtime serves the file from the mounted path; if missing, the widget endpoint returns 404 with a clear error message.

#### Widget behavior

1. On load: reads `data-*` attributes from the `<script>` tag
2. Creates a floating button (bottom-right corner, configurable position)
3. On click: expands a chat panel (400x500px default, responsive)
4. User types a message, presses Enter or clicks Send
5. Widget calls `POST {data-api-url}/api/v1/learn/query` with the message, course_id, and token
6. Streams the response (SSE) and renders tokens as they arrive
7. Displays source citations with document names and relevance scores
8. Maintains conversation_id across turns for multi-turn context

### Import-linter boundary

vektra_learn can import from vektra_shared only. Add to root `pyproject.toml`:

```toml
[[tool.importlinter.contracts]]
name = "vektra_learn must not import from other vektra components"
type = "forbidden"
source_modules = ["vektra_learn"]
forbidden_modules = ["vektra_core", "vektra_ingest", "vektra_index", "vektra_admin", "vektra_analytics", "vektra_app"]
```

Communication with vektra-core (query pipeline) and vektra-ingest (content trigger) happens through internal Python calls wired in the app lifespan (infra-phase2), or via HTTP if the modular monolith is split. The import boundary enforces this separation.

## Tasks

### Component scaffold (3 tasks)

- [ ] Create `vektra-learn/` directory with `pyproject.toml`, `README.md`, `src/vektra_learn/__init__.py`
- [ ] Add `vektra-learn` to root `pyproject.toml`: workspace members, import-linter contracts, ruff isort known-first-party, coverage source. Note: shared-protocols-phase2 (Wave 0) may have already added import-linter contracts, isort, and coverage entries - verify and skip if present.
- [ ] Create Alembic migration `0004_learn_tables.py` with `revision = "0004"`, `down_revision = "0003"` for `enrollments` and `dashboard_tokens` tables. database-phase2 does NOT include these tables; this plan owns them.

### Backend API (6 tasks)

- [ ] Implement `vektra_learn/models.py` with EnrollmentOrm and DashboardTokenOrm (SQLAlchemy ORM)
- [ ] Implement `vektra_learn/service.py` with LearnService: create_enrollment, list_enrollments, delete_enrollment, trigger_ingest, generate_token, validate_token
- [ ] Implement `vektra_learn/query.py` with course-scoped query wrapper: validates JWT, extracts course context, delegates to query pipeline with namespace and metadata filters
- [ ] Implement `vektra_learn/api.py` with FastAPI router for all /api/v1/learn/* endpoints
- [ ] Add auth dependencies: admin/ingest scope for management endpoints, JWT validation for query endpoint
- [ ] Configure `VEKTRA_LEARN_JWT_SECRET` in VektraSettings (vektra_shared/config.py) as optional, only required when vektra-learn is active

### Chatbot widget (5 tasks)

- [ ] Create `vektra-learn/widget/` directory with `package.json` and `esbuild.config.mjs`
- [ ] Implement `widget/src/index.js`: script tag attribute parsing, widget initialization, DOM injection
- [ ] Implement `widget/src/chat-ui.js`: floating button, expandable panel, message list, input field, send button, SSE streaming display, source citations
- [ ] Implement `widget/src/api-client.js`: fetch-based REST client for POST /api/v1/learn/query with SSE parsing, conversation_id tracking
- [ ] Implement `widget/src/styles.js`: CSS-in-JS string for chat panel styling, light/dark theme support, responsive layout

### Build and tests (4 tasks)

- [ ] Build the widget: `cd widget && npm install && npm run build`, verify `static/vektra-chat.js` output is a single minified file
- [ ] Write unit tests for LearnService: enrollment CRUD, token generation/validation, content trigger delegation
- [ ] Write unit tests for API router: auth enforcement, request/response format, JWT validation
- [ ] Write unit tests for course-scoped query wrapper: namespace injection, metadata filter application

## Acceptance Criteria

- [ ] `vektra-learn` is a workspace member in root pyproject.toml
- [ ] Import-linter enforces that vektra_learn imports only from vektra_shared
- [ ] POST /api/v1/learn/enrollments creates an enrollment and returns 201
- [ ] POST /api/v1/learn/content/ingest triggers ingestion with course_id in chunk metadata
- [ ] POST /api/v1/learn/tokens returns a signed JWT with student_id, course_id, namespace, and expiry
- [ ] POST /api/v1/learn/query validates the JWT, scopes the query to the course namespace, and returns a RAG response
- [ ] /static/vektra-chat.js is a single minified JS file under 50KB
- [ ] Widget renders a chat interface when loaded via a script tag with valid data-* attributes
- [ ] Widget communicates exclusively via REST API (no server-side coupling)
- [ ] Widget data-* attribute API matches the contract defined in ADR-0025 (data-api-url, data-course-id, data-token, data-theme)
- [ ] Widget supports SSE streaming for token-by-token response display
- [ ] Unit tests pass with 80%+ coverage on vektra_learn (Python code)

## Testing Approach

Python backend: unit tests with mocked SQLAlchemy sessions and mocked query pipeline cover enrollment CRUD, token generation/validation, content trigger delegation, and course-scoped query wrapping. API tests use FastAPI TestClient with httpx to verify endpoint routing, auth enforcement, and response format.

Chatbot widget: manual testing via a minimal HTML page that loads the built JS bundle with test data-* attributes. Automated widget testing is deferred to Phase 3 (when the widget moves to its own repo with a proper JS test harness). The build step itself is verified by checking that `static/vektra-chat.js` exists and is non-empty after `npm run build`.

## Integration Notes

vektra-learn is wired into the application in infra-phase2. The app lifespan creates a LearnService instance and registers it in ProviderRegistry. The learn router is included in the FastAPI app via `app.include_router(learn_router)`. The static file serving for `/static/vektra-chat.js` is configured via FastAPI's StaticFiles mount.

The content ingestion trigger calls vektra-ingest internally (Python call via ProviderRegistry, not HTTP). The course-scoped query calls the query pipeline internally. Both integrations are wired in the lifespan, not hard-coded in vektra-learn.

vektra-moodle (separate PHP repo, Phase 2) integrates by: generating a JWT for the authenticated student, injecting the script tag with data-* attributes, and letting the widget handle all client-side interaction. This integration pattern is identical whether the widget is served from the backend (Phase 2) or from npm CDN (Phase 3).

The widget build step must be added to the Dockerfile in infra-phase2. This requires Node.js in the builder stage. The runtime stage does not need Node.js.

## Notes

<!-- Progress notes during implementation -->
