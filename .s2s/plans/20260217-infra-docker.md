# Implementation Plan: Docker Compose stack, Dockerfile, and deployment configs

**ID**: 20260217-infra-docker
**Status**: active
**Branch**: N/A
**Created**: 2026-02-17T22:42:39Z
**Updated**: 2026-02-17T22:42:39Z

## Traceability

**Source**: architecture
**Source Type**: architecture

## References

### Requirements
- REQ-005: WF-INT-001: End-to-End MVP Validation workflow @.s2s/requirements.md
- REQ-007: Phase 1 interaction mode: API + shell scripts @.s2s/requirements.md
- REQ-027: REQ-005 checkpoint decomposition @.s2s/requirements.md
- NFR-004: Container startup time (<60s) @.s2s/requirements.md
- NFR-005: Data durability on graceful restart @.s2s/requirements.md
- NFR-006: Resource ceiling (4GB RAM, 2 CPU) @.s2s/requirements.md
- NFR-012: TLS encryption in transit @.s2s/requirements.md
- NFR-013: Database encryption at rest @.s2s/requirements.md

### Architecture
- ARCH-001: Single container deployment @.s2s/architecture.md
- ARCH-002: PostgreSQL + pgvector @.s2s/architecture.md
- ARCH-026: Memory limits @.s2s/architecture.md
- ARCH-033: Docker resource constraints @.s2s/architecture.md

### Decisions
- ADR-0004: Minimal Docker Compose stack @.s2s/decisions/ADR-0004-minimal-docker-compose-stack.md
- ADR-0012: Docker Compose specification @.s2s/decisions/ADR-0012-docker-compose-spec.md

### Dependencies
- 20260217-infra-app-entrypoint

## Overview

Author the container build and deployment configurations. Includes a multi-stage Dockerfile, the docker-compose.yml with service definitions and optional profiles, a .env.example, and example TLS reverse proxy configurations. The goal is a `docker-compose up -d` experience that reaches a healthy state within 60 seconds without any manual configuration.

## Design Notes

- Multi-stage Dockerfile: `builder` stage installs dependencies with uv, `runtime` stage copies only the installed packages and source. Non-root user (uid 1000) in runtime stage. HEALTHCHECK instruction calls GET /health.
- docker-compose.yml services: `postgres` (postgres:16-alpine with pgvector), `vektra` (built from Dockerfile), `ollama` (profile: local-llm), `qdrant` (profile: qdrant, Phase 2 placeholder).
- Memory limits per ARCH-026/ARCH-033: postgres 512MB, vektra 2GB (leaves headroom for sentence-transformers model), ollama 4GB (separate machine or GPU recommended).
- TLS termination at reverse proxy. Application layer: when VEKTRA_ENV=production, reject plain HTTP connections with 400. Example configs in `deploy/nginx/` and `deploy/traefik/`.
- docker-compose.override.yml for local development: volume mounts for hot reload, DEBUG log level, no TLS enforcement.

## Tasks

- [ ] Write `Dockerfile`: builder stage (python:3.11-slim base, install uv, copy pyproject.toml files, `uv sync --frozen`), runtime stage (python:3.11-slim, copy venv and source, add non-root user, set HEALTHCHECK, set CMD)
- [ ] Write `docker-compose.yml`: `postgres` service (image postgres:16-alpine, pgvector extension init script, health check, persistent volume, env vars for DB name/user/password), `vektra` service (build: ., depends_on postgres with condition service_healthy, memory limit 2GB, env_file .env, ports 8000:8000, health check), `ollama` service (profile: local-llm, ghcr.io/ollama/ollama, memory limit 4GB), `qdrant` service (profile: qdrant, qdrant/qdrant, memory limit 1GB)
- [ ] Write `docker-compose.override.yml`: volume mount for live code reload, VEKTRA_LOG_LEVEL=DEBUG, remove TLS production check
- [ ] Write `.env.example`: with VEKTRA_ADMIN_BOOTSTRAP_KEY and VEKTRA_LLM_API_KEY as required vars, commented alternatives for LLM provider (OpenAI, Anthropic, Ollama), all optional vars documented with defaults matching ARCH-060
- [ ] Write PostgreSQL init script `deploy/postgres/init.sql`: `CREATE EXTENSION IF NOT EXISTS vector; CREATE EXTENSION IF NOT EXISTS pgcrypto;`
- [ ] Write `deploy/nginx/vektra.conf.example`: TLS termination config, proxy_pass to vektra:8000, headers (X-Request-ID, X-Forwarded-For), TLS 1.2+ ciphers
- [ ] Write `deploy/traefik/docker-compose.traefik.yml.example`: Traefik entrypoints and Let's Encrypt resolver for vektra service
- [ ] Write `deploy/postgres/encryption.md`: PostgreSQL TDE documentation and verification checklist (NFR-013)
- [ ] Verify `docker-compose up -d` reaches healthy state within 60 seconds on a clean machine with the required env vars set (NFR-004 manual verification)
- [ ] Verify data durability: ingest a document, `docker-compose restart vektra`, verify document still queryable (NFR-005)
- [ ] Verify resource ceiling: `make demo` completes on 4GB/2-core system without OOM (NFR-006, documented, not enforced in CI)

## Acceptance Criteria

- [ ] `docker-compose up -d` starts all required services and reaches healthy state in < 60 seconds
- [ ] `docker-compose --profile local-llm up -d` starts Ollama in addition
- [ ] Vektra container runs as non-root user
- [ ] `.env.example` contains all required variables with comments explaining each
- [ ] TLS example configs provided for both nginx and Traefik in deploy/
- [ ] Data survives container restart (NFR-005): indexed documents queryable after `docker-compose restart vektra`
- [ ] PostgreSQL encryption at rest documented in deploy/postgres/encryption.md (NFR-013)

## Testing Approach

Manual testing of the docker-compose stack on the developer machine and in CI (see infra-ci plan). The CI integration test pipeline spins up the stack and validates the full ingest-query flow.

## Integration Notes

This plan depends on infra-app-entrypoint being complete (the container must have something to run). CI plan (infra-ci) depends on this plan (uses the docker-compose stack for integration tests). Makefile plan (infra-makefile) also depends on this.
