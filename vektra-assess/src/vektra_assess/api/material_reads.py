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
from vektra_assess.context.materials import EligibleModule, lms_site_key
from vektra_assess.jobs import JobKind, enqueue_job
from vektra_assess.llm.types import EligibilityRead

log = structlog.get_logger(__name__)

TIMEOUT = "timeout"
ENQUEUE_FAILED = "enqueue_failed"
STOPPED = "stopped"  # the turn itself stopped (client gone, cut deadline) while waiting
#: the outcome codes ``observe`` receives
OUTCOMES = ("readable", "unreadable", TIMEOUT, ENQUEUE_FAILED, STOPPED)
#: the polls back off from the first to the last delay, doubling (review 8)
POLL_FIRST_S, POLL_MAX_S = 0.05, 0.5

_CLOCK = text("SELECT clock_timestamp()")
_SITE = text("SELECT base_url FROM assess_sites WHERE id = :s")
_ROW = text(
    "SELECT readable, reason, namespace, modules FROM assess_material_reads "
    "WHERE id = :rid AND session_id = :sid AND site_id = :site AND read_at >= :t0"
)
_DELETE = text("DELETE FROM assess_material_reads WHERE id = :rid AND site_id = :site")


async def _wait(stop: asyncio.Event | None, seconds: float) -> None:
    """Sleep ``seconds``, or less when ``stop`` is set meanwhile."""
    if stop is None:
        await asyncio.sleep(seconds)
        return
    waiter = asyncio.ensure_future(stop.wait())
    sleeper = asyncio.ensure_future(asyncio.sleep(seconds))
    try:
        await asyncio.wait({waiter, sleeper}, return_when=asyncio.FIRST_COMPLETED)
    finally:
        waiter.cancel()
        sleeper.cancel()


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
        observe: Callable[[str, float], None] | None = None,
        clock: Callable[[], float] = time.monotonic,
    ) -> None:
        #: an AsyncEngine, or a zero-argument callable returning one (resolved per read)
        self._engine = engine
        self._timeout_s = timeout_s
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

    async def __call__(
        self, session_id: UUID, site_id: UUID, stop: asyncio.Event | None = None
    ) -> EligibilityRead:
        """The eligibility of one turn; ``stop`` (the turn's own cancellation: the client went
        away or the reply's cut deadline passed) ends the wait at the next poll."""
        started = self._clock()
        rid = uuid4()
        engine = self._engine_now()
        site_key: str | None = None
        try:
            async with engine.begin() as conn:
                await bind_site(conn, site_id)
                base_url = (await conn.execute(_SITE, {"s": site_id})).scalar()
                site_key = lms_site_key(base_url)
                if site_key is None:
                    # no usable site key: nothing could be filtered on it, so nothing is read
                    return self._done(
                        "unreadable",
                        started,
                        EligibilityRead(False, reason="site_unknown"),
                    )
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
        delay = POLL_FIRST_S
        while True:
            if stop is not None and stop.is_set():
                return self._done(
                    STOPPED, started, EligibilityRead(False, reason=STOPPED)
                )
            try:
                async with engine.begin() as conn:
                    await bind_site(conn, site_id)
                    row = (
                        await conn.execute(
                            _ROW,
                            {"rid": rid, "sid": session_id, "site": site_id, "t0": t0},
                        )
                    ).first()
                    if row is not None:
                        await conn.execute(_DELETE, {"rid": rid, "site": site_id})
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
            await _wait(stop, min(delay, left))
            delay = min(delay * 2, POLL_MAX_S)
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
            EligibilityRead(
                True,
                namespace=str(row.namespace),
                modules=modules,
                lms_site=site_key,
            ),
        )
