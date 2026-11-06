"""CodeRabbit 4493109352: audit rows carry the wall-clock time of the event, not the start of the
transaction they are written in (the burst's Start-to-commit latency is read from them)."""

from __future__ import annotations

import uuid

import pytest
from sqlalchemy import text

from vektra_assess.audit import AssessAuditEvent, write_event

pytestmark = pytest.mark.integration


async def test_two_events_in_one_transaction_keep_their_own_time(pg) -> None:  # type: ignore[no-untyped-def]
    marker = uuid.uuid4()
    async with pg.conn("api") as conn:
        await write_event(conn, AssessAuditEvent.START, actor="api", session_id=marker)
        await conn.execute(text("SELECT pg_sleep(0.3)"))
        await write_event(conn, AssessAuditEvent.COMMIT, actor="api", session_id=marker)
        await conn.commit()
    rows = await pg.run(
        "super",
        "SELECT extract(epoch FROM max(created_at) - min(created_at)) FROM assess_audit_events "
        "WHERE session_id = :s",
        {"s": marker},
    )
    assert float(rows[0][0]) >= 0.3
