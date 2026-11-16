"""The API half of the per-turn course-module eligibility read (FEAT-028, ARCH-072 amendment).

The API cannot ask the LMS (only the worker holds the provider's credential). For every RAG
turn ``ApiEligibilityReader``:

1. in one transaction bound to the session's site, reads the database clock ``t0`` and queues a
   ``material_eligibility`` job (args: the session id and a fresh request id; one attempt);
2. polls ``assess_material_reads`` for the row of THAT request and THAT session written at or
   after ``t0`` (a row of another request, another session, or older than the turn is never
   used), at most ``VEKTRA_ASSESS_RAG_ELIGIBILITY_TIMEOUT_S``;
3. deletes the row it read, at once.

Nothing found in time is ``timeout``; a job that cannot be queued is ``enqueue_failed``. Both are
an unreadable read: the turn makes no retrieval call (fail closed) and the runner audits it.
Every read is measured (``observe(outcome, seconds)``: a closed outcome code, no content).
"""

from __future__ import annotations

import asyncio
import time
from collections.abc import Callable
from typing import Any
from uuid import UUID, uuid4

import structlog
from sqlalchemy import text

from vektra_assess.api.site_binding import bind_site
from vektra_assess.context.materials import EligibleModule
from vektra_assess.jobs import JobKind, enqueue_job
from vektra_assess.llm.types import EligibilityRead

log = structlog.get_logger(__name__)

TIMEOUT = "timeout"
ENQUEUE_FAILED = "enqueue_failed"
#: the outcome codes ``observe`` receives
OUTCOMES = ("readable", "unreadable", TIMEOUT, ENQUEUE_FAILED)

_CLOCK = text("SELECT clock_timestamp()")
_ROW = text(
    "SELECT readable, reason, namespace, modules FROM assess_material_reads "
    "WHERE id = :rid AND session_id = :sid AND read_at >= :t0"
)
_DELETE = text("DELETE FROM assess_material_reads WHERE id = :rid")


def dedupe_key(request_id: UUID) -> str:
    return f"material_eligibility:{request_id}"


def _modules(raw: Any) -> tuple[EligibleModule, ...] | None:
    """The worker's ``[[cmid, title], ...]``; None when it is not that shape (fail closed)."""
    if not isinstance(raw, list):
        return None
    out: list[EligibleModule] = []
    for item in raw:
        if (
            not isinstance(item, list)
            or len(item) != 2
            or not isinstance(item[0], int)
            or isinstance(item[0], bool)
            or not isinstance(item[1], str)
        ):
            return None
        out.append(EligibleModule(int(item[0]), item[1]))
    return tuple(out)


class ApiEligibilityReader:
    def __init__(
        self,
        engine: Any,
        *,
        timeout_s: float,
        poll_s: float = 0.1,
        observe: Callable[[str, float], None] | None = None,
        clock: Callable[[], float] = time.monotonic,
    ) -> None:
        #: an AsyncEngine, or a zero-argument callable returning one (resolved per read)
        self._engine = engine
        self._timeout_s = timeout_s
        self._poll_s = poll_s
        self._observe = observe
        self._clock = clock

    def _engine_now(self) -> Any:
        return self._engine() if callable(self._engine) else self._engine

    def _done(
        self, outcome: str, started: float, read: EligibilityRead
    ) -> EligibilityRead:
        elapsed = self._clock() - started
        if self._observe is not None:
            try:
                self._observe(outcome, elapsed)
            except Exception as exc:  # a metrics failure never fails the turn
                log.warning(
                    "assess_material_metric_failed", error_type=type(exc).__name__
                )
        log.info(
            "assess_material_eligibility",
            outcome=outcome,
            reason_code=read.reason,
            duration_ms=int(elapsed * 1000),
            count=len(read.modules),
        )
        return read

    async def __call__(self, session_id: UUID, site_id: UUID) -> EligibilityRead:
        started = self._clock()
        rid = uuid4()
        engine = self._engine_now()
        try:
            async with engine.begin() as conn:
                await bind_site(conn, site_id)
                t0 = (await conn.execute(_CLOCK)).scalar()
                queued = await enqueue_job(
                    conn,
                    JobKind.MATERIAL_ELIGIBILITY,
                    {"session_id": str(session_id), "request_id": str(rid)},
                    dedupe_key=dedupe_key(rid),
                    max_attempts=1,
                )
        except Exception as exc:
            log.warning("assess_material_enqueue_failed", error_type=type(exc).__name__)
            queued = False
        if not queued:
            return self._done(
                ENQUEUE_FAILED,
                started,
                EligibilityRead(False, reason=ENQUEUE_FAILED),
            )
        deadline = started + self._timeout_s
        while True:
            try:
                async with engine.begin() as conn:
                    await bind_site(conn, site_id)
                    row = (
                        await conn.execute(
                            _ROW, {"rid": rid, "sid": session_id, "t0": t0}
                        )
                    ).first()
                    if row is not None:
                        await conn.execute(_DELETE, {"rid": rid})
            except Exception as exc:  # a database hiccup: try again until the deadline
                log.warning(
                    "assess_material_poll_failed", error_type=type(exc).__name__
                )
                row = None
            if row is not None:
                break
            left = deadline - self._clock()
            if left <= 0:
                return self._done(
                    TIMEOUT, started, EligibilityRead(False, reason=TIMEOUT)
                )
            await asyncio.sleep(min(self._poll_s, left))
        if not row.readable:
            return self._done(
                "unreadable",
                started,
                EligibilityRead(False, reason=str(row.reason or "unreadable")),
            )
        modules = _modules(row.modules)
        if modules is None or not row.namespace:
            return self._done(
                "unreadable", started, EligibilityRead(False, reason="malformed")
            )
        return self._done(
            "readable",
            started,
            EligibilityRead(True, namespace=str(row.namespace), modules=modules),
        )
