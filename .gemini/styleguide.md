# Vektra coding conventions for Gemini Code Assist

## General

- Python 3.12+. Use modern syntax: `X | Y` union types, `match` statements where appropriate.
- All public functions and methods must have type annotations.
- Use `from __future__ import annotations` at the top of every module.
- Async-first: all I/O operations (database, HTTP, file reads) must be async.

## Architecture boundaries

The five components (vektra_shared, vektra_admin, vektra_core, vektra_ingest, vektra_index)
are independent modules. Cross-component imports are forbidden except through vektra_shared.
If a change requires importing from another component, flag it — it likely needs a Protocol
interface instead.

## Database (SQLAlchemy 2.0 async)

- Use `mapped_column()` and `Mapped[T]` for all ORM model fields.
- Never use `metadata` as a column name (reserved by DeclarativeBase). Use `chunk_metadata`.
- Always use `AsyncSession` from `sqlalchemy.ext.asyncio`.
- Session lifetime must match request lifetime — never store sessions as instance attributes.

## FastAPI

- All route handlers must be `async def`.
- Authentication is dependency-injected via `Depends()`. Never inline auth logic in handlers.
- HTTP error responses use `ErrorResponse.to_envelope()` for the `detail` field.
  Tests must check `body["detail"]["error"]["code"]`, not `body["error"]["code"]`.
- Use `app.dependency_overrides` in tests, not `patch()` for FastAPI dependencies.

## Error handling

- All user-facing errors must use the error code registry (ERR-* codes in vektra_shared.errors).
- Every error response must include a `remediation` field — empty remediation is a test failure.

## Tests

- Unit tests: no real database, no real HTTP calls. Use mocks for external dependencies.
- Do NOT mock SQLAlchemy ORM classes — this breaks `select()`. Mock `session.execute()` instead.
- Integration tests (marked `@pytest.mark.integration`): use testcontainers for PostgreSQL.
- Coverage target: 80% minimum for unit tests.

## Logging

- Use structlog. All log calls must be keyword-only: `log.info("event_name", key=value)`.
- Never log raw exception objects — use `error=str(exc)`.
- PII (email addresses, names, tokens) must never appear in log messages.
