"""reconcile: a failing unlock must not replace the result (plan assess-job-loop review)."""

from __future__ import annotations

from contextlib import asynccontextmanager
from typing import Any

import pytest

from vektra_assess.worker import reconcile as rec
from vektra_assess.worker.listener import ControlConnectionError


class Locks:
    async def try_lock(self, key: int) -> bool:
        return True

    async def unlock(self, key: int) -> None:
        raise ControlConnectionError("dropped just before unlock")


@asynccontextmanager
async def _acquire():  # type: ignore[no-untyped-def]
    yield object()


async def _requeue(conn: Any) -> int:
    return 2


async def test_unlock_failure_keeps_the_successful_result(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    async def kill(conn: Any, audit: Any) -> tuple[int, int]:
        return 1, 0

    monkeypatch.setattr(rec.sql, "requeue_expired", _requeue)
    monkeypatch.setattr(rec, "_audit_then_kill_exhausted", kill)
    report = await rec.reconcile(Locks(), _acquire, audit=None)  # type: ignore[arg-type]
    assert (report.requeued, report.marked_dead) == (2, 1)


async def test_unlock_failure_does_not_hide_the_real_error(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    async def boom(conn: Any, audit: Any) -> tuple[int, int]:
        raise ValueError("real cause")

    monkeypatch.setattr(rec.sql, "requeue_expired", _requeue)
    monkeypatch.setattr(rec, "_audit_then_kill_exhausted", boom)
    with pytest.raises(ValueError, match="real cause"):
        await rec.reconcile(Locks(), _acquire, audit=None)  # type: ignore[arg-type]
