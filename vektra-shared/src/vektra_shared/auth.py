"""FastAPI auth middleware for API key authentication (REQ-019, REQ-030, REQ-041).

Single trust boundary: all endpoints use `require_scope(scope)` as a FastAPI
Depends(). The key store is retrieved from `request.app.state.registry` and
must be registered under category="key_store", name="default" at startup.

Scope values (REQ-031): "admin", "ingest", "query".
Error codes (REQ-041): ERR-AUTH-001 (invalid/revoked token), ERR-AUTH-003 (wrong scope).
"""
from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any, Callable, Coroutine, Protocol
from uuid import UUID

from fastapi import HTTPException, Request, Security
from fastapi.security import HTTPAuthorizationCredentials, HTTPBearer

from vektra_shared.errors import (
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


def require_scope(required_scope: str) -> Callable[..., Coroutine[Any, Any, ApiKeyInfo]]:
    """Return a FastAPI dependency that enforces the given API key scope.

    Usage:
        @router.get("/endpoint")
        async def endpoint(key: ApiKeyInfo = Depends(require_scope("admin"))):
            ...

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
                detail={"error": {"message": "ProviderRegistry not initialized on app state"}},
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

        # 4. Check scope
        if not info.has_scope(required_scope):
            err = auth_insufficient_scope(required_scope)
            raise HTTPException(
                status_code=http_status_for(err),
                detail=err.to_envelope(),
            )

        return info

    # Give the dependency a descriptive name for FastAPI's OpenAPI schema
    _dependency.__name__ = f"require_scope_{required_scope}"
    return _dependency
