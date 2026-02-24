# Implementation Plan: Makefile targets and operator shell scripts

**ID**: 20260217-infra-makefile
**Status**: active
**Branch**: N/A
**Created**: 2026-02-17T22:42:39Z
**Updated**: 2026-02-17T22:42:39Z

## Traceability

**Source**: infra-makefile
**Source Type**: architecture

## References

### Requirements
- REQ-005: WF-INT-001: End-to-End MVP Validation workflow @.s2s/requirements.md
- REQ-007: Phase 1 interaction mode: API + shell scripts @.s2s/requirements.md
- REQ-018: Makefile targets for MVP workflow @.s2s/requirements.md
- REQ-027: REQ-005 checkpoint decomposition @.s2s/requirements.md
- REQ-032: Integration acceptance test @.s2s/requirements.md

### Architecture
none specific

### Decisions
none specific

### Dependencies
- 20260217-infra-docker
- 20260217-component-core
- 20260217-component-ingest

## Overview

Implements all required Makefile targets and operator scripts. The `make demo` target is the primary MVP validation tool: it runs the complete end-to-end workflow from compose-up to successful query, using the sample PDF committed to the repository. All targets must work on Linux and macOS without additional dependencies beyond bash and docker.

## Design Notes

- Makefile uses `.PHONY` for all targets. Each target outputs its purpose on start and success/failure on end.
- `make help` uses `##` comment convention parsed by awk to generate formatted output.
- Shell scripts in `scripts/` are wrappers around API calls via curl. They are standalone (not sourced), executable, and tested in CI.
- `make demo` implements the REQ-027 checkpoints: T+0:30 compose up, T+2:00 health 200, T+5:00 ingest complete, T+6:00 query response with source reference.
- All scripts use `set -euo pipefail` and validate inputs before making API calls.

## Tasks

- [ ] Write `Makefile` with targets: `help` (awk-parsed ##comments), `up` (docker compose up -d), `down` (docker compose down), `health` (curl GET /health), `ingest FILE=` (curl POST /ingest with file path, requires VEKTRA_API_KEY env var), `query Q=` (curl POST /query, requires VEKTRA_API_KEY), `demo` (full MVP workflow, see below), `logs` (docker compose logs -f), `test` (pytest integration tests), `lint` (ruff + mypy + import-linter)
- [ ] Implement `make demo` script: (1) docker compose up -d, (2) wait loop for /health 200 with 2-minute timeout, (3) POST /ingest with tests/fixtures/sample.pdf using bootstrap key to create first API key, (4) poll ingest job until INDEXED, (5) POST /query with a question about the sample PDF, (6) assert response includes the sample document's document_id in sources array, (7) print elapsed time; fail with clear message if any step exceeds checkpoint
- [ ] Write `scripts/health.sh`: GET /health (shallow) and GET /health?detail=full (authenticated), pretty-print JSON output
- [ ] Write `scripts/ingest.sh FILE NAMESPACE`: validate file exists, detect content type, POST /ingest, poll until complete, print document_id and chunk_count
- [ ] Write `scripts/query.sh "QUESTION" [CONVERSATION_ID]`: POST /query, print answer and sources table
- [ ] Write `scripts/create-key.sh LABEL [SCOPE1 SCOPE2]`: POST /api-keys using bootstrap key or existing admin key, print new key value with warning to save it
- [ ] Make all scripts in `scripts/` executable (`chmod +x`) and add shebang `#!/usr/bin/env bash`
- [ ] Verify all targets work on macOS (BSD sed, etc.) and Linux (GNU sed); use POSIX-compatible commands only
- [ ] Test `make demo` completes within 5 minutes on a machine where all Docker images are already pulled (REQ-018 "make demo completes in under 5 minutes on first run" - images pre-pulled)

## Acceptance Criteria

- [ ] `make help` output fits in 80-column terminal without scrolling
- [ ] `make demo` completes end-to-end in < 5 minutes (images pre-pulled) with zero manual steps
- [ ] `make demo` output shows elapsed time at each REQ-027 checkpoint
- [ ] All targets work on macOS and Linux without modification
- [ ] Shell scripts exit non-zero with clear error message on invalid input (missing file, missing env var)
- [ ] `scripts/` directory committed with executable permissions

## Testing Approach

Shell script tests in CI (`shell-scripts.yml` from infra-ci plan): run each script with `--help` or valid/invalid inputs in a sandboxed environment. `make demo` validated in CI integration workflow. Manual testing on macOS and Linux.

## Integration Notes

`make demo` uses `tests/fixtures/sample.pdf` (committed by infra-ci plan). Scripts require `VEKTRA_API_KEY` or `VEKTRA_BOOTSTRAP_KEY` environment variables - documented in `.env.example` (infra-docker plan). The sample PDF and its expected query answer should be documented in the getting-started guide (docs-001 plan).
