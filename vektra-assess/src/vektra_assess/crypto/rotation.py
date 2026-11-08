"""Key rotation helpers (plan assess-data-crypto 5.3, 5.4, 5.5), worker side.

Each helper is resumable and works in bounded batches: it returns how many rows still
carry the old key, and the caller loops (committing between batches) until it is 0, after
which the old kid can be removed from the keyring. ``SKIP LOCKED`` keeps concurrent runs
from fighting over a row.
"""

from __future__ import annotations

from sqlalchemy import text
from sqlalchemy.ext.asyncio import AsyncConnection

from vektra_assess.crypto import envelope
from vektra_assess.crypto.kek import unwrap_dek, wrap_dek
from vektra_assess.crypto.seal import open_sealed, seal
from vektra_assess.crypto.transcript import unwrap_transcript_dek
from vektra_assess.keys import AssessKeyring


async def rewrap_session_keys(
    conn: AsyncConnection, keyring: AssessKeyring, from_kid: str, batch: int = 100
) -> int:
    """Re-wrap up to ``batch`` session DEKs from ``from_kid`` to the active kid."""
    rows = (
        await conn.execute(
            text(
                "SELECT session_id, kek_wrapped, key_version FROM assess_session_keys "
                "WHERE kek_kid = :k AND kek_wrapped IS NOT NULL "
                "ORDER BY session_id LIMIT :n FOR UPDATE SKIP LOCKED"
            ),
            {"k": from_kid, "n": batch},
        )
    ).all()
    for sid, wrapped, version in rows:
        dek = unwrap_dek(keyring, bytes(wrapped), sid, version)
        blob, kid, new_version = wrap_dek(keyring, dek, sid, version + 1)
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
                "WHERE kek_kid = :k AND kek_wrapped IS NOT NULL"
            ),
            {"k": from_kid},
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
    conn: AsyncConnection, keyring: AssessKeyring, from_kid: str, batch: int = 100
) -> int:
    """Re-wrap up to ``batch`` transcript DEKs from ``from_kid``. Returns the remaining count."""
    rows = (
        await conn.execute(
            text(
                "SELECT id, dek_wrapped FROM assess_transcripts "
                "WHERE transcript_kid = :k AND dek_wrapped IS NOT NULL "
                "ORDER BY id LIMIT :n FOR UPDATE SKIP LOCKED"
            ),
            {"k": from_kid, "n": batch},
        )
    ).all()
    for tid, wrapped in rows:
        dek = unwrap_transcript_dek(keyring, tid, bytes(wrapped))
        fresh = envelope.aead_encrypt(
            keyring.active_key,
            keyring.active,
            dek,
            purpose=envelope.Purpose.TRANSCRIPT_DEK,
            binding=[tid],
        )
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
                "WHERE transcript_kid = :k AND dek_wrapped IS NOT NULL"
            ),
            {"k": from_kid},
        )
    ).scalar_one()
    return int(result)


async def rewrap_teacher_reasons(
    conn: AsyncConnection, keyring: AssessKeyring, from_kid: str, batch: int = 100
) -> int:
    """Re-encrypt up to ``batch`` teacher reasons (resets and terminations, plan 11b) from
    ``from_kid`` to the active transcript KEK, per table. Returns the remaining count."""
    from vektra_assess.crypto.teacher_reason import (
        TABLES,
        decrypt_reason,
        encrypt_reason,
    )

    remaining = 0
    for table in TABLES:  # a closed list of table names, never input
        rows = (
            await conn.execute(
                text(
                    f"SELECT id, reason_ct FROM {table} WHERE reason_kid = :k "
                    "ORDER BY id LIMIT :n FOR UPDATE SKIP LOCKED"
                ),
                {"k": from_kid, "n": batch},
            )
        ).all()
        for rid, blob in rows:
            plain = decrypt_reason(keyring, table, rid, bytes(blob))
            fresh, kid = encrypt_reason(keyring, table, rid, plain)
            await conn.execute(
                text(
                    f"UPDATE {table} SET reason_ct = :b, reason_kid = :k WHERE id = :i"
                ),
                {"b": fresh, "k": kid, "i": rid},
            )
        remaining += int(
            (
                await conn.execute(
                    text(f"SELECT count(*) FROM {table} WHERE reason_kid = :k"),
                    {"k": from_kid},
                )
            ).scalar_one()
        )
    return remaining
