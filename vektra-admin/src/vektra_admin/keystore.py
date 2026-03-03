"""In-memory API key cache implementing KeyStoreProvider Protocol (ARCH-023).

The cache is populated at startup by loading all non-revoked api_keys from
the database. At runtime it is updated immediately on key creation and
revocation — TTL (60s) is a fallback for external revocation only.

Lookup strategy (argon2id does not allow direct hash lookup):
  1. Check LRU-cached verifications (O(1), covers repeated tokens).
  2. Filter by key_preview (last 4 chars of the plaintext token) to narrow candidates.
  3. argon2id-verify each candidate (O(candidates * hash_cost)).

For Phase 1 deployments with few keys this is fast; the LRU cache in
vektra_admin.keys eliminates repeated argon2 calls for active tokens.
"""

from __future__ import annotations

import asyncio
from dataclasses import dataclass
from datetime import UTC, datetime
from uuid import UUID

import structlog
from sqlalchemy.ext.asyncio import AsyncSession

from vektra_admin.keys import verify_key
from vektra_shared.auth import ApiKeyInfo

log = structlog.get_logger(__name__)


@dataclass
class _KeyEntry:
    key_id: UUID
    key_hash: str
    key_preview: str
    scopes: list[str]
    revoked_at: datetime | None
    expires_at: datetime | None = None
    rate_limit_rpm: int | None = None


class InMemoryKeyStore:
    """Thread-safe in-memory key store used by the vektra_shared auth middleware.

    Registered in ProviderRegistry as ("key_store", "default") by
    infra-app-entrypoint at startup.
    """

    def __init__(self) -> None:
        # Primary index: key_hash → _KeyEntry
        self._by_hash: dict[str, _KeyEntry] = {}
        # Secondary index: key_preview → set of key_hashes (for fast narrowing)
        self._by_preview: dict[str, set[str]] = {}
        self._lock = asyncio.Lock()

    # ------------------------------------------------------------------
    # KeyStoreProvider Protocol
    # ------------------------------------------------------------------

    async def lookup_by_token(self, token: str) -> ApiKeyInfo | None:
        """Validate a Bearer token and return key info, or None if invalid/revoked.

        Argon2id verification is slow by design; the LRU cache in
        vektra_admin.keys handles repeated lookups efficiently.
        """
        preview = token[-4:] if len(token) >= 4 else token
        candidate_hashes = self._by_preview.get(preview, set())

        for key_hash in list(candidate_hashes):
            entry = self._by_hash.get(key_hash)
            if entry is None:
                continue
            if not await verify_key(token, key_hash):
                continue
            # Found a matching key
            if entry.revoked_at is not None:
                log.info("auth_rejected_revoked_key", key_id=str(entry.key_id))
                return None
            if entry.expires_at is not None and entry.expires_at <= datetime.now(UTC):
                log.info("auth_rejected_expired_key", key_id=str(entry.key_id))
                return None
            return ApiKeyInfo(
                key_id=entry.key_id,
                scopes=entry.scopes,
                rate_limit_rpm=entry.rate_limit_rpm,
            )

        return None

    # ------------------------------------------------------------------
    # Runtime cache maintenance (called from API handlers)
    # ------------------------------------------------------------------

    async def add_key(
        self,
        key_id: UUID,
        key_hash: str,
        key_preview: str,
        scopes: list[str],
        expires_at: datetime | None = None,
    ) -> None:
        """Add a newly created key to the cache immediately."""
        entry = _KeyEntry(
            key_id=key_id,
            key_hash=key_hash,
            key_preview=key_preview,
            scopes=scopes,
            revoked_at=None,
            expires_at=expires_at,
        )
        async with self._lock:
            self._by_hash[key_hash] = entry
            self._by_preview.setdefault(key_preview, set()).add(key_hash)

    async def revoke_key(self, key_hash: str) -> None:
        """Mark an existing key as revoked in the cache immediately."""
        async with self._lock:
            entry = self._by_hash.get(key_hash)
            if entry is not None:
                entry.revoked_at = datetime.now(UTC)

    async def revoke_key_by_id(self, key_id: UUID) -> None:
        """Mark a key revoked by its UUID (used when we don't have the hash)."""
        async with self._lock:
            for entry in self._by_hash.values():
                if entry.key_id == key_id:
                    entry.revoked_at = datetime.now(UTC)
                    return

    # ------------------------------------------------------------------
    # Startup population
    # ------------------------------------------------------------------

    async def load_from_db(self, session: AsyncSession) -> int:
        """Load all non-revoked API keys from the database into the cache.

        Returns the number of keys loaded. Called once at startup before
        the first request is served (ARCH-057 step 5).
        """
        from sqlalchemy import or_, select
        from sqlalchemy.sql import func

        from vektra_admin.models import ApiKeyOrm  # late import

        result = await session.execute(
            select(ApiKeyOrm).where(
                ApiKeyOrm.revoked_at.is_(None),
                or_(
                    ApiKeyOrm.expires_at.is_(None),
                    ApiKeyOrm.expires_at > func.now(),
                ),
            )
        )
        rows = result.scalars().all()

        async with self._lock:
            for row in rows:
                entry = _KeyEntry(
                    key_id=row.id,
                    key_hash=row.key_hash,
                    key_preview=row.key_preview,
                    scopes=row.scopes,
                    revoked_at=None,
                    expires_at=row.expires_at,
                    rate_limit_rpm=row.rate_limit_rpm,
                )
                self._by_hash[row.key_hash] = entry
                self._by_preview.setdefault(row.key_preview, set()).add(row.key_hash)

        log.info("keystore_loaded", count=len(rows))
        return len(rows)

    def size(self) -> int:
        """Return the number of cached key entries (including revoked)."""
        return len(self._by_hash)
