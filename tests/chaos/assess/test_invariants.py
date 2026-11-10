"""The invariant checker can say no (plan assess-hardening-release, chaos consolidation).

A checker that never reports a violation would make every chaos scenario pass. Each invariant is
shown here to catch a violation planted directly in the rows, with the triggers and the partial
unique index that normally prevent it switched off for this throwaway database only. A clean
database passes, and the false-pass guard fails a scenario that created fewer sessions than it
declared.
"""

from __future__ import annotations

import uuid
from typing import Any

import pytest
from invariants import ALWAYS, assert_ok, check
from sqlalchemy import text
from sqlalchemy.ext.asyncio import create_async_engine

pytestmark = pytest.mark.integration

GENESIS = "0" * 64
REF = "a" * 64


async def plant(pg: Any, *sql: str, **params: Any) -> None:
    """Run ``sql`` as the superuser with triggers and FK checks off (replica role)."""
    engine = create_async_engine(pg.super_url)
    try:
        async with engine.begin() as conn:
            await conn.execute(text("SET LOCAL session_replication_role = replica"))
            for stmt in sql:
                await conn.execute(text(stmt), params)
    finally:
        await engine.dispose()


async def wipe(pg: Any) -> None:
    await plant(
        pg,
        "DELETE FROM assess_deliveries",
        "DELETE FROM assess_transcripts",
        "DELETE FROM assess_session_handoff",
        "DELETE FROM assess_turns",
        "DELETE FROM assess_snapshots",
        "DELETE FROM assess_session_keys",
        "DELETE FROM assess_purge_records",
        "DELETE FROM assess_sessions",
        "DELETE FROM assess_jobs",
        "DELETE FROM assess_sites",
    )


async def session(
    pg: Any,
    state: str,
    *,
    opening: bool,
    key: bool = True,
    student: int = 1,
    no: int = 1,
    **extra: Any,
) -> uuid.UUID:
    site = extra.get("site") or uuid.uuid4()
    sid = uuid.uuid4()
    stmts = [
        "INSERT INTO assess_sites (id, moodle_site_uuid, base_url) VALUES (:site, :site_u, "
        "'https://m.example') ON CONFLICT DO NOTHING",
        "INSERT INTO assess_sessions (id, site_id, cmid, student_userid, session_no, state, "
        "close_reason, epoch, starting_expires_at) VALUES (:sid, :site, 1, :student, :no, :state, "
        "CASE WHEN :state = 'CLOSED' THEN 'finished' END, 1, :exp)",
    ]
    if opening:
        stmts.append(
            "INSERT INTO assess_turns (session_id, turn_no, role, kind, ciphertext, turn_hash, "
            "prev_hash) VALUES (:sid, 1, 'bot', 'opening', '\\x02', 'h1', :g)"
        )
    if key:
        stmts.append(
            "INSERT INTO assess_session_keys (session_id, kek_wrapped, kek_kid, key_version) "
            "VALUES (:sid, '\\x01', 'k', 1)"
        )
    await plant(
        pg,
        *stmts,
        site=site,
        site_u=str(site),
        sid=sid,
        student=student,
        no=no,
        state=state,
        exp=extra.get("expires"),
        g=GENESIS,
    )
    return sid


async def report_for(pg: Any, name: str, **kw: Any) -> list[str]:
    r = await check(pg, f"negative:{name}", expect_sessions=0, only=[name], **kw)
    return r.violations[name]


async def test_a_clean_database_passes_every_invariant(pg) -> None:  # type: ignore[no-untyped-def]
    await wipe(pg)
    site = uuid.uuid4()
    await session(pg, "ACTIVE", opening=True, site=site)
    await session(pg, "CLOSED", opening=True, student=2, site=site)
    await session(pg, "STARTING", opening=False, key=False, student=3, site=site)
    report = await check(pg, "selftest:clean", expect_sessions=3, sites=[site])
    assert report.checked == list(ALWAYS)
    assert_ok(report)


async def test_fewer_sessions_than_declared_fails(pg) -> None:  # type: ignore[no-untyped-def]
    await wipe(pg)
    report = await check(pg, "selftest:empty", expect_sessions=1, sites=[uuid.uuid4()])
    assert not report.ok
    with pytest.raises(AssertionError, match="declared 1"):
        assert_ok(report)


async def test_rows_left_by_an_earlier_test_cannot_satisfy_the_declared_count(
    pg,
) -> None:  # type: ignore[no-untyped-def]
    """``expect_sessions`` counts the scenario's own sites only: three sessions an earlier test
    left on the module-scoped database do not stand in for the one this scenario never made."""
    await wipe(pg)
    earlier = uuid.uuid4()
    for student in (1, 2, 3):
        await session(pg, "CLOSED", opening=True, student=student, site=earlier)
    mine = uuid.uuid4()
    report = await check(pg, "selftest:scoped", expect_sessions=1, sites=[mine])
    assert report.sessions == 0 and not report.ok
    with pytest.raises(AssertionError, match="declared 1"):
        assert_ok(report)
    await session(pg, "ACTIVE", opening=True, site=mine)
    assert (await check(pg, "selftest:scoped", expect_sessions=1, sites=[mine])).ok


async def test_a_violation_on_an_earlier_site_does_not_fail_a_later_scenario(
    pg,
) -> None:  # type: ignore[no-untyped-def]
    """CodeRabbit nitpick on #195: the session-keyed checks are scoped to the scenario's sites
    too, so a test order change cannot make one scenario fail on another's rows. The same
    violation is still caught when its site is in scope."""
    await wipe(pg)
    earlier = uuid.uuid4()
    await session(
        pg, "CLOSED", opening=False, site=earlier
    )  # consumed without an opening
    mine = uuid.uuid4()
    await session(pg, "ACTIVE", opening=True, site=mine, student=2)
    assert (
        await check(pg, "selftest:scoped-clean", expect_sessions=1, sites=[mine])
    ).ok
    caught = await check(
        pg, "selftest:scoped-caught", expect_sessions=1, sites=[mine, earlier]
    )
    assert not caught.ok and caught.violations["consumed_iff_opening"]


async def test_a_declared_count_without_its_sites_is_refused(pg) -> None:  # type: ignore[no-untyped-def]
    with pytest.raises(ValueError, match="sites"):
        await check(pg, "selftest:unscoped", expect_sessions=1)


async def test_two_live_sessions_for_one_student_are_caught(pg) -> None:  # type: ignore[no-untyped-def]
    await wipe(pg)
    await plant(pg, "DROP INDEX uq_assess_sessions_one_open")
    site = uuid.uuid4()
    await session(pg, "ACTIVE", opening=True, site=site)
    await session(pg, "OUTAGE", opening=True, site=site, no=2)
    assert len(await report_for(pg, "one_live_session")) == 1


async def test_a_committed_session_without_its_opening_turn_is_caught(pg) -> None:  # type: ignore[no-untyped-def]
    await wipe(pg)
    sid = await session(pg, "ACTIVE", opening=False)
    assert await report_for(pg, "consumed_iff_opening") == [str(sid)]


async def test_a_starting_session_with_an_opening_turn_is_caught(pg) -> None:  # type: ignore[no-untyped-def]
    await wipe(pg)
    sid = await session(pg, "STARTING", opening=True, key=False)
    assert await report_for(pg, "consumed_iff_opening") == [str(sid)]


async def test_a_purged_session_is_exempt_from_the_content_invariants(pg) -> None:  # type: ignore[no-untyped-def]
    await wipe(pg)
    sid = await session(pg, "CLOSED", opening=False, key=False)
    await plant(
        pg,
        "INSERT INTO assess_purge_records (site_id, subject_ref, session_id, session_no, cmid, "
        "reason, counts) SELECT site_id, 'x', id, 1, 1, 'retention', '{}' FROM assess_sessions "
        "WHERE id = :sid",
        sid=sid,
    )
    assert await report_for(pg, "consumed_iff_opening") == []
    assert await report_for(pg, "key_present") == []


async def test_a_handoff_row_on_a_closed_session_is_caught(pg) -> None:  # type: ignore[no-untyped-def]
    await wipe(pg)
    sid = await session(pg, "CLOSED", opening=True)
    await plant(
        pg,
        "INSERT INTO assess_session_handoff (session_id, session_state, epoch, sealed_dek, "
        "handoff_kid) VALUES (:sid, 'ACTIVE', 1, '\\x03', 'k1')",
        sid=sid,
    )
    assert await report_for(pg, "handoff_only_live") == [str(sid)]


async def test_content_of_a_deleted_session_is_caught(pg) -> None:  # type: ignore[no-untyped-def]
    await wipe(pg)
    sid = await session(pg, "ACTIVE", opening=True)
    await plant(pg, "DELETE FROM assess_sessions WHERE id = :sid", sid=sid)
    got = await report_for(pg, "no_orphan_content")
    assert sorted(got) == sorted([f"key:{sid}", f"turn:{sid}"])


async def test_a_closed_session_without_a_transcript_is_caught_once_settled(pg) -> None:  # type: ignore[no-untyped-def]
    await wipe(pg)
    sid = await session(pg, "CLOSED", opening=True)
    assert await report_for(pg, "transcript_per_closed") == [str(sid)]


async def test_two_sent_rows_for_one_recipient_are_caught(pg) -> None:  # type: ignore[no-untyped-def]
    await wipe(pg)
    sid = await session(pg, "CLOSED", opening=True)
    tid = uuid.uuid4()
    await plant(
        pg,
        "INSERT INTO assess_transcripts (id, session_id, ciphertext, dek_wrapped, transcript_kid, "
        "content_hash, turns_digest, filename, byte_size) VALUES (:tid, :sid, '\\x01', '\\x01', "
        "'k', 'h', 'd', 'f.pdf', 1)",
        # the round/ref unique constraint normally makes this impossible: dropped here only
        "ALTER TABLE assess_deliveries DROP CONSTRAINT uq_assess_deliveries_round_ref",
        "INSERT INTO assess_deliveries (transcript_id, kind, round, recipient_kind, status, "
        "recipient_ref, message_id, sent_at) VALUES "
        "(:tid, 'transcript', 1, 'address', 'SENT', :ref, 'm1', now()), "
        "(:tid, 'transcript', 1, 'address', 'SENT', :ref, 'm2', now())",
        tid=tid,
        sid=sid,
        ref=REF,
    )
    assert await report_for(pg, "one_sent_per_recipient") == [f"{tid}:{REF}:1"]
    # the sink check: m1 delivered twice without an audited duplicate is a violation too
    r = await check(
        pg,
        "negative:sink",
        expect_sessions=0,
        only=[],
        sink_copies=lambda mid: 2 if mid == "m1" else 1,
    )
    assert r.violations["sink_copies"] == [f"{tid}:m1:2"]


async def test_a_duplicate_audit_excuses_only_its_own_delivery_row(pg) -> None:  # type: ignore[no-untyped-def]
    """Two recipients of one transcript: A was legitimately resent (its row has an audited
    ``assess_delivery_possible_duplicate``), B was double-sent with no audit. The audit of A
    must not excuse B."""
    await wipe(pg)
    sid = await session(pg, "CLOSED", opening=True)
    tid, a, b = uuid.uuid4(), uuid.uuid4(), uuid.uuid4()
    await plant(
        pg,
        "INSERT INTO assess_transcripts (id, session_id, ciphertext, dek_wrapped, transcript_kid, "
        "content_hash, turns_digest, filename, byte_size) VALUES (:tid, :sid, '\\x01', '\\x01', "
        "'k', 'h', 'd', 'f.pdf', 1)",
        "INSERT INTO assess_deliveries (id, transcript_id, kind, round, recipient_kind, status, "
        "recipient_ref, message_id, sent_at) VALUES "
        "(:a, :tid, 'transcript', 1, 'address', 'SENT', :ra, 'mA', now()), "
        "(:b, :tid, 'transcript', 1, 'address', 'SENT', :rb, 'mB', now())",
        "INSERT INTO assess_audit_events (session_id, event, actor_kind, data) VALUES "
        "(:sid, 'assess_delivery_possible_duplicate', 'worker', "
        "jsonb_build_object('delivery_id', CAST(:a_s AS text)))",
        tid=tid,
        sid=sid,
        a=a,
        a_s=str(a),
        b=b,
        ra="a" * 64,
        rb="b" * 64,
    )
    r = await check(
        pg,
        "negative:sink-per-row",
        expect_sessions=0,
        only=[],
        sink_copies=lambda _m: 2,
    )
    assert r.violations["sink_copies"] == [f"{tid}:mB:2"]


async def test_a_lease_left_past_its_expiry_is_caught(pg) -> None:  # type: ignore[no-untyped-def]
    await wipe(pg)
    await plant(
        pg,
        "INSERT INTO assess_jobs (kind, args, state, lease_until) VALUES ('purge', '{}', "
        "'leased', now() - interval '1 minute')",
    )
    assert len(await report_for(pg, "no_stale_lease")) == 1


async def test_a_starting_row_past_the_reaper_bound_is_caught(pg) -> None:  # type: ignore[no-untyped-def]
    await wipe(pg)
    from datetime import UTC, datetime, timedelta

    sid = await session(
        pg,
        "STARTING",
        opening=False,
        key=False,
        expires=datetime.now(UTC) - timedelta(minutes=5),
    )
    assert await report_for(pg, "no_stale_starting") == [str(sid)]


async def test_a_committed_session_without_its_key_is_caught(pg) -> None:  # type: ignore[no-untyped-def]
    await wipe(pg)
    sid = await session(pg, "ACTIVE", opening=True, key=False)
    assert await report_for(pg, "key_present") == [str(sid)]
