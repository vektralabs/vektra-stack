"""Revision 0035 (plan 10b): delivery rows keyed by ``recipient_ref``, never by an address.

An ``address`` row carries a 64-hex ref and no user id (and only then); one row per (transcript,
round, ref); the resend function copies the ref into round n+1; the API role cannot read the ref.
"""

from __future__ import annotations

import hashlib

import pytest
from sqlalchemy.exc import DBAPIError, IntegrityError

from .conftest import _alembic
from .test_transcript_schema import _transcript

pytestmark = pytest.mark.integration


def ref(n: int) -> str:
    return hashlib.sha256(f"site|r{n}@uni.example".encode()).hexdigest()


async def _address(pg, tid, n, status="PENDING", round_=1):  # type: ignore[no-untyped-def]
    sent = "now()" if status == "SENT" else "NULL"
    await pg.run(
        "worker",
        "INSERT INTO assess_deliveries (transcript_id, round, recipient_kind, recipient_ref, "
        f"status, sent_at) VALUES (:t, :r, 'address', :ref, :st, {sent})",
        {"t": tid, "r": round_, "ref": ref(n), "st": status},
    )


async def test_an_address_row_has_a_ref_and_no_user_id(pg) -> None:  # type: ignore[no-untyped-def]
    tid = await _transcript(pg)
    await _address(pg, tid, 1)
    with pytest.raises(IntegrityError, match="uq_assess_deliveries_round_ref"):
        await _address(pg, tid, 1)
    bad = (
        # an address row without a ref
        ("'address'", "NULL", "NULL"),
        # an address row with a user id too
        ("'address'", "7", f"'{ref(2)}'"),
        # a grader row with a ref
        ("'grader'", "7", f"'{ref(3)}'"),
    )
    for kind, uid, r in bad:
        with pytest.raises(IntegrityError, match="ck_assess_deliveries_recipient"):
            await pg.run(
                "worker",
                "INSERT INTO assess_deliveries (transcript_id, recipient_kind, "
                f"recipient_userid, recipient_ref) VALUES (:t, {kind}, {uid}, {r})",
                {"t": tid},
            )
    # not an address: a ref is 64 lowercase hex, nothing else fits
    with pytest.raises(IntegrityError, match="ck_assess_deliveries_ref"):
        await pg.run(
            "worker",
            "INSERT INTO assess_deliveries (transcript_id, recipient_kind, recipient_ref) "
            "VALUES (:t, 'address', 'prof@uni.example')",
            {"t": tid},
        )


async def test_resend_copies_the_refs_of_the_last_round(pg) -> None:  # type: ignore[no-untyped-def]
    tid = await _transcript(pg)
    await _address(pg, tid, 1, "SENT")
    await _address(pg, tid, 2, "FAILED")
    await _address(pg, tid, 3, "REPLACED")
    got = await pg.run(
        "api", "SELECT assess_enqueue_resend(:t, 'grader:11', 0)", {"t": tid}
    )
    assert got[0][0] == 2
    rows = await pg.run(
        "super",
        "SELECT recipient_kind, recipient_ref, recipient_userid, status FROM assess_deliveries "
        "WHERE transcript_id = :t AND round = 2 ORDER BY recipient_ref",
        {"t": tid},
    )
    assert [tuple(r) for r in rows] == sorted(
        [("address", ref(1), None, "PENDING"), ("address", ref(2), None, "PENDING")],
        key=lambda r: r[1],
    )


async def test_the_api_reads_the_status_but_never_the_ref(pg) -> None:  # type: ignore[no-untyped-def]
    tid = await _transcript(pg)
    await _address(pg, tid, 1)
    rows = await pg.run(
        "api",
        "SELECT status, round, recipient_kind FROM assess_deliveries WHERE transcript_id = :t",
        {"t": tid},
    )
    assert [tuple(r) for r in rows] == [("PENDING", 1, "address")]
    for sql in (
        "SELECT recipient_ref FROM assess_deliveries",
        # CodeRabbit 4504362581: the Message-ID hashes the ref too
        "SELECT message_id FROM assess_deliveries",
        "SELECT * FROM assess_deliveries",
    ):
        with pytest.raises(DBAPIError, match="permission denied"):
            await pg.run("api", sql)


def test_round_trip_to_0034_and_back(pg) -> None:  # type: ignore[no-untyped-def]
    down = _alembic(pg.super_url, "downgrade", "0034")
    assert down.returncode == 0, down.stderr
    up = _alembic(pg.super_url, "upgrade", "head")
    assert up.returncode == 0, up.stderr


async def test_a_duplicate_job_is_skipped_without_an_error_or_a_savepoint(pg) -> None:  # type: ignore[no-untyped-def]
    """Review #8: the delivery tick re-offers due transcripts every 30 s. A duplicate dedupe
    key is ON CONFLICT DO NOTHING (no unique-violation error, no savepoint, no NOTIFY), as
    the API role too."""
    import uuid

    from sqlalchemy import event, text
    from sqlalchemy.ext.asyncio import create_async_engine

    from vektra_assess.jobs import JobKind, enqueue_job

    for who in ("api", "worker"):
        engine = create_async_engine(pg.url(who))
        seen: list[str] = []
        event.listen(
            engine.sync_engine,
            "before_cursor_execute",
            lambda *a, seen=seen: seen.append(str(a[2]).upper()),
        )
        key = f"transcript_deliver:{uuid.uuid4()}:retry"
        try:
            async with engine.begin() as conn:
                first = await enqueue_job(
                    conn,
                    JobKind.TRANSCRIPT_DELIVER,
                    {"transcript_id": "t"},
                    dedupe_key=key,
                )
                seen.clear()
                again = await enqueue_job(
                    conn,
                    JobKind.TRANSCRIPT_DELIVER,
                    {"transcript_id": "t"},
                    dedupe_key=key,
                )
                assert (first, again) == (True, False)
                assert not any("SAVEPOINT" in q for q in seen), seen
                assert not any("PG_NOTIFY" in q for q in seen), seen
                await conn.execute(text("SELECT 1"))  # the transaction is still usable
        finally:
            await engine.dispose()
        got = await pg.run(
            "super",
            "SELECT count(*) FROM assess_jobs WHERE dedupe_key = :k",
            {"k": key},
        )
        assert got[0][0] == 1


async def test_the_fan_out_scan_has_an_index(pg) -> None:  # type: ignore[no-untyped-def]
    got = await pg.run(
        "super",
        "SELECT indexdef FROM pg_indexes WHERE tablename = 'assess_transcripts' "
        "AND indexname = 'ix_assess_transcripts_generated_live'",
    )
    assert got and "generated_at" in got[0][0] and "ciphertext IS NOT NULL" in got[0][0]


async def test_the_fan_out_scan_moves_past_transcripts_already_queued(pg) -> None:  # type: ignore[no-untyped-def]
    """CodeRabbit 4504362595: with a small batch, the tick must not offer the same
    transcripts every time: one already holding a pending fan-out job is skipped."""
    from datetime import timedelta

    from sqlalchemy.ext.asyncio import create_async_engine

    from vektra_assess.delivery.handler import delivery_retry_tick

    for _ in range(3):
        await _transcript(pg)  # live transcripts with no delivery row
    no_rows = (
        "SELECT t.id FROM assess_transcripts t WHERE t.ciphertext IS NOT NULL "
        "AND NOT EXISTS (SELECT 1 FROM assess_deliveries d WHERE d.transcript_id = t.id)"
    )
    pending = [r[0] for r in await pg.run("super", no_rows)]
    engine = create_async_engine(pg.url("worker"))
    try:
        for _ in range(len(pending)):
            await delivery_retry_tick(
                engine.connect,
                stale_after=timedelta(seconds=240),
                window=timedelta(hours=72),
                batch=1,
            )
    finally:
        await engine.dispose()
    queued = await pg.run(
        "super",
        "SELECT count(DISTINCT dedupe_key) FROM assess_jobs WHERE dedupe_key = ANY(:k)",
        {"k": [f"transcript_deliver:{t}:fanout" for t in pending]},
    )
    assert queued[0][0] == len(pending)
