"""Bootstrap key management: single-use enforcement (REQ-036, ARCH-025).

The bootstrap key (VEKTRA_ADMIN_BOOTSTRAP_KEY env var) allows creating the
first API key without requiring an existing key. It is consumed on first
successful use and stored permanently in system_state table.

After consumption, any request bearing the bootstrap key returns 401.
Consumed state survives container restarts (stored in PostgreSQL, not memory).
"""

from __future__ import annotations

import os

import structlog
from sqlalchemy import select, update
from sqlalchemy.ext.asyncio import AsyncSession

log = structlog.get_logger(__name__)

_BOOTSTRAP_KEY_STATE_ROW = "bootstrap_consumed"


def get_bootstrap_key() -> str | None:
    """Return the configured bootstrap key, or None if not set."""
    return os.environ.get("VEKTRA_ADMIN_BOOTSTRAP_KEY")


def is_bootstrap_key(token: str) -> bool:
    """Return True if the token matches the configured bootstrap key."""
    key = get_bootstrap_key()
    if key is None:
        return False
    # Constant-time comparison to prevent timing attacks
    import hmac

    return hmac.compare_digest(token, key)


async def is_bootstrap_consumed(session: AsyncSession) -> bool:
    """Check whether the bootstrap key has already been consumed.

    Reads from system_state; returns True if the key was already used.
    """
    from vektra_admin.models import (
        SystemStateOrm,  # late import: avoids circular ORM load
    )

    result = await session.execute(
        select(SystemStateOrm)
        .where(SystemStateOrm.key == _BOOTSTRAP_KEY_STATE_ROW)
        .with_for_update()
    )
    row = result.scalar_one_or_none()
    if row is None:
        # Row should exist (seeded by migration); treat as consumed for safety
        log.warning("bootstrap_state_row_missing", key=_BOOTSTRAP_KEY_STATE_ROW)
        return True
    return row.value == "true"


def warn_if_bootstrap_in_production() -> None:
    """Log a warning if the bootstrap key is set in a production environment (REQ-021).

    Call this once at startup (infra-app-entrypoint step 5) before serving requests.
    """
    key = get_bootstrap_key()
    env = os.environ.get("VEKTRA_ENV", "development")
    if key and env == "production":
        log.warning(
            "bootstrap_key_set_in_production",
            message=(
                "VEKTRA_ADMIN_BOOTSTRAP_KEY is set in production. "
                "Create a permanent API key and unset the bootstrap key (REQ-021)."
            ),
        )


async def consume_bootstrap_key(session: AsyncSession) -> None:
    """Mark the bootstrap key as consumed. Must be called inside an open transaction.

    Uses UPDATE ... SET value='true' rather than a select-then-update to
    minimise the window for concurrent consumption. The caller is responsible
    for committing the transaction.
    """
    from vektra_admin.models import SystemStateOrm  # late import

    await session.execute(
        update(SystemStateOrm)
        .where(SystemStateOrm.key == _BOOTSTRAP_KEY_STATE_ROW)
        .values(value="true")
    )
    log.info("bootstrap_key_consumed")
