"""The live course-module eligibility read of one RAG turn (FEAT-028, ARCH-072 amendment).

Bound to the ``material_eligibility`` job kind. The API cannot ask the LMS, so a discussion turn
with course material queues this job (args: ``session_id``, ``request_id``) and waits for its row
in ``assess_material_reads``. The handler re-reads the session (still ACTIVE or OUTAGE), resolves
its course inside the student's own courses, reads the course's modules NOW (TTL 0: nothing of
the read is cached) and writes ONE row:

- readable: the course's namespace (the ingest pipeline's slug of the short name) and the
  ELIGIBLE modules only, ``[[cmid, title], ...]``;
- not readable: a closed ``reason`` code (``rag_off``, ``session_not_live``, ``rag_withdrawn``
  (the activity's RAG is no longer ON and attested),
  ``course_unresolved``, ``modules_unreadable``, ``error``) and nothing else.

The row holds module ids and titles, never content and never a student value. The API deletes
the row it read; ``sweep_material_reads`` (the sweeper tick) deletes rows nobody picked up.
"""

from __future__ import annotations

import json
from collections.abc import Awaitable, Callable
from typing import Any, Protocol
from uuid import UUID

import structlog
from sqlalchemy import text

from vektra_assess.context.materials import course_namespace, eligible_modules
from vektra_assess.worker.preflight import StartTarget
from vektra_assess.worker.runner import AcquireDb, JobContext
from vektra_shared.lms import (
    ActivityRef,
    CourseModuleVisibility,
    Readable,
    Unreadable,
)

log = structlog.get_logger(__name__)

#: rows older than this are deleted by the sweeper: the API waits a few seconds at most
READ_RETENTION_S = 300

_SESSION = text(
    "SELECT s.site_id, s.cmid, s.student_userid, s.courseid_hint, s.session_no, s.state, "
    "c.activity_type, (c.rag_enabled AND c.rag_attested_at IS NOT NULL) AS rag_on "
    "FROM assess_sessions s JOIN assess_activity_config c "
    "ON c.site_id = s.site_id AND c.cmid = s.cmid WHERE s.id = :sid"
)
_INSERT = text(
    "INSERT INTO assess_material_reads (id, session_id, site_id, readable, reason, namespace, "
    "modules) VALUES (:id, :sid, :site, :ok, :reason, :ns, CAST(:modules AS jsonb)) "
    "ON CONFLICT (id) DO NOTHING"
)
_SWEEP = text(
    "DELETE FROM assess_material_reads WHERE read_at < clock_timestamp() - "
    "make_interval(secs => :age)"
)


class MaterialSource(Protocol):
    @property
    def rag_opt_out_tag(self) -> str | None: ...

    async def course_material(
        self, target: StartTarget
    ) -> (
        tuple[ActivityRef, Readable[tuple[CourseModuleVisibility, ...]] | Unreadable]
        | None
    ): ...


async def read_material(
    source: MaterialSource, acquire: AcquireDb, session_id: UUID, request_id: UUID
) -> dict[str, Any]:
    """Read the eligibility for one request and store it; returns what was stored."""
    async with acquire() as conn:
        row = (await conn.execute(_SESSION, {"sid": session_id})).first()
        await conn.rollback()
    if row is None:
        return {}  # the session is gone: nobody waits for a row (and the FK refuses one)
    out: dict[str, Any] = {"ok": False, "reason": None, "ns": None, "modules": None}
    tag = source.rag_opt_out_tag
    if tag is None:
        out["reason"] = "rag_off"
    elif row.state not in ("ACTIVE", "OUTAGE"):
        out["reason"] = "session_not_live"
    elif not row.rag_on:
        # the teacher turned RAG OFF (or the attestation is gone) after this session started:
        # no material from the next turn on (ARCH-072: disabled for new turns, audited)
        out["reason"] = "rag_withdrawn"
    else:
        target = StartTarget(
            session_id=session_id,
            site_id=row.site_id,
            cmid=int(row.cmid),
            student_userid=int(row.student_userid),
            courseid_hint=row.courseid_hint,
            activity_type=str(row.activity_type),
            session_no=int(row.session_no),
            flags={},
        )
        try:
            got = await source.course_material(target)
        except Exception as exc:  # a reader bug is "unreadable", never a guess
            log.warning("assess_material_read_error", error_type=type(exc).__name__)
            got, out["reason"] = None, "error"
        if got is None:
            out["reason"] = out["reason"] or "course_unresolved"
        else:
            ref, modules = got
            if isinstance(modules, Unreadable):
                out["reason"] = "modules_unreadable"
            else:
                out["ok"] = True
                out["ns"] = course_namespace(ref.course_shortname, ref.course_id)
                out["modules"] = [
                    [m.cmid, m.title] for m in eligible_modules(modules.value, tag)
                ]
    async with acquire() as conn:
        async with conn.begin():
            await conn.execute(
                _INSERT,
                {
                    "id": request_id,
                    "sid": session_id,
                    "site": row.site_id,
                    "ok": out["ok"],
                    "reason": out["reason"],
                    "ns": out["ns"],
                    "modules": None
                    if out["modules"] is None
                    else json.dumps(out["modules"]),
                },
            )
    log.info(
        "assess_material_read",
        session_id=str(session_id),
        ok=out["ok"],
        reason_code=out["reason"],
        count=len(out["modules"] or ()),
    )
    return out


async def sweep_material_reads(
    acquire: AcquireDb, *, age_s: int = READ_RETENTION_S
) -> int:
    """Delete eligibility rows nobody picked up (a turn that timed out, an API that died)."""
    async with acquire() as conn:
        async with conn.begin():
            deleted = (await conn.execute(_SWEEP, {"age": age_s})).rowcount or 0
    if deleted:
        log.info("assess_material_reads_swept", count=deleted)
    return int(deleted)


def make_material_handler(
    source: MaterialSource,
) -> Callable[[JobContext], Awaitable[None]]:
    async def handle(ctx: JobContext) -> None:
        try:
            sid = UUID(str(ctx.args["session_id"]))
            rid = UUID(str(ctx.args["request_id"]))
        except (KeyError, ValueError):
            return  # a malformed job is a stale no-op
        await read_material(source, ctx.acquire, sid, rid)

    return handle
