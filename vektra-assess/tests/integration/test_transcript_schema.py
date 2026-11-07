"""Revision 0034 (plan assess-transcript-delivery): outbox constraints, resend function, round trip.

The resend function is driven as the real API role: it creates round n+1 for the recipients of
the last round only (a REPLACED recipient is not resent), two calls never collide on a round, and
an unknown or never-delivered transcript yields 0 without a row or an audit event.
"""

from __future__ import annotations

import uuid

import pytest
from sqlalchemy.exc import DBAPIError, IntegrityError

from .conftest import ROOT, _alembic, make_session

pytestmark = pytest.mark.integration


async def _transcript(pg) -> uuid.UUID:  # type: ignore[no-untyped-def]
    sid = await make_session(pg, "CLOSED")
    tid = uuid.uuid4()
    await pg.run(
        "worker",
        "INSERT INTO assess_transcripts (id, session_id, ciphertext, dek_wrapped, transcript_kid, "
        "content_hash, turns_digest, filename, byte_size) VALUES (:t, :s, '\\x00', '\\x00', 'k1', "
        "'h', 'd', 'f.html', 1)",
        {"t": tid, "s": sid},
    )
    return tid


async def _deliver(pg, tid, userid, status="SENT", round_=1):  # type: ignore[no-untyped-def]
    sent = "now()" if status == "SENT" else "NULL"
    await pg.run(
        "worker",
        "INSERT INTO assess_deliveries (transcript_id, round, recipient_kind, recipient_userid, "
        f"status, sent_at) VALUES (:t, :r, 'grader', :u, :st, {sent})",
        {"t": tid, "r": round_, "u": userid, "st": status},
    )


async def test_a_live_transcript_needs_its_digest_name_and_size(pg) -> None:  # type: ignore[no-untyped-def]
    sid = await make_session(pg, "CLOSED")
    with pytest.raises(IntegrityError, match="ck_assess_transcripts_live_fields"):
        await pg.run(
            "worker",
            "INSERT INTO assess_transcripts (session_id, ciphertext, dek_wrapped, transcript_kid, "
            "content_hash) VALUES (:s, '\\x00', '\\x00', 'k1', 'h')",
            {"s": sid},
        )


async def test_sent_iff_sent_at_and_one_row_per_round_and_recipient(pg) -> None:  # type: ignore[no-untyped-def]
    tid = await _transcript(pg)
    with pytest.raises(IntegrityError, match="ck_assess_deliveries_sent"):
        await pg.run(
            "worker",
            "INSERT INTO assess_deliveries (transcript_id, recipient_kind, recipient_userid, "
            "status) VALUES (:t, 'grader', 1, 'SENT')",
            {"t": tid},
        )
    await _deliver(pg, tid, 1, "PENDING")
    with pytest.raises(IntegrityError, match="uq_assess_deliveries_round"):
        await _deliver(pg, tid, 1, "PENDING")
    with pytest.raises(IntegrityError, match="ck_assess_deliveries_transcript"):
        await pg.run(
            "worker",
            "INSERT INTO assess_deliveries (kind, recipient_kind, recipient_userid) "
            "VALUES ('transcript', 'grader', 2)",
        )


async def test_resend_is_a_new_round_for_the_current_recipients(pg) -> None:  # type: ignore[no-untyped-def]
    tid = await _transcript(pg)
    await _deliver(pg, tid, 11, "SENT")
    await _deliver(pg, tid, 12, "FAILED")
    await _deliver(pg, tid, 13, "REPLACED")
    got = await pg.run(
        "api", "SELECT assess_enqueue_resend(:t, 'grader:11', 0)", {"t": tid}
    )
    assert got[0][0] == 2
    rows = await pg.run(
        "super",
        "SELECT recipient_userid, status FROM assess_deliveries WHERE transcript_id = :t "
        "AND round = 2 ORDER BY recipient_userid",
        {"t": tid},
    )
    assert [tuple(r) for r in rows] == [(11, "PENDING"), (12, "PENDING")]
    again = await pg.run(
        "api", "SELECT assess_enqueue_resend(:t, 'grader:11', 0)", {"t": tid}
    )
    assert again[0][0] == 3  # serialised: never the same round twice
    audits = await pg.run(
        "super",
        "SELECT count(*) FROM assess_audit_events WHERE event = 'assess_transcript_resend' "
        "AND (data->>'round')::int IN (2, 3)",
    )
    assert audits[0][0] == 2


async def test_resend_of_nothing_writes_nothing(pg) -> None:  # type: ignore[no-untyped-def]
    tid = await _transcript(pg)  # never delivered
    for target in (tid, uuid.uuid4()):
        got = await pg.run(
            "api", "SELECT assess_enqueue_resend(:t, 'operator', 0)", {"t": target}
        )
        assert got[0][0] == 0
    rows = await pg.run(
        "super",
        "SELECT count(*) FROM assess_deliveries WHERE transcript_id = :t",
        {"t": tid},
    )
    assert rows[0][0] == 0


async def test_the_api_cannot_write_deliveries_directly(pg) -> None:  # type: ignore[no-untyped-def]
    tid = await _transcript(pg)
    with pytest.raises(DBAPIError, match="permission denied"):
        await pg.run(
            "api",
            "INSERT INTO assess_deliveries (transcript_id, recipient_kind, recipient_userid) "
            "VALUES (:t, 'grader', 99)",
            {"t": tid},
        )


def test_round_trip_to_0033_and_back(pg) -> None:  # type: ignore[no-untyped-def]
    assert ROOT.exists()
    down = _alembic(pg.super_url, "downgrade", "0033")
    assert down.returncode == 0, down.stderr
    up = _alembic(pg.super_url, "upgrade", "head")
    assert up.returncode == 0, up.stderr


async def test_a_round_inside_the_minimum_interval_is_refused_under_the_lock(
    pg,
) -> None:  # type: ignore[no-untyped-def]
    """Review #6: -1 (no row, no audit) while the last round is younger than the interval."""
    tid = await _transcript(pg)
    await _deliver(pg, tid, 11, "SENT")
    got = await pg.run(
        "api", "SELECT assess_enqueue_resend(:t, 'grader:11', 600)", {"t": tid}
    )
    assert got[0][0] == -1
    await pg.run(
        "super",
        "UPDATE assess_deliveries SET created_at = now() - interval '11 minutes' "
        "WHERE transcript_id = :t",
        {"t": tid},
    )
    got = await pg.run(
        "api", "SELECT assess_enqueue_resend(:t, 'grader:11', 600)", {"t": tid}
    )
    assert got[0][0] == 2
    got = await pg.run(
        "api", "SELECT assess_enqueue_resend(:t, 'grader:11', 600)", {"t": tid}
    )
    assert got[0][0] == -1  # the new round restarts the interval


async def test_concurrent_resends_serialise_on_the_transcript_lock(pg) -> None:  # type: ignore[no-untyped-def]
    """CodeRabbit nitpick (r1): overlapping calls on separate connections. Without an interval
    every call gets its own round; with one, exactly one call wins and the others get -1."""
    import asyncio

    async def call(tid, gap: int) -> int:  # type: ignore[no-untyped-def]
        return int(
            (
                await pg.run(
                    "api",
                    "SELECT assess_enqueue_resend(:t, 'grader:11', :g)",
                    {"t": tid, "g": gap},
                )
            )[0][0]
        )

    tid = await _transcript(pg)
    await _deliver(pg, tid, 11, "SENT")
    rounds = await asyncio.gather(*(call(tid, 0) for _ in range(5)))
    assert sorted(rounds) == [2, 3, 4, 5, 6]
    tid2 = await _transcript(pg)
    await _deliver(pg, tid2, 11, "SENT")
    await pg.run(
        "super",
        "UPDATE assess_deliveries SET created_at = now() - interval '1 hour' "
        "WHERE transcript_id = :t",
        {"t": tid2},
    )
    rounds = await asyncio.gather(*(call(tid2, 600) for _ in range(5)))
    assert sorted(rounds) == [-1, -1, -1, -1, 2]
