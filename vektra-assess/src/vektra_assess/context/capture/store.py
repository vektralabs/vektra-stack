"""Snapshot persistence for the worker: insert, delete, verify (BR-015).

The table is plan 2's ``assess_snapshots`` (migration 0011): ``session_id`` PK with ``ON DELETE
CASCADE``, ``ciphertext`` (the AES-256-GCM envelope ``kid | nonce | ct`` under the session DEK, AAD
bound to the session id and the snapshot purpose), ``content_hash``; a trigger rejects every UPDATE,
and only the worker role holds SELECT / INSERT / DELETE. The API never reads the table: a turn gets
the ciphertext through the definer function ``get_session_material``. ``schema_version`` lives in the
authenticated plaintext (``SnapshotHeader``), so it needs no column of its own.

Audit rows go through ``write_audit`` on the SAME connection as the write they describe, so a failed
audit insert aborts the transaction (fail closed): no snapshot without its exclusion event.
"""

from __future__ import annotations

import json
from collections.abc import Mapping
from dataclasses import dataclass
from enum import StrEnum
from typing import Any, Protocol
from uuid import UUID

from cryptography.exceptions import InvalidTag
from pydantic import ValidationError
from sqlalchemy import text

from vektra_assess.context.snapshot import (
    Snapshot,
    canonical_bytes,
    content_hash,
    from_canonical_bytes,
)
from vektra_assess.crypto import envelope


class Conn(Protocol):
    async def execute(self, statement: Any, parameters: Any = ..., /) -> Any: ...


_AUDIT = text(
    "INSERT INTO assess_audit_events (site_id, session_id, event, reason_code, actor_kind, data) "
    "VALUES (:site, :session, :event, :reason, 'worker', CAST(:data AS jsonb))"
)


async def write_audit(
    conn: Conn,
    event: str,
    *,
    session_id: UUID | None = None,
    site_id: UUID | None = None,
    reason_code: str | None = None,
    data: Mapping[str, Any] | None = None,
) -> None:
    """One content-free audit row, in the caller's transaction (raises on failure)."""
    await conn.execute(
        _AUDIT,
        {
            "site": site_id,
            "session": session_id,
            "event": event,
            "reason": reason_code,
            "data": json.dumps(dict(data or {}), sort_keys=True),
        },
    )


async def insert_snapshot(
    conn: Conn, session_id: UUID, dek: bytes, snapshot: Snapshot
) -> str:
    """Encrypt and insert; returns the content hash for the commit audit event."""
    plaintext = canonical_bytes(snapshot)
    digest = content_hash(snapshot)
    await conn.execute(
        text(
            "INSERT INTO assess_snapshots (session_id, ciphertext, content_hash) "
            "VALUES (:s, :c, :h)"
        ),
        {
            "s": session_id,
            "c": envelope.encrypt_snapshot(dek, session_id, plaintext),
            "h": digest,
        },
    )
    return digest


async def delete_snapshot(conn: Conn, session_id: UUID) -> int:
    """Rollback / purge: delete the session's snapshot; returns the rows deleted (0 or 1)."""
    res = await conn.execute(
        text("DELETE FROM assess_snapshots WHERE session_id = :s"), {"s": session_id}
    )
    return int(res.rowcount or 0)


async def load_snapshot(conn: Conn, session_id: UUID, dek: bytes) -> Snapshot | None:
    row = (
        await conn.execute(
            text("SELECT ciphertext FROM assess_snapshots WHERE session_id = :s"),
            {"s": session_id},
        )
    ).first()
    if row is None:
        return None
    return from_canonical_bytes(
        envelope.decrypt_snapshot(dek, session_id, bytes(row.ciphertext))
    )


class MismatchReason(StrEnum):
    MISSING = "missing"
    DECRYPT_FAILED = (
        "decrypt_failed"  # tampered ciphertext or wrong key: AEAD tag check
    )
    UNPARSEABLE = "unparseable"
    HASH_MISMATCH = "hash_mismatch"  # plaintext hash differs from the commit hash


@dataclass(frozen=True)
class Ok:
    snapshot: Snapshot


@dataclass(frozen=True)
class Mismatch:
    reason: MismatchReason


async def verify_snapshot_integrity(
    conn: Conn, session_id: UUID, dek: bytes, expected_hash: str
) -> Ok | Mismatch:
    """Plan 10 at transcript generation: recompute the hash and compare it with the commit hash.

    A mismatch is audited ``assess_snapshot_integrity_failed`` (reason code only) in the caller's
    transaction. Both the stored ``content_hash`` column and the recomputed hash must equal the
    commit hash, so an attacker who rewrites the column cannot make a tampered row pass.
    """
    row = (
        await conn.execute(
            text(
                "SELECT ciphertext, content_hash FROM assess_snapshots WHERE session_id = :s"
            ),
            {"s": session_id},
        )
    ).first()
    result: Ok | Mismatch
    if row is None:
        result = Mismatch(MismatchReason.MISSING)
    else:
        try:
            snap = from_canonical_bytes(
                envelope.decrypt_snapshot(dek, session_id, bytes(row.ciphertext))
            )
        except (InvalidTag, envelope.EnvelopeError):
            result = Mismatch(MismatchReason.DECRYPT_FAILED)
        except (ValidationError, ValueError):
            result = Mismatch(MismatchReason.UNPARSEABLE)
        else:
            digest = content_hash(snap)
            ok = digest == expected_hash and row.content_hash == expected_hash
            result = Ok(snap) if ok else Mismatch(MismatchReason.HASH_MISMATCH)
    if isinstance(result, Mismatch):
        await write_audit(
            conn,
            "assess_snapshot_integrity_failed",
            session_id=session_id,
            reason_code=result.reason.value,
        )
    return result
