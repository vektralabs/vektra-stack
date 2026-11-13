"""A reset grant is consumed by the session it unlocks (REQ-087).

``assess_reset_grants.consumed_at`` was never written: eligibility counts grants and was right,
but the subject export (``GET /privacy/subjects/{userid}/activities``) showed every reset as
unconsumed, even after the next session had started. The grant written after session N (its
``session_no`` is N) is consumed when session N+1 COMMITS, in the pre-flight's commit
transaction, at that session's ``committed_at``. A rolled-back Start consumes nothing.
"""

from __future__ import annotations

import uuid
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


async def _grants(pg, site: uuid.UUID) -> list[tuple[int, Any]]:  # type: ignore[no-untyped-def]
    rows = await pg.run(
        "super",
        "SELECT session_no, consumed_at FROM assess_reset_grants "
        "WHERE site_id = :s AND cmid = :c AND student_userid = :u ORDER BY session_no",
        {"s": site, "c": CMID, "u": STU},
    )
    return [(int(r[0]), r[1]) for r in rows]


async def _committed_at(pg, sid: uuid.UUID) -> Any:  # type: ignore[no-untyped-def]
    rows = await pg.run(
        "super", "SELECT committed_at FROM assess_sessions WHERE id = :s", {"s": sid}
    )
    return rows[0][0]


async def test_the_next_session_commit_consumes_the_grant(pg, worker_engine) -> None:  # type: ignore[no-untyped-def]
    await _reset(pg)
    await activate(pg)
    async with world(pg) as w:
        await configure_activity(pg, w.site, CMID)
        first = await _commit_session(w, worker_engine)
        await _close(pg, first)
        await _grant(pg, w.site, 1)
        assert await _grants(pg, w.site) == [(1, None)]
        second = await _commit_session(w, worker_engine)
        at = await _committed_at(pg, second)
        assert at is not None
        assert await _grants(pg, w.site) == [(1, at)]
        # a second run of the same pre-flight job is a stale no-op: the time does not move
        handler = make_preflight_handler(
            deps(w.settings, w.handoff_public, start_source(worker_engine))
        )
        await handler(job_context(worker_engine, second))
        assert await _grants(pg, w.site) == [(1, at)]


async def test_a_rolled_back_start_leaves_the_grant_unconsumed(
    pg, worker_engine
) -> None:  # type: ignore[no-untyped-def]
    await _reset(pg)
    await activate(pg)
    async with world(pg) as w:
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
        assert await _grants(pg, w.site) == [(1, None)]
        # the grant still unlocks the next Start, which consumes it
        second = await _commit_session(w, worker_engine)
        assert await _grants(pg, w.site) == [(1, await _committed_at(pg, second))]


async def test_two_resets_in_a_row_consume_one_each(pg, worker_engine) -> None:  # type: ignore[no-untyped-def]
    await _reset(pg)
    await activate(pg)
    async with world(pg) as w:
        await configure_activity(pg, w.site, CMID)
        first = await _commit_session(w, worker_engine)
        await _close(pg, first)
        await _grant(pg, w.site, 1)
        second = await _commit_session(w, worker_engine)
        await _close(pg, second)
        await _grant(pg, w.site, 2)
        assert await _grants(pg, w.site) == [
            (1, await _committed_at(pg, second)),
            (2, None),
        ]
        third = await _commit_session(w, worker_engine)
        assert await _grants(pg, w.site) == [
            (1, await _committed_at(pg, second)),
            (2, await _committed_at(pg, third)),
        ]


async def test_the_a0005_backfill_applies_the_same_rule(pg, worker_engine) -> None:  # type: ignore[no-untyped-def]
    """Rows written before the fix: the revision's statement sets each grant whose next
    session committed to that session's committed_at, leaves a grant with no committed next
    session NULL, and never moves a value already set."""
    import importlib

    backfill = importlib.import_module(
        "vektra_assess.migrations.versions.a0005_assess_reset_grant_consumed"
    ).BACKFILL
    await _reset(pg)
    await activate(pg)
    async with world(pg) as w:
        await configure_activity(pg, w.site, CMID)
        first = await _commit_session(w, worker_engine)
        await _close(pg, first)
        await _grant(pg, w.site, 1)
        second = await _commit_session(w, worker_engine)
        await _close(pg, second)
        await _grant(pg, w.site, 2)  # no session #3: stays NULL
        # the state before the fix: nothing consumed
        await pg.run(
            "super",
            "UPDATE assess_reset_grants SET consumed_at = NULL WHERE site_id = :s",
            {"s": w.site},
        )
        await pg.run("super", backfill)
        want = [(1, await _committed_at(pg, second)), (2, None)]
        assert await _grants(pg, w.site) == want
        # idempotent, and a value already set is never moved
        await pg.run(
            "super",
            "UPDATE assess_reset_grants SET consumed_at = '2000-01-01Z' "
            "WHERE site_id = :s AND session_no = 1",
            {"s": w.site},
        )
        await pg.run("super", backfill)
        (one, two) = await _grants(pg, w.site)
        assert one[1].year == 2000 and two == (2, None)


async def test_the_export_shows_the_consumption(pg, worker_engine) -> None:  # type: ignore[no-untyped-def]
    await _reset(pg)
    await activate(pg)
    async with world(pg) as w:
        await _privacy_key(w, "assess:privacy-export", "export")
        await configure_activity(pg, w.site, CMID)
        first = await _commit_session(w, worker_engine)
        await _close(pg, first)
        await _grant(pg, w.site, 1)
        second = await _commit_session(w, worker_engine)
        r = await _get(w, STU)
        assert r.status_code == 200, r.text
        (item,) = [i for i in r.json()["items"] if i["cmid"] == CMID]
        (g,) = item["resets"]
        assert g["after_session_no"] == 1
        assert g["consumed_at"] is not None
        committed = await _committed_at(pg, second)
        from datetime import datetime

        assert datetime.fromisoformat(g["consumed_at"]) == committed
