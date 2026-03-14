# Changelog

All notable changes to this project will be documented in this file.

The format is based on [Keep a Changelog](https://keepachangelog.com/en/1.1.0/).

## [0.2.0] - 2026-03-14

Phase 2: advanced RAG features, e-learning vertical, and production hardening.

### Added

- **vektra-core**: AdvancedQueryPipeline with conversational query rewriting (ARCH-061), cross-encoder reranking via `rerankers` library, hybrid search orchestration. Persistent encrypted conversations (pgcrypto). Response feedback collection (response_id, citation_id). Presidio PII safeguard with content modification. Composable Jinja2 prompt templates (system, context, conversation, rewrite)
- **vektra-ingest**: OCR support via UnstructuredExtractor (Tesseract, optional INSTALL_UNSTRUCTURED build arg). DualStrategyChunking (text split with overlap, tables never split, parent-child hierarchy). Document versioning (re-ingest creates new version, soft-deletes old). Batch delete API. Markdown extractor. Granular pipeline APIs (extract, chunk, embed endpoints)
- **vektra-index**: QdrantVectorStoreProvider with native dense/sparse/hybrid search. FastEmbedBM25Provider for sparse embeddings. Hybrid search via Reciprocal Rank Fusion (RRF). Zero-downtime reindex API with index version management
- **vektra-analytics** (new component): QueryTrace dedicated storage, metrics aggregation (latency, retrieval scores, model distribution, throughput), reporting API with namespace filtering
- **vektra-learn** (new component): LMS-agnostic e-learning vertical with enrollment management, course-scoped content ingestion, JWT dashboard token generation, course-scoped RAG query endpoint. Backend-served chatbot widget (esbuild IIFE bundle) with streaming, source citations, light/dark themes, i18n (en/it)
- **vektra-admin**: HTMX + Jinja2 admin dashboard (health, keys, namespaces, audit, config pages). Per-key rate limiting (rate_limit_rpm). Granular scope enforcement (admin/ingest/query via require_scope middleware). Namespace quota management
- **Database**: 4 new migrations (Phase 2 tables, RLS policies, hybrid search indexes, learn tables). PostgreSQL RLS policies for namespace isolation. TOCTOU fix on API key creation
- **Infrastructure**: Docker multi-stage build with widget-builder stage. Qdrant Docker Compose profile (--profile qdrant). TEI embedding server profile (--profile tei). INSTALL_UNSTRUCTURED build arg for optional OCR. Torch CPU-only optimization
- **Protocols**: SparseEmbeddingProvider, extended VectorStoreProvider (SearchMode, full-store contract), ChunkingStrategy, extended SafeguardHook (content modification), LogEventEmitter
- **Configuration**: 11 new env vars (rewrite, rerank, webhook, ingest extensions, learn JWT). 48 total VEKTRA_* variables
- **ADRs**: ADR-0022 (SQLAlchemy async), ADR-0023 (query rewriting), ADR-0024 (admin UI server-side), ADR-0025 (chatbot widget)

### Fixed

- Qdrant point IDs: use uuid5(doc_id, chunk_index) for deterministic UUID generation
- Qdrant collection auto-creation at startup via ensure_collection()
- no_relevant_context detection: trigger on zero filtered results (not only when search returns results)
- Retrieval filter deduplication: preserve score-descending order
- Learn enrollment IntegrityError: detect FK violation via sqlstate/pgcode (not string matching)
- Learn content/ingest: SSRF mitigation with per-hop DNS re-validation on redirects
- Ingest namespace: auto-create namespace record on first ingest (pg upsert)
- Ingest status: return "new" (not "indexed") for successfully ingested documents

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
