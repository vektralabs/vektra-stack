"""M1.10: the seeding helper produces each state through the real paths (row counts)."""

from __future__ import annotations

import pytest
from sqlalchemy.ext.asyncio import create_async_engine
from support.sessions import seed_session
from support.world import world

from integration.test_session_preflight import _reset

pytestmark = pytest.mark.integration

EXPECTED = {
    # state: (session rows open, snapshots, keys, turns, hand-off rows, transcript jobs)
    "ELIGIBLE": (0, 0, 0, 0, 0, 0),
    "STARTING": (1, 0, 0, 0, 0, 0),
    "ACTIVE": (1, 1, 1, 2, 1, 0),
    "OUTAGE": (1, 1, 1, 2, 1, 0),
    "CLOSED": (0, 1, 1, 2, 0, 1),
}


@pytest.mark.parametrize("state", list(EXPECTED))
async def test_seeded_states(pg, state: str) -> None:  # type: ignore[no-untyped-def]
    await _reset(pg)
    await pg.run("super", "DELETE FROM assess_jobs")
    worker = create_async_engine(pg.url("worker"))
    try:
        async with world(pg) as w:
            s = await seed_session(w, worker, state)  # type: ignore[arg-type]
            q = {"s": s.session_id}
            open_rows = (
                await pg.run(
                    "super",
                    "SELECT count(*) FROM assess_sessions WHERE student_userid = :u "
                    "AND cmid = :c AND site_id = :site AND state <> 'CLOSED'",
                    {"u": s.sub, "c": s.cmid, "site": w.site},
                )
            )[0][0]
            counts = [open_rows]
            for table in (
                "assess_snapshots",
                "assess_session_keys",
                "assess_turns",
                "assess_session_handoff",
            ):
                counts.append(
                    (
                        await pg.run(
                            "super",
                            f"SELECT count(*) FROM {table} WHERE session_id = :s",
                            q,
                        )
                    )[0][0]
                    if s.session_id
                    else 0
                )
            counts.append(
                (
                    await pg.run(
                        "super",
                        "SELECT count(*) FROM assess_jobs WHERE kind = 'transcript_generate' "
                        "AND args->>'session_id' = :s",
                        {"s": str(s.session_id)},
                    )
                )[0][0]
            )
            assert tuple(counts) == EXPECTED[state], (state, counts)
            if state == "OUTAGE":
                st = await pg.run(
                    "super",
                    "SELECT h.session_state FROM assess_session_handoff h WHERE session_id = :s",
                    q,
                )
                assert (
                    st[0][0] == "OUTAGE"
                )  # the hand-off row followed the state (FK cascade)
    finally:
        await worker.dispose()
