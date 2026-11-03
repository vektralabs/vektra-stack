"""The worker heartbeat: a file touch and a row upsert, both from the scheduler (ARCH-068).

Two pieces of evidence, deliberately different:

- the **file** (``VEKTRA_ASSESS_WORKER_HEARTBEAT_FILE``, tmpfs) is touched first, from the tick, so
  it only advances while the event loop runs; the container healthcheck reads its mtime and does
  not depend on the database;
- the **row** in ``assess_heartbeat`` (component ``worker``) proves the worker reaches Postgres and
  makes progress. ``/health/assess`` reads it over the API role, which has no access to
  ``assess_jobs``, so the worker publishes the one job figure readiness needs: the age of the oldest
  ``ready`` job. Everything in ``details`` is a count, a state word or an id: no job content.

``state`` is ``starting`` until reconciliation completes, then ``ready``; ``draining`` and ``stopped``
mark a shutdown, so readiness drops at once instead of waiting for the row to age out.
"""

from __future__ import annotations

import json
import logging
import os
from collections.abc import Callable, Mapping
from contextlib import AbstractAsyncContextManager
from datetime import UTC, datetime
from pathlib import Path
from typing import Any, Literal

from vektra_assess.worker import sql

log = logging.getLogger("vektra_assess.worker")

State = Literal["starting", "ready", "draining", "stopped"]

UPSERT = """
INSERT INTO assess_heartbeat (component, beat_at, details)
VALUES ('worker', now(), $1::jsonb)
ON CONFLICT (component) DO UPDATE SET beat_at = now(), details = EXCLUDED.details
"""


class Heartbeat:
    def __init__(
        self,
        acquire: Callable[[], AbstractAsyncContextManager[sql.Conn]],
        *,
        worker_id: str,
        version: str,
        file_path: Path,
        stats: Callable[[], Mapping[str, Any]] = lambda: {},
    ) -> None:
        self._acquire = acquire
        self._worker_id = worker_id
        self._version = version
        self._file = file_path
        self._stats = stats
        self._started_at = datetime.now(UTC)
        self.state: State = "starting"

    def touch_file(self) -> None:
        try:
            self._file.parent.mkdir(parents=True, exist_ok=True)
            self._file.touch()
            os.utime(self._file)
        except OSError:
            # the healthcheck will notice the stale mtime; do not stop the DB beat for it
            log.exception("assess_heartbeat_file_failed")

    async def beat(self) -> None:
        """One beat: file first (loop alive), then the row (database reachable)."""
        self.touch_file()
        async with self._acquire() as conn:
            age = await sql.oldest_ready_age_s(conn)
            details = {
                "worker_id": self._worker_id,
                "started_at": self._started_at.isoformat(),
                "version": self._version,
                "state": self.state,
                "oldest_ready_age_s": round(age, 1),
                **self._stats(),
            }
            await conn.execute(UPSERT, json.dumps(details))

    def set_state(self, state: State) -> None:
        self.state = state
