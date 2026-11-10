"""Key rotation helpers (plan assess-data-crypto 5.3, 5.4, 5.5), worker side.

Each helper is resumable and works in bounded batches: it returns how many rows still
carry the old key, and the caller loops (committing between batches) until it is 0, after
which the old kid can be removed from the keyring. ``SKIP LOCKED`` keeps concurrent runs
from fighting over a row.
"""

from __future__ import annotations

from collections.abc import Awaitable, Callable, Sequence
from typing import Any

from sqlalchemy import text
from sqlalchemy.ext.asyncio import AsyncConnection

from vektra_assess.crypto import envelope
from vektra_assess.crypto.kek import unwrap_dek, wrap_dek
from vektra_assess.crypto.seal import open_sealed, seal
from vektra_assess.crypto.transcript import unwrap_transcript_dek
from vektra_assess.keys import AssessKeyring

#: ``skip`` (plan 13a review): the rows of a re-wrap run that did not decrypt, as
#: ``"<table>:<id>"``. They are left under the old kid, never retried in the same run and
#: not counted as "left", so one corrupt row neither aborts the run nor stalls it; the
#: caller reports them by id. Only ids go in, never content.
_SKIP_SQL = "NOT (CAST({col} AS text) = ANY(CAST(:skip AS text[])))"


def _skipped(skip: set[str] | None, table: str) -> list[str]:
    prefix = f"{table}:"
    return [k[len(prefix) :] for k in (skip or ()) if k.startswith(prefix)]


async def rewrap_session_keys(
    conn: AsyncConnection,
    keyring: AssessKeyring,
    from_kid: str,
    batch: int = 100,
    *,
    skip: set[str] | None = None,
) -> int:
    """Re-wrap up to ``batch`` session DEKs from ``from_kid`` to the active kid."""
    not_skipped = _SKIP_SQL.format(col="session_id")
    excluded = _skipped(skip, "assess_session_keys")
    rows = (
        await conn.execute(
            text(
                "SELECT session_id, kek_wrapped, key_version FROM assess_session_keys "
                f"WHERE kek_kid = :k AND kek_wrapped IS NOT NULL AND {not_skipped} "
                "ORDER BY session_id LIMIT :n FOR UPDATE SKIP LOCKED"
            ),
            {"k": from_kid, "n": batch, "skip": excluded},
        )
    ).all()
    for sid, wrapped, version in rows:
        try:
            dek = unwrap_dek(keyring, bytes(wrapped), sid, version)
            blob, kid, new_version = wrap_dek(keyring, dek, sid, version + 1)
        except Exception:
            if skip is None:
                raise
            skip.add(f"assess_session_keys:{sid}")
            continue
        await conn.execute(
            text(
                "UPDATE assess_session_keys SET kek_wrapped = :b, kek_kid = :k, "
                "key_version = :v WHERE session_id = :s"
            ),
            {"b": blob, "k": kid, "v": new_version, "s": sid},
        )
    result = (
        await conn.execute(
            text(
                "SELECT count(*) FROM assess_session_keys "
                f"WHERE kek_kid = :k AND kek_wrapped IS NOT NULL AND {not_skipped}"
            ),
            {"k": from_kid, "skip": _skipped(skip, "assess_session_keys")},
        )
    ).scalar_one()
    return int(result)


async def reseal_handoffs(
    conn: AsyncConnection,
    kek: AssessKeyring,
    new_api_pub: bytes,
    new_kid: str,
    limit: int = 100,
) -> int:
    """Re-seal up to ``limit`` LIVE hand-off DEKs to the new API public key. Returns how
    many live rows STILL carry another kid (0 means done; a locked or skipped row keeps the
    count above 0, so a pass can never look finished while rows remain). A session that
    closed meanwhile has no hand-off row any more (the composite FK and the close trigger
    delete it), so it drops out of the count."""
    rows = (
        await conn.execute(
            text(
                "SELECT h.session_id, k.kek_wrapped, k.key_version FROM assess_session_handoff h "
                "JOIN assess_session_keys k ON k.session_id = h.session_id "
                "WHERE h.handoff_kid <> :n AND k.kek_wrapped IS NOT NULL "
                "ORDER BY h.session_id LIMIT :l FOR UPDATE OF h SKIP LOCKED"
            ),
            {"n": new_kid, "l": limit},
        )
    ).all()
    for sid, wrapped, version in rows:
        dek = unwrap_dek(kek, bytes(wrapped), sid, version)
        sealed = seal(
            new_api_pub,
            new_kid,
            dek,
            purpose=envelope.Purpose.HANDOFF,
            binding=[sid, new_kid],
        )
        result = await conn.execute(
            text(
                "UPDATE assess_session_handoff SET sealed_dek = :b, handoff_kid = :k "
                "WHERE session_id = :s AND session_state IN ('ACTIVE', 'OUTAGE')"
            ),
            {"b": sealed, "k": new_kid, "s": sid},
        )
        _ = result.rowcount  # 0 when the session closed meanwhile: nothing to reseal
    remaining = (
        await conn.execute(
            text("SELECT count(*) FROM assess_session_handoff WHERE handoff_kid <> :n"),
            {"n": new_kid},
        )
    ).scalar_one()
    return int(remaining)


async def reseal_teacher_configs(
    conn: AsyncConnection,
    old_worker_priv: AssessKeyring,
    new_worker_pub: AssessKeyring,
    batch: int = 100,
) -> int:
    """Re-seal up to ``batch`` sealed teacher-config fields to the new worker public key, each
    under its own binding ``[site_id, cmid, field]`` (revision 0036). Returns how many still
    carry another kid (0 means done). Only ``sealed_blob`` and ``kid`` change: the database's
    change audit skips the worker's login role, so a rotation is never audited as a change."""
    new_kid = new_worker_pub.active
    rows = (
        await conn.execute(
            text(
                "SELECT site_id, cmid, field, sealed_blob FROM assess_activity_config_sealed "
                "WHERE sealed_blob IS NOT NULL AND kid <> :k "
                "ORDER BY site_id, cmid, field LIMIT :n FOR UPDATE SKIP LOCKED"
            ),
            {"k": new_kid, "n": batch},
        )
    ).all()
    for site_id, cmid, field, sealed in rows:
        binding = [site_id, cmid, field]
        plain = open_sealed(
            old_worker_priv,
            bytes(sealed),
            purpose=envelope.Purpose.TEACHER_CONFIG,
            binding=binding,
        )
        fresh = seal(
            new_worker_pub.active_key,
            new_kid,
            plain,
            purpose=envelope.Purpose.TEACHER_CONFIG,
            binding=binding,
        )
        await conn.execute(
            text(
                "UPDATE assess_activity_config_sealed SET sealed_blob = :b, kid = :k "
                "WHERE site_id = :s AND cmid = :c AND field = :f"
            ),
            {"b": fresh, "k": new_kid, "s": site_id, "c": cmid, "f": field},
        )
    remaining = (
        await conn.execute(
            text(
                "SELECT count(*) FROM assess_activity_config_sealed "
                "WHERE sealed_blob IS NOT NULL AND kid <> :k"
            ),
            {"k": new_kid},
        )
    ).scalar_one()
    return int(remaining)


async def rewrap_transcript_deks(
    conn: AsyncConnection,
    keyring: AssessKeyring,
    from_kid: str,
    batch: int = 100,
    *,
    skip: set[str] | None = None,
) -> int:
    """Re-wrap up to ``batch`` transcript DEKs from ``from_kid``. Returns the remaining count."""
    not_skipped = _SKIP_SQL.format(col="id")
    rows = (
        await conn.execute(
            text(
                "SELECT id, dek_wrapped FROM assess_transcripts "
                f"WHERE transcript_kid = :k AND dek_wrapped IS NOT NULL AND {not_skipped} "
                "ORDER BY id LIMIT :n FOR UPDATE SKIP LOCKED"
            ),
            {"k": from_kid, "n": batch, "skip": _skipped(skip, "assess_transcripts")},
        )
    ).all()
    for tid, wrapped in rows:
        try:
            dek = unwrap_transcript_dek(keyring, tid, bytes(wrapped))
            fresh = envelope.aead_encrypt(
                keyring.active_key,
                keyring.active,
                dek,
                purpose=envelope.Purpose.TRANSCRIPT_DEK,
                binding=[tid],
            )
        except Exception:
            if skip is None:
                raise
            skip.add(f"assess_transcripts:{tid}")
            continue
        await conn.execute(
            text(
                "UPDATE assess_transcripts SET dek_wrapped = :b, transcript_kid = :k WHERE id = :i"
            ),
            {"b": fresh, "k": keyring.active, "i": tid},
        )
    result = (
        await conn.execute(
            text(
                "SELECT count(*) FROM assess_transcripts "
                f"WHERE transcript_kid = :k AND dek_wrapped IS NOT NULL AND {not_skipped}"
            ),
            {"k": from_kid, "skip": _skipped(skip, "assess_transcripts")},
        )
    ).scalar_one()
    return int(result)


async def rewrap_teacher_reasons(
    conn: AsyncConnection,
    keyring: AssessKeyring,
    from_kid: str,
    batch: int = 100,
    *,
    skip: set[str] | None = None,
) -> int:
    """Re-encrypt up to ``batch`` teacher reasons (resets and terminations, plan 11b) from
    ``from_kid`` to the active transcript KEK, per table. Returns the remaining count."""
    from vektra_assess.crypto.teacher_reason import (
        TABLES,
        decrypt_reason,
        encrypt_reason,
    )

    remaining = 0
    not_skipped = _SKIP_SQL.format(col="id")
    for table in TABLES:  # a closed list of table names, never input
        rows = (
            await conn.execute(
                text(
                    f"SELECT id, reason_ct FROM {table} WHERE reason_kid = :k "
                    f"AND {not_skipped} ORDER BY id LIMIT :n FOR UPDATE SKIP LOCKED"
                ),
                {"k": from_kid, "n": batch, "skip": _skipped(skip, table)},
            )
        ).all()
        for rid, blob in rows:
            try:
                plain = decrypt_reason(keyring, table, rid, bytes(blob))
                fresh, kid = encrypt_reason(keyring, table, rid, plain)
            except Exception:
                if skip is None:
                    raise
                skip.add(f"{table}:{rid}")
                continue
            await conn.execute(
                text(
                    f"UPDATE {table} SET reason_ct = :b, reason_kid = :k WHERE id = :i"
                ),
                {"b": fresh, "k": kid, "i": rid},
            )
        remaining += int(
            (
                await conn.execute(
                    text(
                        f"SELECT count(*) FROM {table} WHERE reason_kid = :k "
                        f"AND {not_skipped}"
                    ),
                    {"k": from_kid, "skip": _skipped(skip, table)},
                )
            ).scalar_one()
        )
    return remaining


async def rewrap_to_done(
    connect: Callable[[], Any],
    steps: Sequence[Callable[[AsyncConnection], Awaitable[int]]],
    *,
    stall_after: int = 3,
) -> int:
    """Run each re-wrap step in committed batches until it reports 0 rows left (plan 13a).

    Returns what is still left: 0 means the old kid can be retired. A step whose remaining count
    does not go down for ``stall_after`` batches in a row stops the run with that count, so a
    row locked elsewhere ends it instead of spinning forever; the caller runs it again later.
    The helpers are resumable (``SKIP LOCKED``), so a stopped run loses nothing.
    """
    left_total = 0
    for step in steps:
        best: int | None = None
        stalled = 0
        while True:
            async with connect() as conn:
                left = await step(conn)
                await conn.commit()
            if left == 0:
                break
            if best is not None and left >= best:
                stalled += 1
                if stalled >= stall_after:
                    left_total += left
                    break
            else:
                best, stalled = left, 0
    return left_total
