"""FastAPI auth middleware for API key authentication (REQ-019, REQ-030, REQ-041).

Single trust boundary: all endpoints use `require_scope(scope)` as a FastAPI
Depends(). The key store is retrieved from `request.app.state.registry` and
must be registered under category="key_store", name="default" at startup.

Scope values (REQ-031): "admin", "ingest", "query".
Error codes (REQ-041): ERR-AUTH-001 (invalid/revoked token), ERR-AUTH-003 (wrong scope).
"""

from __future__ import annotations

from collections.abc import Callable, Coroutine
from dataclasses import dataclass, field
from typing import Any, Protocol
from uuid import UUID

from fastapi import HTTPException, Request, Security
from fastapi.security import HTTPAuthorizationCredentials, HTTPBearer

from vektra_shared.errors import (
    ERR_AUTH_004,
    ErrorCategory,
    ErrorResponse,
    auth_insufficient_scope,
    auth_invalid_token,
    http_status_for,
)

_bearer = HTTPBearer(auto_error=False)


# ---------------------------------------------------------------------------
# API key info returned from the key store and the auth dependency
# ---------------------------------------------------------------------------


@dataclass
class ApiKeyInfo:
    """Metadata about a validated API key."""

    key_id: UUID
    scopes: list[str]
    namespace_id: str | None = None
    rate_limit_rpm: int | None = None
    metadata: dict[str, Any] = field(default_factory=dict)

    def has_scope(self, scope: str) -> bool:
        return scope in self.scopes


# ---------------------------------------------------------------------------
# KeyStoreProvider Protocol - implemented by components using the DB
# ---------------------------------------------------------------------------


class KeyStoreProvider(Protocol):
    """Protocol for API key validation (registered in ProviderRegistry as 'key_store').

    The concrete implementation (in vektra-admin/vektra-core) uses SQLAlchemy
    to hash the token with argon2id and look it up in the api_keys table.
    """

    async def lookup_by_token(self, token: str) -> ApiKeyInfo | None:
        """Validate a Bearer token and return key info, or None if invalid/revoked."""
        ...


# ---------------------------------------------------------------------------
# Auth dependency factory
# ---------------------------------------------------------------------------


# ---------------------------------------------------------------------------
# Scope vocabulary (REQ-031, ARCH-067). Mirrored by the database constraints
# ck_api_keys_scopes and ck_api_keys_assess_scope_exclusive (migration 0009).
# ---------------------------------------------------------------------------

LEGACY_SCOPES: frozenset[str] = frozenset({"admin", "ingest", "query"})
# Site-bound scopes of the Moodle discussion plugin. Never satisfied by admin
# (require_exact_scope) and never combined with a legacy scope on one key.
ASSESS_SCOPES: frozenset[str] = frozenset(
    {"assess:mint", "assess:teacher", "assess:privacy-export", "assess:privacy-erase"}
)
ALL_SCOPES: frozenset[str] = LEGACY_SCOPES | ASSESS_SCOPES

ASSESS_SCOPE_EXCLUSIVE_MESSAGE = (
    "An API key holding an assess:* scope cannot also hold admin, ingest or query "
    "(ck_api_keys_assess_scope_exclusive)."
)


def scope_set_error(scopes: list[str] | set[str]) -> str | None:
    """Return why ``scopes`` cannot be stored on one key, or None if they can.

    Same rules as the database constraints, checked before the insert so callers
    answer 422 with a clear message instead of a 500 from an IntegrityError.
    """
    requested = set(scopes)
    if not requested:
        return "At least one scope is required."
    invalid = requested - ALL_SCOPES
    if invalid:
        return f"Invalid scopes: {sorted(invalid)}"
    if requested & ASSESS_SCOPES and requested & LEGACY_SCOPES:
        return ASSESS_SCOPE_EXCLUSIVE_MESSAGE
    return None


def require_scope(
    required_scope: str | None,
) -> Callable[..., Coroutine[Any, Any, ApiKeyInfo]]:
    """Return a FastAPI dependency that enforces the given API key scope.

    Usage:
        @router.get("/endpoint")
        async def endpoint(key: ApiKeyInfo = Depends(require_scope("admin"))):
            ...

    Pass None to require authentication without enforcing a specific scope.

    Admin scope is treated as a superscope: keys with "admin" scope pass
    any scope check (ARCH-059). Use ``require_exact_scope`` for scopes that
    admin must NOT satisfy (the site-bound ``assess:*`` scopes).

    The dependency reads the key store from request.app.state.registry.
    Raises HTTP 401 (ERR-AUTH-001) for missing/invalid tokens.
    Raises HTTP 403 (ERR-AUTH-003) for tokens with insufficient scope.
    """
    dependency = _scope_dependency(required_scope, admin_superscope=True)
    dependency.__name__ = f"require_scope_{required_scope or 'any'}"
    return dependency


def require_exact_scope(
    scope: str,
) -> Callable[..., Coroutine[Any, Any, ApiKeyInfo]]:
    """Like ``require_scope`` but without the ARCH-059 admin superscope.

    The key must hold ``scope`` itself; ``admin`` does not satisfy it, and no
    other scope does either (``assess:teacher`` is not ``assess:mint``). This is
    what makes "a leaked plugin key is not Vektra admin" and its converse hold
    (ARCH-067, NFR-014). Same envelopes: ERR-AUTH-001 (401), ERR-AUTH-003 (403).
    """
    if not scope:
        raise ValueError("require_exact_scope needs a non-empty scope")
    dependency = _scope_dependency(scope, admin_superscope=False)
    dependency.__name__ = f"require_exact_scope_{scope}"
    return dependency


def _scope_dependency(
    required_scope: str | None,
    *,
    admin_superscope: bool,
) -> Callable[..., Coroutine[Any, Any, ApiKeyInfo]]:
    async def _dependency(
        request: Request,
        credentials: HTTPAuthorizationCredentials | None = Security(_bearer),
    ) -> ApiKeyInfo:
        # 1. Extract Bearer token
        if credentials is None:
            err = auth_invalid_token()
            raise HTTPException(
                status_code=http_status_for(err),
                detail=err.to_envelope(),
            )

        token = credentials.credentials

        # 2. Retrieve key store from app registry
        registry = getattr(request.app.state, "registry", None)
        if registry is None:
            raise HTTPException(
                status_code=500,
                detail={
                    "error": {
                        "message": "ProviderRegistry not initialized on app state"
                    }
                },
            )

        try:
            key_store: KeyStoreProvider = registry.get("key_store", "default")
        except ValueError:
            raise HTTPException(
                status_code=500,
                detail={"error": {"message": "Key store provider not configured"}},
            )

        # 3. Validate token
        info = await key_store.lookup_by_token(token)
        if info is None:
            err = auth_invalid_token()
            raise HTTPException(
                status_code=http_status_for(err),
                detail=err.to_envelope(),
            )

        # 4. Expose key_id on request.state for audit attribution
        #    (set early so denied requests are still attributable)
        request.state.key_id = info.key_id

        # 5. Check scope (admin is a superscope per ARCH-059, unless exact)
        if required_scope is not None and not (
            info.has_scope(required_scope)
            or (admin_superscope and info.has_scope("admin"))
        ):
            err = auth_insufficient_scope(required_scope)
            raise HTTPException(
                status_code=http_status_for(err),
                detail=err.to_envelope(),
            )

        # 6. Rate limiting (optional, duck-typed from app.state)
        rate_limiter = getattr(request.app.state, "rate_limiter", None)
        if rate_limiter is not None and info.rate_limit_rpm is not None:
            allowed, rl_headers = rate_limiter.check(info.key_id, info.rate_limit_rpm)
            if not allowed:
                err = ErrorResponse(
                    category=ErrorCategory.TRANSIENT,
                    code=ERR_AUTH_004,
                    message="Rate limit exceeded.",
                    remediation="Wait and retry after the rate limit window resets.",
                )
                raise HTTPException(
                    status_code=http_status_for(err),
                    detail=err.to_envelope(),
                    headers=rl_headers,
                )
            # Store headers for response middleware to add
            request.state.rate_limit_headers = rl_headers

        return info

    return _dependency
