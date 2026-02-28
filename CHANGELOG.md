# Changelog

All notable changes to this project will be documented in this file.

The format is based on [Keep a Changelog](https://keepachangelog.com/en/1.1.0/).

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
