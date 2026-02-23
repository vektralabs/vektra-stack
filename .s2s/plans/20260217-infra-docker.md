---
provides_requires:
  provides:
    - "docker.compose:stack"
    - "vektra-worker:service"
  requires:
    - "FastAPI.app:factory"
---
# Implementation Plan: Docker Compose stack, Dockerfile, and deployment configs

**ID**: 20260217-infra-docker
**Status**: in_progress
**Branch**: feat/wave-5-infra-docker
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

- Multi-stage Dockerfile: `builder` stage installs dependencies with uv, `runtime` stage copies only the installed packages and source. Non-root user (uid 1000) in runtime stage. HEALTHCHECK calls GET /health with `start_period=30s` to cover the embedding model warm-up; without this, Docker marks the container unhealthy during normal startup before the model finishes loading.
- Single image, two roles: `docker/entrypoint.sh` reads `CMD_TARGET` env var to exec either `uvicorn` (API server) or `arq` (background worker). Both roles use the same Docker image. This avoids maintaining two Dockerfiles or multi-stage build targets.
- docker-compose.yml services: `postgres` (pgvector/pgvector:pg16 — preinstalled vector extension), `vektra` (CMD_TARGET=server, ports 8000), `vektra-worker` (CMD_TARGET=worker, no ports, depends on vektra being healthy), `ollama` (profile: local-llm), `qdrant` (profile: qdrant, Phase 2 placeholder).
- Memory limits: postgres 512MB, vektra API server 2GB (includes sentence-transformers model ~500MB), vektra-worker 1GB (arq worker also loads the model for embedding during ingestion — consider whether EmbeddingProvider should be a separate service in Phase 2 if memory pressure becomes an issue), ollama 4GB.
- TLS termination at reverse proxy. Application layer: when VEKTRA_ENV=production, reject plain HTTP connections with 400. Example configs in `deploy/nginx/` and `deploy/traefik/`.
- docker-compose.override.yml for local development: volume mounts for hot reload, DEBUG log level, no TLS enforcement.

## Tasks

- [x] Write `Dockerfile`: multi-stage build (python:3.12-slim + uv 0.6). Builder: `--no-install-workspace` then `--no-editable --no-dev`. Runtime: libmagic1 + curl, non-root user uid=1000, HEALTHCHECK with 30s start_period. Added `.dockerignore`. Added `alembic>=1.13` to vektra-app production deps for in-container migrations.
- [x] Write `docker-compose.yml`: three-service stack per ADR-0004. `postgres` (pgvector:pg16, 512MB), `vektra` (build from Dockerfile, 2GB), `ollama` (profile: local-llm, 4GB), `qdrant` (profile: qdrant, 1GB). No Redis or vektra-worker: ADR-0006 specifies in-process arq for Phase 1; async ingest uses FastAPI BackgroundTasks. Phase 2: add redis + vektra-worker for scaling.
- [x] Write `docker/entrypoint.sh`: CMD_TARGET dispatch (server, migrate). Server runs `alembic upgrade head` then uvicorn. No worker target in Phase 1 (in-process mode per ADR-0006).
- [x] Write `docker-compose.override.yml`: sets VEKTRA_ENV=development and VEKTRA_STARTUP_LLM_CHECK=false. No volume mounts (packages are non-editable; use `uv run uvicorn` locally for live reload).
- [x] Write `.env.example`: all 37 ARCH-060 variables documented with defaults. Required: VEKTRA_LLM_PROVIDER. Grouped by category with LLM provider examples (Ollama, OpenAI, Anthropic).
- [x] Write PostgreSQL init script `deploy/postgres/init.sql`: `CREATE EXTENSION IF NOT EXISTS vector; CREATE EXTENSION IF NOT EXISTS pgcrypto;`
- [x] Write `deploy/nginx/vektra.conf.example`: TLS termination config, proxy_pass to vektra:8000, headers (X-Request-ID, X-Forwarded-For), TLS 1.2+ ciphers, SSE streaming support, HSTS header.
- [x] Write `deploy/traefik/docker-compose.traefik.yml.example`: Traefik v3, Let's Encrypt ACME, HTTP-to-HTTPS redirect, SSE streaming flush.
- [x] Write `deploy/postgres/encryption.md`: PostgreSQL TDE documentation and verification checklist (NFR-013). Covers pgcrypto, LUKS, Docker encrypted volumes, and cloud-managed encryption.
- [ ] Verify `docker-compose up -d` reaches healthy state within 60 seconds on a clean machine with the required env vars set (NFR-004 manual verification)
- [ ] Verify data durability: ingest a document, `docker-compose restart vektra`, verify document still queryable (NFR-005)
- [ ] Verify resource ceiling: `make demo` completes on 4GB/2-core system without OOM (NFR-006, documented, not enforced in CI)

## Acceptance Criteria

- [ ] `docker-compose up -d` starts postgres and vektra; both reach healthy/running state in < 60 seconds (NFR-004)
- [ ] `docker-compose --profile local-llm up -d` starts Ollama in addition
- [ ] Async ingestion works in-process via BackgroundTasks (ADR-0006 Phase 1 mode)
- [ ] Vektra container runs as non-root user
- [ ] `.env.example` contains all required variables with comments explaining each
- [ ] TLS example configs provided for both nginx and Traefik in deploy/
- [ ] Data survives container restart (NFR-005): indexed documents queryable after `docker-compose restart vektra`
- [ ] PostgreSQL encryption at rest documented in deploy/postgres/encryption.md (NFR-013)

## Testing Approach

Manual testing of the docker-compose stack on the developer machine and in CI (see infra-ci plan). The CI integration test pipeline spins up the stack and validates the full ingest-query flow.

## Integration Notes

This plan depends on infra-app-entrypoint being complete (the container must have something to run). CI plan (infra-ci) depends on this plan (uses the docker-compose stack for integration tests). Makefile plan (infra-makefile) also depends on this.

## Notes

### Session 2026-02-23: config tasks complete, verification pending

**Completed (tasks 1-9):**
- `Dockerfile`: multi-stage build (python:3.12-slim + uv 0.6), non-editable install, non-root user, HEALTHCHECK with 30s start_period. Added `.dockerignore`.
- `docker-compose.yml`: three-service stack per ADR-0004 (postgres, vektra, ollama/qdrant profiles). No Redis/worker: ADR-0006 in-process mode.
- `docker/entrypoint.sh`: dispatches server/migrate via CMD_TARGET. Server runs alembic upgrade head before uvicorn.
- `vektra-app/pyproject.toml`: added `alembic>=1.13` as production dependency.
- `.env.example`: all 37 ARCH-060 variables documented.
- `deploy/postgres/init.sql`, `deploy/nginx/vektra.conf.example`, `deploy/traefik/docker-compose.traefik.yml.example`, `deploy/postgres/encryption.md`.
- `docker-compose.override.yml`: development-friendly env vars.

**Correction after review:**
- Removed `redis` service and `vektra-worker` service. ADR-0006 specifies arq in in-process mode for Phase 1 (PostgreSQL-backed, no Redis). The plan's vektra-worker conflicted with the architecture. Reverted: worker.py deleted, arq pool registration in main.py removed, arq dep removed from vektra-app.
- Phase 2: when scaling is needed, add redis + vektra-worker + arq pool registration.

**Pending (tasks 10-12):** require running Docker daemon for manual verification.

**Next:** start Docker Desktop, then `docker compose up -d` to verify tasks 10-12.
