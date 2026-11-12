"""The probe matrix is written in ONE transaction per check run, as the worker role."""

from __future__ import annotations

import uuid

import pytest

from vektra_assess.capabilities import FLAGS, observe_release
from vektra_assess.setup import persist_matrix
from vektra_shared.lms import CapabilityResult as FlagResult

pytestmark = pytest.mark.integration

REL = "5.2.4+ (Build: 20261030)"


async def make_site(pg, release=REL) -> uuid.UUID:
    site = uuid.uuid4()
    await pg.run(
        "super",
        "INSERT INTO assess_sites (id, moodle_site_uuid, base_url, last_seen_release) "
        "VALUES (:i, :u, 'https://m', :r)",
        {"i": site, "u": str(site), "r": release},
    )
    return site


def results(state="readable", reason=None) -> tuple[FlagResult, ...]:
    return tuple(FlagResult(f, state, reason, "a" * 64) for f in FLAGS)


async def rows(pg, site):
    return await pg.run(
        "super",
        "SELECT flag, state, release, reason_code, evidence_hash FROM assess_site_capabilities "
        "WHERE site_id = :s ORDER BY flag",
        {"s": site},
    )


async def test_a_run_writes_every_flag_in_one_transaction(pg):
    site = await make_site(pg)
    async with pg.conn("worker") as c:
        await observe_release(c, site, REL)
        await persist_matrix(c, site, REL, results())
        # nothing is visible to anyone else before the commit
        assert await rows(pg, site) == []
        await c.commit()
    got = await rows(pg, site)
    assert [r[0] for r in got] == sorted(FLAGS)
    assert {(r[1], r[2], r[3], r[4]) for r in got} == {
        ("readable", REL, None, "a" * 64)
    }


async def test_a_crash_mid_run_leaves_no_partial_matrix(pg):
    site = await make_site(pg)
    async with pg.conn("worker") as c:
        await persist_matrix(c, site, REL, results())
        await c.commit()
    before = await rows(pg, site)
    bad = (
        *results("unreadable", "timeout")[:8],
        FlagResult("not_a_flag", "readable", None, "b"),
    )
    with pytest.raises(Exception, match="ck_assess_site_capabilities_flag"):
        async with pg.conn("worker") as c:
            await persist_matrix(c, site, REL, bad)
            await c.commit()
    assert await rows(pg, site) == before  # the first 8 updates were rolled back


async def test_release_change_flips_and_a_reprobe_restores(pg):
    site = await make_site(pg)
    async with pg.conn("worker") as c:
        await persist_matrix(c, site, REL, results())
        await c.commit()
    async with pg.conn("worker") as c:
        obs = await observe_release(c, site, "5.3.0 (Build: 1)")
        await c.commit()
    assert (obs.changed, obs.flipped) == (True, len(FLAGS))
    flipped = await pg.run(
        "super",
        "SELECT count(*) FROM assess_site_capabilities WHERE site_id=:s "
        "AND state='unreadable' AND reason_code='release_changed'",
        {"s": site},
    )
    assert flipped[0][0] == len(FLAGS)
    async with pg.conn("worker") as c:
        await persist_matrix(c, site, "5.3.0 (Build: 1)", results())
        await c.commit()
    ok = await pg.run(
        "super",
        "SELECT count(*) FROM assess_site_capabilities WHERE site_id=:s "
        "AND state='readable' AND release='5.3.0 (Build: 1)' AND reason_code IS NULL",
        {"s": site},
    )
    assert ok[0][0] == len(FLAGS)
