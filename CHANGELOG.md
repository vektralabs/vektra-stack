# Changelog

All notable changes to this project will be documented in this file.

The format is based on [Keep a Changelog](https://keepachangelog.com/en/1.1.0/).

<!--
Convention (Keep a Changelog 1.1.0):
- Add new entries under "[Unreleased]" using sections: Added, Changed,
  Deprecated, Removed, Fixed, Security.
- At release time: rename "[Unreleased]" to "[X.Y.Z] - YYYY-MM-DD" AND
  add a fresh empty "[Unreleased]" block above it. The file must always
  have an "[Unreleased]" section at the top, even if empty.
- Releases are listed newest-first below "[Unreleased]".
- See CONTRIBUTING.md > Changelog for the full process.
-->

## [Unreleased]

<!-- Add entries under: Added, Changed, Deprecated, Removed, Fixed, Security -->

## [0.5.0] - 2026-04-27

Widget production-ready + instructor configuration.

### Added

- **vektra-admin**: `PATCH /api/v1/admin/namespaces/{id}/config` endpoint for instructor configuration. Admin-scoped, whitelisted (v0.5.0 accepts `grounding_mode` and `show_sources`), partial updates, null removes a key. Backs the Moodle block form that lets teachers toggle strict/hybrid RAG and source-citation visibility per course without admin intervention.
- **vektra-learn / widget**: configurable source citation visibility (FEAT-014). Resolution chain: `data-show-sources` attribute (client override) > `namespaces.config.show_sources` (per-course) > `VEKTRA_LEARN_SHOW_SOURCES` env var > default `true`. The learn query response (both JSON and the SSE `sources` event) now carries a `show_sources` flag the widget uses to decide whether to render the citations section. The API always returns the full sources list so analytics and QueryTrace keep complete data.
- **vektra-admin**: `GET /api/v1/admin/namespaces/{id}/config` symmetric to the PATCH. Returns `{namespace_id, config: <stored JSONB>, resolved: <effective after env defaults>}`. The `resolved` block is what the upstream plugin form (e.g. Moodle block edit) needs to render the "Use default" / "Override" toggle without re-implementing the fallback chain.
- **vektra-learn**: `GET /api/v1/learn/conversations/{id}/turns` JWT-scoped endpoint so the widget can restore a conversation after a page reload. Returns decrypted question/answer + created_at; admin-only metadata is not exposed. 403 on namespace mismatch, 404 on missing.
- **vektra-core**: `document_name` field on every source citation, joined from `source_documents.filename` so the widget renders `[1] lecture-07.pdf` instead of chunk UUIDs. Propagates through both `SimpleQueryPipeline` and `AdvancedQueryPipeline`, JSON and SSE paths. Soft-deleted source documents (REQ-057) keep their citation with an `(archived)` suffix so answers stay traceable.
- **vektra-learn**: content-access audit entry (`learn_conversation_turns_read`) written on every successful turns fetch via the shared `vektra_shared.audit` interface (NFR-007).
- **widget**: white-label `data-*` attributes — `data-title`, `data-primary-color`, `data-icon` (emoji or URL), `data-welcome-message`, `data-powered-by`, `data-powered-by-text`, `data-powered-by-url`. All rendered via `textContent` / safe color whitelist to avoid XSS.
- **widget**: tab-scoped conversation persistence via `sessionStorage` keyed by `course_id` (24h cutoff); history replay on load via the new turns endpoint; explicit "New chat" button in the header.
- **errors**: new codes `ERR-ADMIN-005/006/007` (namespace config) and `ERR-LEARN-005/006` (conversation turns).
- **docs**: consolidated [Widget integration guide](docs/guides/widget-integration.md) covering the LMS-agnostic embed flow (server-side JWT issuance, all `data-*` attributes, conversation persistence, token refresh, source citation visibility, error states, and end-to-end examples for PHP and Python backends). Linked from `docs/README.md` under Guides.

### Changed

- **User-facing branding**: README, top-level docs h1s, and external surfaces now use **Vektra RAG** as the product name. Coordinated with [vektralabs/vektra-moodle#14](https://github.com/vektralabs/vektra-moodle/pull/14) which renames its README h1 to "Vektra RAG for Moodle". Repo name (`vektra-stack`), Python package names (`vektra_*`), Docker images, container names, CLI commands, and internal code identifiers are unchanged.
- **widget footer label**: default "Powered by" link text changed from "Vektra" to "VektraLabs" — clearer maintainer-credit pattern (the link points to vektralabs.github.io, the org landing page) and avoids the residual short form. Integrators using the default branding will see the new label after upgrading the bundle; custom `data-powered-by-text` overrides are unaffected.
- **widget styles**: button and accent colors now use `var(--vektra-primary, …)` so `data-primary-color` takes effect without rebuilding the bundle. Hover states use `filter: brightness()` so custom colors still feel interactive.
- **widget primary-color scope (DEBT-018)**: the `--vektra-primary` override is now scoped to widget roots (`.vektra-chat-btn, .vektra-chat-panel`) instead of `:root`, so it no longer clobbers any unrelated `--vektra-primary` declarations on the host page. Repeated widget initialization reuses a single `<style id="vektra-primary-override">` node instead of accumulating duplicates (multi-instance / hot-reload safety).

### Fixed

- **audit log fallback (DEBT-020, NFR-007)**: sensitive admin, learn, and ingest endpoints now always write an audit row, even if the request-id middleware doesn't populate `request.state.request_id`. Each component declares a private `_resolve_request_id` helper that synthesizes a `uuid4()` fallback and emits a structlog warning so middleware misconfiguration is observable. Applied to `POST /api-keys`, `DELETE /api-keys/{id}`, `PATCH /admin/namespaces/{id}/config`, `GET /admin/conversations/{id}/turns`, `GET /api/v1/learn/conversations/{id}/turns`, and the vektra-ingest async + direct audit writers (used by `POST /api/v1/ingest`, batch ingest, deletion). Previous code skipped the audit silently when `request_id` was missing.
- **test fakes**: renamed `self_inner` to `self` in nested `_FakeResult` / `_FakeSession` helpers inside `test_fetch_document_names_marks_archived` (style consistency with PEP 8; no behaviour change).

### Security

- **deps**: bumped `litellm` from `>=1.40,<1.82.7` to `>=1.83.10,<1.83.11` — closes 2 critical CVEs (authentication bypass via OIDC userinfo cache key collision) and several high-severity findings (authenticated command execution via MCP stdio test endpoints, SSTI in `/prompts/test`).
- **deps-dev**: bumped `pytest` from `>=8.0` to `>=9.0.3` (full test suite green on the new major version).
- **deps-dev**: bumped widget `esbuild` from `^0.24` to `^0.25`.

## [0.4.0] - 2026-04-11

QueryTrace observability, eval harness, widget polish, and prompt hardening.

### Added

- **vektra-core**: configurable prompt grounding mode (FEAT-020, DEBT-016). `VEKTRA_PROMPT_GROUNDING_MODE=strict` (default) keeps the LLM tied to retrieved context + history; `hybrid` allows confident fallback to model knowledge. Foundation for the per-namespace override added in v0.5.0.
- **vektra-core**: multilingual reranker upgrade (BUG-016, DEBT-010). Default reranker switched to `BAAI/bge-reranker-v2-m3` with a lower score threshold so non-English queries are no longer over-filtered.
- **vektra-core**: `QueryTrace` persistence and an admin `GET /api/v1/admin/conversations/{id}/turns` endpoint (BUG-013, DEBT-011). Reranker scores, eval-mode metadata, and streaming model name are now captured in traces; gaps in the streaming path closed.
- **vektra-learn**: SSE streaming in the learn query endpoint (FEAT-010). The `done` event now carries `conversation_id` so the widget keeps multi-turn continuity across SSE responses (BUG-018).
- **widget**: Markdown rendering for assistant messages (FEAT-007), with code-block, list, and inline formatting; raw HTML stays sanitized.
- **widget**: token auto-refresh on 401 (FEAT-009) via either an `onTokenExpired` callback or a `data-token-refresh-url` endpoint.
- **widget**: API connectivity check and visible error feedback (FEAT-006) when the backend is unreachable.
- **eval**: retrieval and end-to-end evaluation harness (TECH-002), plus a curated 55-question bilingual EN/IT evaluation dataset.
- **config**: `VEKTRA_LLM_CONTEXT_WINDOW` propagation through `VektraSettings`, `VEKTRA_LLM_API_KEY` wired to litellm, and `VEKTRA_LLM_EXTRA_BODY` support for vLLM thinking-mode flags.

### Fixed

- **vektra-core**: warn (not crash) when `VEKTRA_LLM_CONTEXT_WINDOW` is unset for a model litellm does not know, falling back to 4096 (BUG-017).
- **vektra-core**: conversation row now created before the first turn is persisted (BUG-014).
- **vektra-core**: reranker scores propagated to `SearchResult` so observability and analytics see the post-rerank ranking (BUG-015).
- **vektra-core**: register the conversation store in `ProviderRegistry` so dependent endpoints can resolve it.
- **vektra-core**: distinguish "safeguard blocked" from "no relevant context" in the response so callers can tell apart a refusal from an empty result.
- **vektra-core**: system prompt iteratively hardened to stop the LLM from leaking RAG internals (chunk IDs, scores, role names) in any language.
- **vektra-learn**: audit logging on the conversation turns endpoint (NFR-007) was missing in the initial v0.4.0 release-review build.
- **config**: `grounding_mode` validator added to `VektraSettings` so invalid values fail fast at startup.
- **widget**: streaming token newlines and code-block formatting; centralized input-disabled state across sending and connection events; `<p>` no longer wraps inside `<code>` blocks.
- **build**: removed redundant `uv sync` when `INSTALL_UNSTRUCTURED=true`; eliminated all `uv pip install` invocations outside the lockfile to harden the supply chain.
- **CI**: latency measurement skipped on PR runs (post-merge only); perf-measurement queries reduced from 100 to 20 to keep workflow time bounded.

## [0.3.0] - 2026-03-21

E-learning vertical refinements, widget UX improvements, and Phase 2 stabilization.

### Added

- **vektra-learn**: optional enrollment mode (`VEKTRA_LEARN_REQUIRE_ENROLLMENT`) for LMS integrations where enrollment sync is not yet configured
- **vektra-learn**: collapsible source citations in chatbot widget with accessibility (aria-controls, focus-visible)
- **vektra-learn**: i18n support for source fallback labels (en/it)
- **vektra-core**: `VEKTRA_LLM_API_BASE` config for OpenAI-compatible providers (vLLM, etc.)
- **vektra-ingest**: sparse embedding count validation (fail-fast, consistent with dense path)
- **docker-compose**: parameterized host ports for optional services (Qdrant, TEI)

### Fixed

- **vektra-learn**: auto-generate conversation_id for multi-turn continuity (BUG-010)
- **vektra-learn**: validate course_id claim in JWT before namespace resolution
- **vektra-learn**: show fallback message when no relevant context found
- **vektra-learn**: destructure onNoRelevantContext callback in widget API client
- **vektra-ingest**: generate sparse embeddings for hybrid search (BUG-011)
- **vektra-admin**: guard uninitialized registry in health check (prevents 500 during startup)
- **vektra-core**: pass registry to qdrant_check startup validation step (ARCH-057)
- **widget**: fix snippet truncation and ellipsis detection
- **widget**: improve source citation readability

### Changed

- Default configuration updated to Combo D (RAG tuning report winning config): advanced pipeline, paraphrase-multilingual-MiniLM-L12-v2 embeddings, 2048 response token reserve, 60s fallback timeout
- Streaming responses now emit QueryTrace via SSE (DEBT-002)
- post_retrieval safeguard boundary now called in both execute and stream paths (DEBT-003)

### Security

- API key verification uses TTLCache instead of plaintext LRU cache (DEBT-008)

## [0.2.0] - 2026-03-15

Phase 2: advanced RAG pipeline, multi-tenant isolation, hybrid search, analytics, admin UI, and e-learning vertical.

### Added

- **vektra-shared**: SparseEmbeddingProvider and SparseVector protocols, WebhookEventEmitter, Phase 2 config fields (rewrite, rerank, webhook, learn)
- **vektra-core**: AdvancedQueryPipeline with query rewriting, cross-encoder reranking, and hybrid search routing; Presidio-based PII safeguard with content modification; persistent conversations with pgcrypto encryption; feedback and citation-feedback APIs; streaming QueryTrace
- **vektra-ingest**: OCR support via Unstructured extractor; dual-strategy chunking (text + table preservation); batch ingestion and deletion; Markdown extraction; document version tracking; phase-aware job lifecycle
- **vektra-index**: Qdrant vector store provider; BM25 sparse embeddings via fastembed; hybrid search (dense + sparse); reindex API with atomic version switching
- **vektra-admin**: PostgreSQL RLS enforcement for namespace isolation; scope checking (admin/ingest/query/monitor); rate-limiting middleware; TTL cache for key verification; HTMX + Jinja2 admin dashboard (ADR-0024) with health, keys, namespaces, and audit views
- **vektra-analytics**: QueryTrace storage and retrieval; per-namespace metrics aggregation; retention cleanup
- **vektra-learn**: LMS-agnostic API for enrollment, token generation, and course-scoped queries; JWT-based course isolation; chatbot widget as backend-served JS bundle (ADR-0025)
- **Infrastructure**: TEI and Qdrant Docker Compose profiles; reindex and batch-ingest operator scripts; 11-step startup validation sequence (ARCH-057)
- **Database**: 4 new migrations (Phase 2 tables, RLS policies, hybrid search columns, learn tables)
- **ADRs**: ADR-0022 (SQLAlchemy async), ADR-0023 (query rewriting), ADR-0024 (admin UI), ADR-0025 (learn widget)

### Fixed

- Safeguard pre_query/post_retrieval/pre_response boundaries fully wired
- Conversation persistence made best-effort (non-blocking)
- Qdrant collection creation race condition handled
- Namespace binding enforced on stats and reindex endpoints
- Audit pagination cursor with microsecond precision
- Non-object JSON guard in RLS namespace resolution

### Changed

- Embedding model default: all-MiniLM-L6-v2 to paraphrase-multilingual-MiniLM-L12-v2
- Query pipeline default: simple to advanced
- pydantic capped to <3 across all components

## [0.1.0] - 2026-02-27

Phase 1 MVP: from `git clone` to first RAG query in under 30 minutes.

### Added

- **vektra-shared**: 9 Protocol interfaces, configuration (40 env vars), API key auth with argon2id, ProviderRegistry, startup validation
- **vektra-core**: RAG query pipeline (embed, search, filter, prompt, LLM), multi-turn conversations, streaming SSE, LLM graceful degradation (primary + fallback), token budget allocation
- **vektra-ingest**: PDF extraction via pdfplumber, fixed-size chunking, async jobs for large files (>10 MB), duplicate detection (SHA-256), content type validation via python-magic
- **vektra-index**: pgvector-backed semantic search, embedding generation (all-MiniLM-L6-v2), namespace isolation, relevance threshold filtering, overlap deduplication
- **vektra-admin**: API key CRUD, hierarchical health endpoints (/health, /health/{component}, /health/memory), audit logging, Prometheus metrics on /metrics
- **Docker Compose stack**: single-command deployment (vektra + postgres), optional Ollama profile for local LLM
- **CI pipeline**: GitHub Actions (lint, unit tests x5 components, integration tests, NFR hard gates, shell script validation, auto-labeler)
- **Makefile**: 10 operator targets (test, lint, health, demo, etc.)
- **Operator scripts**: health.sh, create-key.sh, ingest.sh, query.sh, demo.sh
- **Documentation**: Diataxis structure (getting started, API reference, configuration, error codes, architecture overview, contributor guide, n8n workflow)
- **13 error codes**: structured error envelope (ERR-AUTH, ERR-INGEST, ERR-QUERY, ERR-CONFIG) with categories and remediation
- **23 ADRs**: architectural decisions from modular monolith strategy to query pipeline design
