"""SECURITY DEFINER access boundary (plan assess-data-crypto 2.3, 2.4, 2.5), as real roles."""

from __future__ import annotations

import asyncio

import pytest
from sqlalchemy.exc import DBAPIError

from .conftest import GENESIS, make_session

pytestmark = pytest.mark.integration

MATERIAL = "SELECT * FROM get_session_material(:s, :e)"
APPEND = (
    "SELECT assess_append_turn(:s, :e, :seq, 'student', 'normal', '\\x04', :h, :prev)"
)


async def _turns(pg, sid) -> int:
    return (
        await pg.run(
            "super", "SELECT count(*) FROM assess_turns WHERE session_id=:s", {"s": sid}
        )
    )[0][0]


async def test_material_only_for_live_session_with_matching_epoch(pg):
    live = await make_session(pg, "ACTIVE")
    rows = await pg.run("api", MATERIAL, {"s": live, "e": 1})
    assert (
        len(rows) == 1 and rows[0].turn_nos == [1] and rows[0].snapshot_hash == GENESIS
    )
    assert await pg.run("api", MATERIAL, {"s": live, "e": 2}) == []  # wrong epoch
    assert (
        len(
            await pg.run(
                "api", MATERIAL, {"s": await make_session(pg, "OUTAGE"), "e": 1}
            )
        )
        == 1
    )
    for state in ("CLOSED", "STARTING"):
        sid = await make_session(pg, state)
        assert await pg.run("api", MATERIAL, {"s": sid, "e": 1}) == [], state


async def test_append_turn_happy_path_and_chain(pg):
    sid = await make_session(pg)
    n = (
        await pg.run(
            "api", APPEND, {"s": sid, "e": 1, "seq": 0, "h": "h2", "prev": "h1"}
        )
    )[0][0]
    assert n == 2
    seq = (
        await pg.run("super", "SELECT seq FROM assess_sessions WHERE id=:s", {"s": sid})
    )[0][0]
    assert seq == 1
    n = (
        await pg.run(
            "api", APPEND, {"s": sid, "e": 1, "seq": 1, "h": "h3", "prev": "h2"}
        )
    )[0][0]
    assert n == 3


async def test_append_turn_refuses_stale_seq_wrong_hash_wrong_epoch_and_non_active(pg):
    sid = await make_session(pg)
    before = await _turns(pg, sid)
    cases = [
        {"s": sid, "e": 1, "seq": 5, "h": "x", "prev": "h1"},  # stale/wrong seq
        {"s": sid, "e": 1, "seq": 0, "h": "x", "prev": "bad"},  # broken hash chain
        {"s": sid, "e": 9, "seq": 0, "h": "x", "prev": "h1"},  # wrong epoch
    ]
    for p in cases:
        assert (await pg.run("api", APPEND, p))[0][0] is None, p
    outage = await make_session(pg, "OUTAGE")  # append needs ACTIVE
    assert (
        await pg.run(
            "api", APPEND, {"s": outage, "e": 1, "seq": 0, "h": "x", "prev": "h1"}
        )
    )[0][0] is None
    assert await _turns(pg, sid) == before
    opening = (
        "SELECT assess_append_turn(:s, 1, 0, 'bot', 'opening', '\\x04', 'x', 'h1')"
    )
    assert (await pg.run("api", opening, {"s": sid}))[0][
        0
    ] is None  # API cannot write openings


async def test_concurrent_appends_with_same_seq_add_exactly_one_turn(pg):
    sid = await make_session(pg)
    params = {"s": sid, "e": 1, "seq": 0, "prev": "h1"}

    async def one(tag):
        return (await pg.run("api", APPEND, {**params, "h": f"h-{tag}"}))[0][0]

    results = await asyncio.gather(*[one(i) for i in range(6)])
    assert sorted(r for r in results if r is not None) == [2]
    assert await _turns(pg, sid) == 2


async def test_close_function_is_idempotent_and_enqueues_one_job(pg):
    sid = await make_session(pg)
    close = "SELECT assess_close_session(:s, :e, :r)"
    assert (await pg.run("api", close, {"s": sid, "e": 2, "r": "finished"}))[0][
        0
    ] is False
    assert (await pg.run("api", close, {"s": sid, "e": 1, "r": "bogus"}))[0][0] is False
    assert (await pg.run("api", close, {"s": sid, "e": 1, "r": "finished"}))[0][
        0
    ] is True
    assert (await pg.run("api", close, {"s": sid, "e": 1, "r": "finished"}))[0][
        0
    ] is True
    jobs = await pg.run(
        "super",
        "SELECT count(*) FROM assess_jobs WHERE dedupe_key=:k",
        {"k": f"transcript_generate:{sid}"},
    )
    assert jobs[0][0] == 1
    hand = await pg.run(
        "super",
        "SELECT count(*) FROM assess_session_handoff WHERE session_id=:s",
        {"s": sid},
    )
    assert hand[0][0] == 0


# --- 2.4 search_path hijack ----------------------------------------------------------


async def test_temp_objects_cannot_shadow_what_the_functions_read(pg):
    sid = await make_session(pg)
    async with pg.conn("api") as c:
        from sqlalchemy import text

        await c.execute(
            text(
                "CREATE TEMP TABLE assess_sessions (id uuid, state text, "
                "epoch int, seq int, site_id uuid)"
            )
        )
        await c.execute(
            text(
                "CREATE TEMP TABLE assess_session_handoff (session_id uuid, "
                "epoch int, sealed_dek bytea, handoff_kid text)"
            )
        )
        await c.execute(text("SET search_path = pg_temp, public"))
        rows = (await c.execute(text(MATERIAL), {"s": sid, "e": 1})).fetchall()
        assert (
            len(rows) == 1 and bytes(rows[0].sealed_dek) == b"\x03"
        )  # the REAL hand-off row
        n = (
            await c.execute(
                text(APPEND), {"s": sid, "e": 1, "seq": 0, "h": "hz", "prev": "h1"}
            )
        ).scalar()
        assert n == 2
        await c.rollback()


# --- 2.5 hand-off invariants ----------------------------------------------------------


async def test_handoff_row_for_non_live_session_is_unrepresentable(pg):
    starting = await make_session(pg, "STARTING")
    closed = await make_session(pg, "CLOSED")
    ins = (
        "INSERT INTO assess_session_handoff (session_id, session_state, epoch, "
        "sealed_dek, handoff_kid) VALUES (:s, :st, 1, '\\x00', 'k')"
    )
    for sid, st in (
        (starting, "STARTING"),
        (closed, "CLOSED"),
        (starting, "ACTIVE"),
        (closed, "ACTIVE"),
    ):
        with pytest.raises(DBAPIError):
            await pg.run("super", ins, {"s": sid, "st": st})


async def test_closing_a_session_removes_the_handoff_row_without_explicit_delete(pg):
    sid = await make_session(pg)
    await pg.run(
        "super",
        "UPDATE assess_sessions SET state='CLOSED', close_reason='timed_out' "
        "WHERE id=:s",
        {"s": sid},
    )
    assert (
        await pg.run(
            "super",
            "SELECT count(*) FROM assess_session_handoff WHERE session_id=:s",
            {"s": sid},
        )
    )[0][0] == 0


async def test_closed_session_cannot_be_revived(pg):
    sid = await make_session(pg)
    await pg.run(
        "super",
        "UPDATE assess_sessions SET state='CLOSED', close_reason='finished' "
        "WHERE id=:s",
        {"s": sid},
    )
    with pytest.raises(DBAPIError, match="cannot be reopened"):
        await pg.run(
            "super",
            "UPDATE assess_sessions SET state='ACTIVE', close_reason=NULL WHERE id=:s",
            {"s": sid},
        )
