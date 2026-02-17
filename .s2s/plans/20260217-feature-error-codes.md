# Implementation Plan: Error code registry and actionability enforcement

**ID**: 20260217-feature-error-codes
**Status**: active
**Branch**: N/A
**Created**: 2026-02-17T22:42:39Z
**Updated**: 2026-02-17T22:42:39Z

## Traceability

**Source**: feature-error-codes
**Source Type**: architecture

## References

### Requirements
- REQ-009: API error responses include remediation guidance @.s2s/requirements.md
- REQ-010: Error response envelope schema @.s2s/requirements.md
- REQ-011: Phase 1 error code registry @.s2s/requirements.md
- REQ-030: Authentication error response contracts @.s2s/requirements.md
- REQ-041: Unified authentication error codes @.s2s/requirements.md
- NFR-009: Error actionability (100%) @.s2s/requirements.md

### Architecture
- ARCH-020: Error envelope and code registry @.s2s/architecture.md

### Decisions
none specific

### Dependencies
- 20260217-component-shared
- 20260217-component-core
- 20260217-component-ingest
- 20260217-component-index

## Overview

A focused deliverable: implement all 13 normative error codes from REQ-011, document them in `docs/error-codes.md`, and enforce actionability via a CI integration test that triggers each code and validates the response envelope. This is a cross-cutting concern that spans all components but is small enough to be a single plan.

## Design Notes

- All 13 normative error codes are constants in `vektra_shared/errors.py` (already planned in component-shared). This plan covers the documentation and the verification test.
- The integration test must trigger each error code with a real HTTP request to the running application (not a unit test), to verify the full middleware chain produces the correct envelope.
- Error codes added beyond the 13 normative ones (e.g., ERR-INGEST-005 for Word extraction failure) should be added to `docs/error-codes.md` before implementation per REQ-011.

## Tasks

- [ ] Write `docs/error-codes.md` with a table for each of the 13 normative error codes: code, category (TRANSIENT/PERMANENT/CONFIGURATION/UPSTREAM), HTTP status, message template, remediation text example
- [ ] Verify all 13 error codes are implemented in the component handlers: ERR-INGEST-001 (invalid PDF), ERR-INGEST-002 (PDF too large), ERR-INGEST-003 (scanned PDF), ERR-INGEST-004 (vector store write failed), ERR-QUERY-001 (no documents indexed), ERQ-QUERY-002 (LLM unavailable), ERR-QUERY-003 (query too long), ERR-QUERY-004 (vector store read failed), ERR-CONFIG-001 (missing config), ERR-CONFIG-002 (invalid provider config), ERR-AUTH-001 (invalid token), ERR-AUTH-002 (expired token, reserved), ERR-AUTH-003 (insufficient scope)
- [ ] Write integration test `tests/test_error_codes.py`: for each triggerable error code, send a crafted HTTP request to the running test application, assert response matches REQ-010 envelope schema (error.category, error.code, error.message non-empty, error.remediation non-empty, error.request_id present)
- [ ] Add error code validation to CI: run `test_error_codes.py` as part of the integration test suite; assert 100% of responses have non-empty remediation field (NFR-009 gate)
- [ ] Document in `docs/error-codes.md` the procedure for adding new error codes: add to registry first, implement second (REQ-011 convention)

## Acceptance Criteria

- [ ] All 13 normative error codes present in `docs/error-codes.md` with category, message template, and remediation
- [ ] Integration test triggers each code and asserts error.remediation is non-empty
- [ ] All error responses include error.request_id (correlation ID) matching the X-Request-ID header
- [ ] No error response contains stack trace or internal class names in error.message
- [ ] CI test suite includes and passes the error code validation test

## Testing Approach

Integration test with a running application (testcontainers or docker-compose test profile). Each test case: set up the triggering condition, make HTTP request, assert response schema and non-empty remediation. ERR-AUTH-002 (expired token) is reserved for Phase 2 - test can assert the code exists as a constant but no triggering test needed.

## Integration Notes

This plan documents and validates the error codes already implemented by component plans. The `docs/error-codes.md` file is referenced by the getting-started guide (docs-001 plan) and the API documentation.
