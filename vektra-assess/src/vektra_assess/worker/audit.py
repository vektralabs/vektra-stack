"""Audit hook for the worker runtime: one append-only row per event, in its own transaction.

The runtime and the reconciliation pass call this **before** they mark a job ``dead``; a failure
propagates (they leave the job leased and retry), unlike ``vektra_shared.audit.log_event`` which
never raises. Rows are content-free: ``data`` holds ids, counts and a fixed error code, and the
table caps it at 2 KiB. ``actor_kind`` is ``worker``.
"""

from __future__ import annotations

import json
from collections.abc import Mapping
from typing import Any

from sqlalchemy import text

from vektra_assess.worker.runner import AcquireDb
from vektra_assess.worker.runtime import Audit

# INSERT only, on purpose: the worker role has no SELECT on ``assess_audit_events`` (the log is
# append-only and the DB boundary gives no role a read of it), so the hook cannot check for an
# earlier row. The two writes that matter (this row, then ``dead``) are separate transactions: if the
# update fails after the audit committed, the reaper retries and the log holds a second
# ``assess_job_dead`` for that job. A duplicate in an append-only trail is acknowledged and rare (it
# needs two failures in a row); consumers identify the job by ``data.job_id``.
_INSERT = text(
    "INSERT INTO assess_audit_events (event, reason_code, actor_kind, data) "
    "VALUES (:event, :reason, 'worker', CAST(:data AS jsonb))"
)


def make_audit(acquire_db: AcquireDb) -> Audit:
    async def audit(event: str, data: Mapping[str, Any]) -> None:
        reason = data.get("error_code")
        async with acquire_db() as conn:
            await conn.execute(
                _INSERT,
                {
                    "event": event,
                    "reason": reason if isinstance(reason, str) else None,
                    "data": json.dumps(dict(data), sort_keys=True),
                },
            )
            await conn.commit()

    return audit
