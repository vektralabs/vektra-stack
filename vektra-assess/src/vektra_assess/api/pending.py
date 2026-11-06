"""``PendingTurnDriver``: the API answers a pending student turn without a client (REQ-086, M4.3).

After an outage ends (the worker's probe moves the session back to ACTIVE) the student's last turn
is still unanswered and nobody may be connected. This background task, started with the API
server (``vektra_app.main`` startup, also when uvicorn's lifespan is off, BUG-025), polls every
``interval_s`` for ACTIVE sessions whose turn has been pending for at least ``min_age_s`` and that
no reply is answering, and answers them through the same ``TurnDriver`` code path (released
segments to the broker, bot turn through ``assess_append_turn``, a new outage moves the session
back to OUTAGE).

The claim is the live-reply advisory lock of ``TurnDriver`` (``REPLY_CLAIM_KEY_SQL``), taken with
``pg_try_advisory_lock`` on a dedicated connection and held for the whole reply: a reply already
running in any API process holds it, so the turn is skipped; and the worker's ``reconcile_reply``
never touches a claimed turn. ``min_age_s`` covers the short window between a turn's commit and
its own driver taking the claim. ``nudge()`` (``POST /retry``) makes the next poll happen now.
"""

from __future__ import annotations

import asyncio
import contextlib
from collections.abc import Callable
from dataclasses import dataclass, field
from typing import Any
from uuid import UUID

import structlog
from sqlalchemy import text

from vektra_assess.api.material import load_material
from vektra_assess.api.session_gate import load_row
from vektra_assess.api.turns import (
    bundle_of,
    cut_after_s,
    driver_for_app,
    history_of,
)
from vektra_assess.llm.types import StudentTurn
from vektra_assess.turn_markers import REPLY_CLAIM_KEY_SQL

log = structlog.get_logger(__name__)

_PENDING = text(
    "SELECT id FROM assess_sessions WHERE state = 'ACTIVE' "
    "AND pending_reply_since IS NOT NULL "
    "AND pending_reply_since <= now() - make_interval(secs => :age) "
    "AND deadline_at + make_interval(secs => reply_grace_s) > now() "
    "ORDER BY pending_reply_since LIMIT :n"
)
_TRY = text(f"SELECT pg_try_advisory_lock({REPLY_CLAIM_KEY_SQL})")
_UNLOCK = text(f"SELECT pg_advisory_unlock({REPLY_CLAIM_KEY_SQL})")


@dataclass
class PendingTurnDriver:
    app: Any
    interval_s: float = 2.0
    min_age_s: float = 3.0
    batch: int = 50
    _wake: asyncio.Event = field(default_factory=asyncio.Event)
    _task: asyncio.Task[None] | None = None

    # --- lifecycle --------------------------------------------------------------------------

    def start(self) -> None:
        if self._task is None:
            self._task = asyncio.create_task(self._loop(), name="assess-pending-turns")

    async def stop(self) -> None:
        task, self._task = self._task, None
        if task is not None:
            task.cancel()
            with contextlib.suppress(asyncio.CancelledError):
                await task

    def nudge(self) -> None:
        self._wake.set()

    async def _loop(self) -> None:
        while True:
            try:
                await self.run_once()
            except Exception as exc:  # a database blip never stops the loop
                log.warning("assess_pending_poll_failed", error_type=type(exc).__name__)
            with contextlib.suppress(TimeoutError):
                async with asyncio.timeout(self.interval_s):
                    await self._wake.wait()
            self._wake.clear()

    # --- one poll ---------------------------------------------------------------------------

    async def run_once(self) -> list[UUID]:
        """Start an answer for every unclaimed pending turn; returns the sessions taken."""
        if getattr(self.app.state, "assess_runner", None) is None:
            return []
        driver = driver_for_app(self.app)
        async with driver.engine.connect() as conn:
            rows = (
                await conn.execute(_PENDING, {"age": self.min_age_s, "n": self.batch})
            ).all()
        taken = []
        for (sid,) in rows:
            if sid in driver.tasks:
                continue  # this process is already answering it
            task = asyncio.create_task(self._answer(sid))
            driver.tasks[sid] = task
            task.add_done_callback(_forget(driver.tasks, sid))
            taken.append(sid)
        return taken

    async def _answer(self, sid: UUID) -> None:
        driver = driver_for_app(self.app)
        conn = await driver.engine.connect()
        try:
            claimed = bool((await conn.execute(_TRY, {"sid": str(sid)})).scalar())
            await conn.commit()
            if not claimed:
                return  # a live reply (here or in another API process) owns it
            try:
                prepared = await self._prepare(sid)
                if prepared is not None:
                    epoch, bundle, turn, cut = prepared
                    log.info("assess_pending_turn_answered_server_side")
                    await driver._reply(sid, epoch, bundle, turn, cut)
            finally:
                with contextlib.suppress(Exception):
                    await conn.execute(_UNLOCK, {"sid": str(sid)})
                    await conn.commit()
        except Exception as exc:
            log.warning("assess_pending_turn_failed", error_type=type(exc).__name__)
        finally:
            await conn.close()

    async def _prepare(
        self, sid: UUID
    ) -> tuple[int, Any, StudentTurn, float | None] | None:
        """Re-read under the row lock: still ACTIVE, still pending, last turn the student's."""
        driver = driver_for_app(self.app)
        async with driver.engine.begin() as conn:
            row = await load_row(conn, sid)
            if (
                row is None
                or row.state != "ACTIVE"
                or row.pending_reply_since is None
                or row.committed_at is None
            ):
                return None
            material = await load_material(
                conn, sid, row.epoch, driver.handoff_private, driver.cache
            )
            if not material.turns or material.turns[-1].role != "student":
                return None
            last = material.turns[-1]
            history = history_of(material)[:-1]
            turn = StudentTurn(
                text=material.text_of(last),
                turn_index=sum(1 for t in material.turns if t.role == "student"),
                history=history,
            )
            return row.epoch, bundle_of(material, row), turn, cut_after_s(row)


def _forget(
    tasks: dict[UUID, asyncio.Task[None]], sid: UUID
) -> Callable[[asyncio.Task[None]], None]:
    def done(task: asyncio.Task[None]) -> None:
        if tasks.get(sid) is task:
            tasks.pop(sid, None)

    return done


def pending_driver_of(app: Any) -> PendingTurnDriver | None:
    return getattr(app.state, "assess_pending_driver", None)
