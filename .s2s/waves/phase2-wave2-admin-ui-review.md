# Code review: admin-ui (plan #8)

**Branch**: feat/phase2-wave2-admin-ui
**Commit**: 9c3ef99
**Date**: 2026-03-07
**Reviewer**: Claude Opus 4.6

---

## Summary

| Severity | Count |
|----------|-------|
| HIGH     | 0     |
| MEDIUM   | 3     |
| LOW      | 4     |
| **Total** | **7** |

---

## Findings

### R1 — Dead code: RedirectResponse created but never returned (MEDIUM)

**File**: `ui.py:81-82`
**Category**: dead code

```python
response = RedirectResponse(url="/admin/login", status_code=303)
response.delete_cookie(_COOKIE_NAME)  # dead code - response is discarded
raise HTTPException(status_code=303, headers={"Location": "/admin/login"})
```

The `RedirectResponse` is constructed with `delete_cookie` but immediately discarded. The `HTTPException` on line 83 is what executes. The invalid-token cookie is never cleared.

**Fix**: remove the dead `RedirectResponse` and use a proper redirect (see R2).

---

### R2 — HTTPException with 303 doesn't redirect browsers (MEDIUM)

**File**: `ui.py:58`, `ui.py:83`
**Category**: functional

`HTTPException(status_code=303, headers={"Location": ...})` returns a JSON error body with a 303 status code. Browsers don't follow this as a redirect because it's not a proper redirect response (missing `Location` in the response, just in error detail). Works in tests because httpx sees the 303, but a real browser gets a JSON page.

**Fix**: raise a custom exception or return `RedirectResponse` directly. The auth dependency can't return a response directly (it's a dependency, not a handler), so either:
- Use a custom exception handler that converts 303 HTTPExceptions to RedirectResponse
- Or restructure the dependency to not raise

---

### R3 — Audit cursor pagination: UUID comparison is unordered (MEDIUM)

**File**: `ui.py:547`
**Category**: functional

```python
stmt = stmt.where(AuditLogOrm.id < cursor_uuid)
```

UUIDv4 values are random. `id < cursor_uuid` does not correlate with chronological order. When paginating "older" entries, this can skip or repeat rows unpredictably.

**Fix**: use `created_at` as the primary cursor field with `id` as tiebreaker, or switch to offset-based pagination.

---

### R4 — Missing `flash` variable on initial page load (LOW)

**File**: `templates/partials/keys_table.html:1`, `templates/partials/namespaces_table.html:1`
**Category**: robustness

On initial `GET /admin/keys`, the `flash` variable is not passed to the template context. `{% if flash %}` works because Jinja2's default `Undefined` evaluates to falsy, but this is fragile if `StrictUndefined` is ever enabled.

**Fix**: pass `flash=None` in the initial page render context, or use `{% if flash is defined and flash %}`.

---

### R5 — Namespace delete is hard DELETE, not soft delete (LOW)

**File**: `ui.py:437`
**Category**: design inconsistency

`await session.delete(ns)` does a SQL DELETE. The plan design notes say "soft-delete". However, `NamespaceOrm` doesn't have a `deleted_at` column, so hard delete is the only option with the current schema. Acceptable for now.

**Status**: WONTFIX (schema limitation)

---

### R6 — Unused import `_SECRET_KEYWORDS` in test (LOW)

**File**: `test_ui.py:14`
**Category**: dead code

`_SECRET_KEYWORDS` is imported but never referenced in any test.

**Fix**: remove the import.

---

### R7 — `generate_key` imported inside function body (LOW)

**File**: `ui.py:259`
**Category**: style

`from vektra_admin.keys import generate_key` is a late import inside `keys_create()`. This is an intra-package import (vektra_admin importing from vektra_admin), so there's no circular dependency risk and no import-linter reason for it. Can be moved to the top-level imports.

**Status**: WONTFIX (cosmetic, no functional impact)

---

## Action plan

| ID | Severity | Action | Status |
|----|----------|--------|--------|
| R1 | MEDIUM | Fix dead code, merge with R2 fix | fixed |
| R2 | MEDIUM | Replace HTTPException(303) with proper redirect | fixed |
| R3 | MEDIUM | Fix cursor pagination to use created_at | fixed |
| R4 | LOW | Pass flash=None or use `is defined` check | fixed |
| R5 | LOW | WONTFIX (no deleted_at column on namespaces) | closed |
| R6 | LOW | Remove unused import | fixed |
| R7 | LOW | WONTFIX (cosmetic) | closed |
