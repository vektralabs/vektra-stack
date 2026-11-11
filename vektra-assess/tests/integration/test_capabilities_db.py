"""Capability matrix and readiness against a real database (plan assess-moodle-ws group D)."""

from __future__ import annotations

import uuid

import pytest
from sqlalchemy.exc import DBAPIError

from vektra_assess.capabilities import (
    CLOSE_TIME_FLAGS,
    FLAGS,
    NotReady,
    Ready,
    capability,
    observe_release,
    site_readiness,
)
from vektra_shared.lms import Readable, Unreadable, UnreadableReason

pytestmark = pytest.mark.integration

REL = "5.2.4+"


async def make_site(pg, release: str | None = REL) -> uuid.UUID:
    site = uuid.uuid4()
    await pg.run(
        "super",
        "INSERT INTO assess_sites (id, moodle_site_uuid, base_url, last_seen_release) "
        "VALUES (:i, :u, 'https://m', :r)",
        {"i": site, "u": str(site), "r": release},
    )
    return site


async def put_flag(pg, site, flag, state="readable", release=REL, age_h=1, reason=None):
    await pg.run(
        "worker",
        "INSERT INTO assess_site_capabilities (site_id, flag, state, release, reason_code, "
        "checked_at) VALUES (:s, :f, :st, :r, :c, now() - make_interval(hours => :h)) "
        "ON CONFLICT (site_id, flag) DO UPDATE SET state=:st, release=:r, reason_code=:c, "
        "checked_at=now() - make_interval(hours => :h)",
        {"s": site, "f": flag, "st": state, "r": release, "c": reason, "h": age_h},
    )


async def put_report(pg, site, items, status="done", release=REL):
    await pg.run(
        "worker",
        "INSERT INTO assess_setup_check_reports (site_id, status, moodle_release, items, "
        "finished_at) VALUES (:s, :st, :r, CAST(:i AS jsonb), now())",
        {"s": site, "st": status, "r": release, "i": __import__("json").dumps(items)},
    )


async def api_conn_call(pg, fn, *args, **kw):
    async with pg.conn("api") as c:
        return await fn(c, *args, **kw)


OK = [{"name": "token_works", "result": "OK"}]


async def test_capability_reads_as_the_api_role_with_every_fail_closed_rule(pg):
    site = await make_site(pg)
    assert (
        await api_conn_call(pg, capability, site, "quiz_overrides")
    ).reason is UnreadableReason.NOT_PROBED
    await put_flag(pg, site, "quiz_overrides")
    assert isinstance(
        await api_conn_call(pg, capability, site, "quiz_overrides"), Readable
    )
    await put_flag(pg, site, "quiz_overrides", age_h=27)
    assert (
        await api_conn_call(pg, capability, site, "quiz_overrides")
    ).detail_code == "stale"  # type: ignore[union-attr]
    await put_flag(pg, site, "quiz_overrides", release="5.2.3")
    assert (
        await api_conn_call(pg, capability, site, "quiz_overrides")
    ).reason is UnreadableReason.RELEASE_CHANGED  # type: ignore[union-attr]
    await put_flag(pg, site, "quiz_overrides", state="partial")
    assert isinstance(
        await api_conn_call(pg, capability, site, "quiz_overrides"), Unreadable
    )
    with pytest.raises(ValueError):
        await api_conn_call(pg, capability, site, "not_a_flag")


async def test_the_flag_column_only_accepts_the_closed_list(pg):
    site = await make_site(pg)
    with pytest.raises(DBAPIError, match="ck_assess_site_capabilities_flag"):
        await pg.run(
            "worker",
            "INSERT INTO assess_site_capabilities (site_id, flag, state) "
            "VALUES (:s, 'typo_flag', 'readable')",
            {"s": site},
        )


async def test_readiness_transitions(pg):
    site = await make_site(pg)
    assert await api_conn_call(pg, site_readiness, site) == NotReady(
        ("no_report_for_current_release",)
    )
    await put_report(pg, site, OK)
    missing = await api_conn_call(pg, site_readiness, site)
    assert missing == NotReady(tuple(f"capability:{f}" for f in CLOSE_TIME_FLAGS))
    for f in CLOSE_TIME_FLAGS:
        await put_flag(pg, site, f)
    assert await api_conn_call(pg, site_readiness, site) == Ready()
    # a report whose item is MISSING blocks, even with every flag fresh
    await put_report(pg, site, [{"name": "token_works", "result": "MISSING"}])
    assert await api_conn_call(pg, site_readiness, site) == NotReady(("token_works",))
    # a running or failed report is not evidence
    await put_report(pg, site, OK, status="running")
    assert await api_conn_call(pg, site_readiness, site) == NotReady(("token_works",))
    # a report of another release is not evidence for the current one
    other = await make_site(pg, release="5.2.5")
    await put_report(pg, other, OK, release=REL)
    assert (await api_conn_call(pg, site_readiness, other)) == NotReady(
        ("no_report_for_current_release",)
    )
    # a disabled site is never ready
    await pg.run(
        "super",
        "UPDATE assess_sites SET disabled_at = now() WHERE id = :s",
        {"s": site},
    )
    assert await api_conn_call(pg, site_readiness, site) == NotReady(
        ("site_unavailable",)
    )


async def test_release_change_flips_every_row_in_one_transaction(pg):
    site = await make_site(pg, release=REL)
    for f in FLAGS:
        await put_flag(pg, site, f)
    async with pg.conn("worker") as c:
        first = await observe_release(c, site, REL)
        assert (first.changed, first.flipped) == (
            False,
            0,
        )  # same release: nothing happens
        await c.commit()
    unchanged = await pg.run(
        "super",
        "SELECT count(*) FROM assess_site_capabilities WHERE site_id=:s "
        "AND state='readable'",
        {"s": site},
    )
    assert unchanged[0][0] == len(FLAGS)

    async with pg.conn("worker") as c:
        obs = await observe_release(c, site, "5.3.0")
        assert (obs.changed, obs.flipped) == (True, len(FLAGS))
        await c.commit()
    counts = (
        await pg.run(
            "super",
            "SELECT count(*) FILTER (WHERE state='unreadable' AND reason_code='release_changed'), count(*) "
            "FROM assess_site_capabilities WHERE site_id=:s",
            {"s": site},
        )
    )[0]
    assert tuple(counts) == (len(FLAGS), len(FLAGS))  # counted, not assumed
    assert (
        await pg.run(
            "super",
            "SELECT last_seen_release FROM assess_sites WHERE id=:s",
            {"s": site},
        )
    )[0][0] == "5.3.0"
    assert (
        await pg.run(
            "super",
            "SELECT count(*) FROM assess_jobs WHERE dedupe_key=:k AND kind='setup_check'",
            {"k": f"setup_check:{site}"},
        )
    )[0][0] == 1
    audit = (
        await pg.run(
            "super",
            "SELECT count(*), max(data->>'rows_invalidated') FROM assess_audit_events "
            "WHERE site_id=:s AND event='assess_site_release_changed'",
            {"s": site},
        )
    )[0]
    assert (audit[0], audit[1]) == (1, str(len(FLAGS)))
    # starts are blocked meanwhile
    assert isinstance(await api_conn_call(pg, site_readiness, site), NotReady)
    cap = await api_conn_call(pg, capability, site, "quiz_overrides")
    assert (
        isinstance(cap, Unreadable) and cap.reason is UnreadableReason.RELEASE_CHANGED
    )

    # a second change before the job ran does not queue a second setup check
    async with pg.conn("worker") as c:
        await observe_release(c, site, "5.3.1")
        await c.commit()
    assert (
        await pg.run(
            "super",
            "SELECT count(*) FROM assess_jobs WHERE dedupe_key=:k",
            {"k": f"setup_check:{site}"},
        )
    )[0][0] == 1


async def test_first_sight_records_the_release_without_invalidating(pg):
    site = await make_site(pg, release=None)
    await put_flag(pg, site, "quiz_overrides", release=None)
    async with pg.conn("worker") as c:
        obs = await observe_release(c, site, REL)
        await c.commit()
    assert (obs.changed, obs.flipped) == (False, 0)
    assert (
        await pg.run(
            "super",
            "SELECT last_seen_release FROM assess_sites WHERE id=:s",
            {"s": site},
        )
    )[0][0] == REL
    with pytest.raises(LookupError):
        async with pg.conn("worker") as c:
            await observe_release(c, uuid.uuid4(), REL)


async def test_api_cannot_write_the_matrix_or_read_the_probe_manifest(pg):
    site = await make_site(pg)
    for sql in (
        "INSERT INTO assess_site_capabilities (site_id, flag, state) VALUES (:s, 'quiz_grace', 'readable')",
        "UPDATE assess_sites SET last_seen_release = 'x' WHERE id = :s",
        "SELECT probe_manifest FROM assess_sites",
    ):
        with pytest.raises(DBAPIError) as exc:
            await pg.run("api", sql, {"s": site} if ":s" in sql else None)
        assert exc.value.orig.sqlstate == "42501", sql
    # but it can queue a setup check and read it back, content-free
    rid = (
        await pg.run(
            "api",
            "INSERT INTO assess_setup_check_reports (site_id) VALUES (:s) RETURNING id",
            {"s": site},
        )
    )[0][0]
    row = (
        await pg.run(
            "api",
            "SELECT status, items FROM assess_setup_check_reports WHERE id=:i",
            {"i": rid},
        )
    )[0]
    assert (row[0], row[1]) == ("queued", [])
