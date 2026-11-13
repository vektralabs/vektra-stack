"""When a reset grant was consumed, as the subject export shows it (REQ-087).

No code ever wrote ``assess_reset_grants.consumed_at``, so the export
(``GET /privacy/subjects/{userid}/activities``) showed every reset as unconsumed, even after the
next session had started. The export now derives it (``privacy.subject.reset_consumed_at_sql``):
the grant written after session N (its ``session_no`` is N) is consumed by session N+1, at that
session's ``committed_at``; NULL while none committed. A rolled-back Start consumes nothing.
Nothing is written at commit: the pre-flight's T guard stays the last statement before COMMIT.
"""

from __future__ import annotations

import uuid
from datetime import datetime
from typing import Any

import pytest
from sqlalchemy.ext.asyncio import create_async_engine
from support.preflight import FakePartB, activate, deps, job_context
from support.world import configure_activity, world

from integration.test_privacy_retention import _privacy_key
from integration.test_privacy_subject_activities import _get
from integration.test_session_preflight import CMID, STU, _reset, _start, start_source
from vektra_assess.llm.types import PartBRejectedError
from vektra_assess.worker.preflight import make_preflight_handler

pytestmark = pytest.mark.integration


@pytest.fixture
async def worker_engine(pg):  # type: ignore[no-untyped-def]
    engine = create_async_engine(pg.url("worker"))
    yield engine
    await engine.dispose()


async def _commit_session(w, worker_engine, runner: Any = None) -> uuid.UUID:  # type: ignore[no-untyped-def]
    sid = await _start(w)
    handler = make_preflight_handler(
        deps(w.settings, w.handoff_public, start_source(worker_engine), runner)
    )
    await handler(job_context(worker_engine, sid))
    return sid


async def _close(pg, sid: uuid.UUID) -> None:  # type: ignore[no-untyped-def]
    await pg.run(
        "super",
        "UPDATE assess_sessions SET state = 'CLOSED', close_reason = 'finished', "
        "closed_at = now() WHERE id = :s",
        {"s": sid},
    )


async def _grant(pg, site: uuid.UUID, after_session_no: int) -> None:  # type: ignore[no-untyped-def]
    """The row the reset route writes (the reason is opaque here)."""
    await pg.run(
        "super",
        "INSERT INTO assess_reset_grants (site_id, cmid, student_userid, granted_by, "
        "session_no, reason_ct, reason_kid) VALUES (:s, :c, :u, 'grader:1', :n, :ct, 'k1')",
        {"s": site, "c": CMID, "u": STU, "n": after_session_no, "ct": b"\x00" * 16},
    )


async def _committed_at(pg, sid: uuid.UUID) -> datetime:  # type: ignore[no-untyped-def]
    rows = await pg.run(
        "super", "SELECT committed_at FROM assess_sessions WHERE id = :s", {"s": sid}
    )
    assert rows[0][0] is not None
    return rows[0][0]


async def _exported(w) -> list[tuple[int, datetime | None]]:  # type: ignore[no-untyped-def]
    """The export's resets of the activity: (after_session_no, consumed_at)."""
    r = await _get(w, STU)
    assert r.status_code == 200, r.text
    (item,) = [i for i in r.json()["items"] if i["cmid"] == CMID]
    return [
        (
            g["after_session_no"],
            None
            if g["consumed_at"] is None
            else datetime.fromisoformat(g["consumed_at"]),
        )
        for g in item["resets"]
    ]


async def _stored(pg, site: uuid.UUID) -> list[Any]:  # type: ignore[no-untyped-def]
    rows = await pg.run(
        "super",
        "SELECT consumed_at FROM assess_reset_grants WHERE site_id = :s",
        {"s": site},
    )
    return [r[0] for r in rows]


async def test_a_reset_then_the_next_commit_shows_it_consumed(
    pg, worker_engine
) -> None:  # type: ignore[no-untyped-def]
    await _reset(pg)
    await activate(pg)
    async with world(pg) as w:
        await _privacy_key(w, "assess:privacy-export", "export")
        await configure_activity(pg, w.site, CMID)
        first = await _commit_session(w, worker_engine)
        await _close(pg, first)
        await _grant(pg, w.site, 1)
        assert await _exported(w) == [(1, None)]
        second = await _commit_session(w, worker_engine)
        assert await _exported(w) == [(1, await _committed_at(pg, second))]
        # derived on read: the stored column is not written (and is not the source)
        assert await _stored(pg, w.site) == [None]


async def test_a_rolled_back_start_leaves_it_unconsumed(pg, worker_engine) -> None:  # type: ignore[no-untyped-def]
    await _reset(pg)
    await activate(pg)
    async with world(pg) as w:
        await _privacy_key(w, "assess:privacy-export", "export")
        await configure_activity(pg, w.site, CMID)
        first = await _commit_session(w, worker_engine)
        await _close(pg, first)
        await _grant(pg, w.site, 1)
        rejected = FakePartB(fail=PartBRejectedError("part_b_values"))
        rolled = await _commit_session(w, worker_engine, rejected)
        rows = await pg.run(
            "super", "SELECT count(*) FROM assess_sessions WHERE id = :s", {"s": rolled}
        )
        assert rows[0][0] == 0  # rolled back
        assert await _exported(w) == [(1, None)]
        # the grant still unlocks the next Start, which consumes it
        second = await _commit_session(w, worker_engine)
        assert await _exported(w) == [(1, await _committed_at(pg, second))]


async def test_two_resets_in_a_row_map_to_their_own_sessions(pg, worker_engine) -> None:  # type: ignore[no-untyped-def]
    await _reset(pg)
    await activate(pg)
    async with world(pg) as w:
        await _privacy_key(w, "assess:privacy-export", "export")
        await configure_activity(pg, w.site, CMID)
        first = await _commit_session(w, worker_engine)
        await _close(pg, first)
        await _grant(pg, w.site, 1)
        second = await _commit_session(w, worker_engine)
        await _close(pg, second)
        await _grant(pg, w.site, 2)
        at2 = await _committed_at(pg, second)
        assert await _exported(w) == [(1, at2), (2, None)]
        third = await _commit_session(w, worker_engine)
        assert await _exported(w) == [(1, at2), (2, await _committed_at(pg, third))]
