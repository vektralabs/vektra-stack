"""Crypto-shredding and purge helpers (plan assess-data-crypto 5.1, 5.2).

Library functions for the WORKER role; the jobs that call them are wired in later plans
(privacy-retention for the retention purge and OQ-033 erasure). Each runs inside the
caller's transaction and returns per-table deleted counts so a purge can be asserted by
counting rows, never by trusting a status.

Order matters: the wrapped DEK is nulled first (``shredded_at`` set, the
``kek_wrapped IS NULL = shredded_at IS NOT NULL`` check), then the ciphertext rows go. After
either function returns, no byte that could decrypt the data survives in this database.
"""

from __future__ import annotations

from dataclasses import dataclass, field

from sqlalchemy import text
from sqlalchemy.ext.asyncio import AsyncConnection


class ShredRefused(RuntimeError):
    """The target is not in a state in which it may be shredded."""


@dataclass
class ShredReport:
    deleted: dict[str, int] = field(default_factory=dict)

    def add(self, table: str, count: int) -> None:
        self.deleted[table] = self.deleted.get(table, 0) + count

    def merge(self, other: ShredReport) -> None:
        for table, count in other.deleted.items():
            self.add(table, count)


async def _delete(
    conn: AsyncConnection, report: ShredReport, table: str, where: str, **p: object
) -> None:
    result = await conn.execute(text(f"DELETE FROM {table} WHERE {where}"), p)
    report.add(table, result.rowcount)


async def shred_transcript(conn: AsyncConnection, transcript_id: object) -> ShredReport:
    """Null the wrapped DEK, then delete download tokens, deliveries and the transcript."""
    report = ShredReport()
    nulled = await conn.execute(
        text(
            "UPDATE assess_transcripts SET dek_wrapped = NULL, ciphertext = NULL, "
            "shredded_at = now() WHERE id = :t"
        ),
        {"t": transcript_id},
    )
    if nulled.rowcount == 0:
        return report  # nothing to shred: idempotent
    await _delete(
        conn,
        report,
        "assess_transcript_download_tokens",
        "transcript_id = :t",
        t=transcript_id,
    )
    await _delete(
        conn, report, "assess_deliveries", "transcript_id = :t", t=transcript_id
    )
    await _delete(conn, report, "assess_transcripts", "id = :t", t=transcript_id)
    return report


async def shred_session(
    conn: AsyncConnection, session_id: object, *, with_transcript: bool = False
) -> ShredReport:
    """Shred a CLOSED session. Refuses any other state, and refuses while a transcript
    still references it unless ``with_transcript`` says to shred that too."""
    state = (
        await conn.execute(
            text("SELECT state FROM assess_sessions WHERE id = :s FOR UPDATE"),
            {"s": session_id},
        )
    ).scalar()
    if state is None:
        return ShredReport()
    if state != "CLOSED":
        raise ShredRefused(f"session is {state}, only a CLOSED session can be shredded")
    report = ShredReport()
    transcript_ids = (
        (
            await conn.execute(
                text("SELECT id FROM assess_transcripts WHERE session_id = :s"),
                {"s": session_id},
            )
        )
        .scalars()
        .all()
    )
    if transcript_ids and not with_transcript:
        raise ShredRefused("a transcript still references this session; shred it first")
    for tid in transcript_ids:
        report.merge(await shred_transcript(conn, tid))

    await conn.execute(
        text(
            "UPDATE assess_session_keys SET kek_wrapped = NULL, shredded_at = now() WHERE session_id = :s"
        ),
        {"s": session_id},
    )
    for table, col in (
        ("assess_session_handoff", "session_id"),
        ("assess_turns", "session_id"),
        ("assess_snapshots", "session_id"),
        ("assess_session_keys", "session_id"),
        ("assess_outage_incidents", "session_id"),
        ("assess_enrolment_checks", "session_id"),
        ("assess_sessions", "id"),
    ):
        await _delete(conn, report, table, f"{col} = :s", s=session_id)
    return report
