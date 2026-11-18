"""The worker half of a teacher's read-back of one sealed config field, "Mostra" (FEAT-030).

Bound to the ``config_reveal`` job kind. The API holds no private key of the teacher config
(ARCH-071, OQ-034 resolved 2026-11-18), so a fresh editor's read-back queues this job (args: the
site, the cmid, the field, a fresh request id and the request's EPHEMERAL public key) and waits
for its row in ``assess_config_reveals``. The handler opens the ONE field asked for with the
worker's private key (``read_teacher_config``), re-seals it to the ephemeral key
(``crypto.reveal.seal_reveal``, binding ``[site, cmid, field, request]``) and writes ONE row:

- ``ok`` with the re-sealed value (ciphertext only: the plaintext exists in this process's memory
  for the duration of the call and is never written or logged);
- ``not_set``: the field has no value now (never set, or cleared meanwhile);
- ``unreadable``: the stored seal does not open, or its plaintext has the wrong shape.

A malformed job (unknown field, a public key that is not 32 bytes, a bad id) writes nothing: the
API times out (503). The API deletes the row it read; ``sweep_config_reveals`` (the sweeper tick)
deletes rows nobody picked up. A late row is harmless: its ephemeral private key is gone.
"""

from __future__ import annotations

import json
from collections.abc import Awaitable, Callable, Mapping
from typing import Any
from uuid import UUID

import structlog
from sqlalchemy import text

from vektra_assess.crypto.reveal import public_from_hex, seal_reveal
from vektra_assess.crypto.teacher_config import (
    FIELDS,
    TRANSCRIPT_RECIPIENTS,
    read_teacher_config,
)
from vektra_assess.keys import AssessKeyring
from vektra_assess.worker.runner import AcquireDb, JobContext

log = structlog.get_logger(__name__)

#: rows older than this are deleted by the sweeper: the API waits a few seconds at most
REVEAL_RETENTION_S = 60

OK, NOT_SET, UNREADABLE = "ok", "not_set", "unreadable"

_INSERT = text(
    "INSERT INTO assess_config_reveals (id, site_id, cmid, field, status, sealed_value) "
    "VALUES (:id, :site, :cmid, :field, :status, :value) ON CONFLICT (id) DO NOTHING"
)
_SWEEP = text(
    "DELETE FROM assess_config_reveals WHERE created_at < clock_timestamp() - "
    "make_interval(secs => :age)"
)


def _parse(
    args: Mapping[str, Any],
) -> tuple[UUID, int, str, UUID, bytes] | None:
    """The job's ids, or None for a malformed job (a stale no-op that writes nothing)."""
    try:
        site = UUID(str(args["site_id"]))
        rid = UUID(str(args["request_id"]))
        cmid, field, public = args["cmid"], args["field"], args["public_key"]
    except (KeyError, ValueError):
        return None
    if not isinstance(cmid, int) or isinstance(cmid, bool) or cmid < 1:
        return None
    if field not in FIELDS:
        return None
    key = public_from_hex(public)
    if key is None:
        return None
    return site, cmid, str(field), rid, key


async def reveal_field(
    private: AssessKeyring, acquire: AcquireDb, args: Mapping[str, Any]
) -> str | None:
    """Answer one read-back; returns the status written (None: malformed, nothing written)."""
    parsed = _parse(args)
    if parsed is None:
        log.warning("assess_config_reveal_malformed")
        return None
    site, cmid, field, rid, public = parsed
    async with acquire() as conn:
        secrets = await read_teacher_config(conn, private, site, cmid, (field,))
        await conn.rollback()
    value: bytes | None = None
    if field in secrets.unreadable:
        status = UNREADABLE
    else:
        plain = getattr(secrets, field)
        if plain is None:
            status = NOT_SET
        else:
            text_ = json.dumps(list(plain)) if field == TRANSCRIPT_RECIPIENTS else plain
            try:
                value = seal_reveal(
                    public, text_.encode("utf-8"), site, cmid, field, rid
                )
                status = OK
            except Exception as exc:  # a public key X25519 refuses (low order)
                log.warning(
                    "assess_config_reveal_seal_failed", error_type=type(exc).__name__
                )
                status = UNREADABLE
    async with acquire() as conn:
        async with conn.begin():
            await conn.execute(
                _INSERT,
                {
                    "id": rid,
                    "site": site,
                    "cmid": cmid,
                    "field": field,
                    "status": status,
                    "value": value,
                },
            )
    # who asked is audited by the API; this line makes the worker's own answers countable
    # even if an API were bypassed (ids and a status code only, never the value)
    log.info(
        "assess_config_reveal_answered",
        site_id=str(site),
        cmid=cmid,
        field=field,
        status=status,
    )
    return status


async def sweep_config_reveals(
    acquire: AcquireDb, *, age_s: int = REVEAL_RETENTION_S
) -> int:
    """Delete read-back answers nobody picked up (the API timed out, or died)."""
    async with acquire() as conn:
        async with conn.begin():
            deleted = (await conn.execute(_SWEEP, {"age": age_s})).rowcount or 0
    if deleted:
        log.info("assess_config_reveals_swept", count=deleted)
    return int(deleted)


def make_config_reveal_handler(
    private: AssessKeyring,
) -> Callable[[JobContext], Awaitable[None]]:
    async def handle(ctx: JobContext) -> None:
        await reveal_field(private, ctx.acquire, ctx.args)

    return handle
