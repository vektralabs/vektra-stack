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
    any scope check (ARCH-059).

    The dependency reads the key store from request.app.state.registry.
    Raises HTTP 401 (ERR-AUTH-001) for missing/invalid tokens.
    Raises HTTP 403 (ERR-AUTH-003) for tokens with insufficient scope.
    """

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

        # 5. Check scope (admin is a superscope per ARCH-059)
        if required_scope is not None and not (
            info.has_scope(required_scope) or info.has_scope("admin")
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

    # Give the dependency a descriptive name for FastAPI's OpenAPI schema
    _dependency.__name__ = f"require_scope_{required_scope or 'any'}"
    return _dependency
