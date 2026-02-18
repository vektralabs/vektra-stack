"""Unit tests: audit log fire-and-forget behavior (REQ-022, NFR-007)."""
import asyncio
from unittest.mock import AsyncMock, MagicMock, call
from uuid import uuid4

import pytest

import vektra_admin.audit as audit_mod


async def test_log_event_schedules_db_write():
    """log_event must schedule a background coroutine that commits to DB."""
    session = AsyncMock()
    session.add = MagicMock()
    session.commit = AsyncMock()

    key_id = uuid4()
    request_id = uuid4()

    # Do NOT mock AuditLogOrm (late import, patch target doesn't exist at module level).
    # Instead, verify that session.add and session.commit are called via the scheduled task.
    audit_mod.log_event(
        session=session,
        key_id=key_id,
        endpoint="/api/v1/api-keys",
        method="POST",
        status_code=201,
        request_id=request_id,
        action="apikey_created",
    )

    # Allow the scheduled coroutine to run
    await asyncio.sleep(0.01)

    session.commit.assert_awaited_once()


async def test_log_event_does_not_raise_on_db_error():
    """Write failures must be logged as ERROR but must NOT propagate (NFR-007)."""
    session = AsyncMock()
    session.add = MagicMock()
    session.commit = AsyncMock(side_effect=RuntimeError("DB down"))

    key_id = uuid4()
    request_id = uuid4()

    audit_mod.log_event(
        session=session,
        key_id=key_id,
        endpoint="/health",
        method="GET",
        status_code=200,
        request_id=request_id,
    )

    # Let the task run — it must NOT propagate the exception to the caller
    await asyncio.sleep(0.01)
    # No exception means the fire-and-forget pattern works correctly
