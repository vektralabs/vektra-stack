"""Render-time gate evaluation (REQ-068, BR-006 "evaluated at render AND at Start").

``render_check`` / ``gate_refresh`` jobs (args: site id, student user id, cmid, optional course
hint) evaluate the SAME ``evaluate_start_gates`` the pre-flight uses, on fresh reads, and upsert:

- ``assess_render_state``: the advisory REQ-068 state and the content-free reason code;
- ``assess_gate_inputs``: ``{"code", "state", "opens_at", "session_no_next"}``, plus (plan 11b)
  ``moodle_reasons``: every failing gate code that needs Moodle facts (``gates.DB_SIDE``
  excluded), present only when the Moodle half was actually evaluated, with ``attested_at`` (the
  REQ-091 date it was evaluated with), which a teacher reset (REQ-087) re-checks against while
  that date is unchanged; plus (plan 10)
  ``graders`` (the course's grader user ids, for the API's fresh grader check) when Moodle could
  be read, and ``recipients`` (``{role: "recipient", name: "m***@domain"}``: the transcript
  recipients of the sealed teacher config, local part masked, REQ-082 as amended) when the config
  could be opened. The API never sees a full address. ``opens_at``
  is set only when the date may be shown to THIS student (the activity's own close or the
  student's own override): the latest close of the cohort, which may be another student's
  extension, is never stored here, so ``GET /state`` cannot leak it.

The ``gate_refresh`` tick re-queues a render check for every (site, user, cmid) asked about in the
last day whose state is older than the freshness window, and deletes what nobody asked about for
7 days (render rows, gate inputs, done render checks).
"""

from __future__ import annotations

import json
import uuid
from collections.abc import Awaitable, Callable, Sequence
from dataclasses import dataclass
from typing import Any, Protocol
from uuid import UUID

import structlog
from sqlalchemy import text

from vektra_assess.config import VektraAssessSettings
from vektra_assess.delivery.recipients import ConfigRecipients, GraderLike, state_view
from vektra_assess.gates import (
    DB_SIDE,
    ButtonState,
    Code,
    GateDecision,
    evaluate_start_gates,
)
from vektra_assess.jobs import JobKind, enqueue_job, render_dedupe_key
from vektra_assess.worker.preflight import (
    DbGateFacts,
    MoodleGateFacts,
    PrivacyCheck,
    StartSource,
    StartTarget,
    gate_inputs,
    privacy_not_configured,
)
from vektra_assess.worker.runner import AcquireDb, JobContext

log = structlog.get_logger(__name__)

RETENTION_DAYS = 7

_DB = text(
    "SELECT c.activity_type, c.enabled, c.cap_minutes, c.flags, "
    "extract(epoch FROM c.end_at)::bigint AS end_at, "
    "extract(epoch FROM c.attested_opening_at)::bigint AS attested_at, "
    "extract(epoch FROM now())::bigint AS db_now, "
    "(SELECT count(*) FROM assess_sessions h WHERE h.site_id = c.site_id AND h.cmid = c.cmid "
    "AND h.student_userid = :u AND h.state = 'CLOSED') AS closed_sessions, "
    "(SELECT count(*) FROM assess_sessions h WHERE h.site_id = c.site_id AND h.cmid = c.cmid "
    "AND h.student_userid = :u AND h.state <> 'CLOSED') AS open_sessions, "
    "(SELECT count(*) FROM assess_reset_grants g WHERE g.site_id = c.site_id AND g.cmid = c.cmid "
    "AND g.student_userid = :u) AS reset_grants "
    "FROM assess_activity_config c WHERE c.site_id = :s AND c.cmid = :c"
)
_UPSERT_STATE = text(
    "INSERT INTO assess_render_state (site_id, student_userid, cmid, status, reason_code, "
    "computed_at) VALUES (:s, :u, :c, :st, :r, now()) ON CONFLICT (site_id, student_userid, cmid) "
    "DO UPDATE SET status = EXCLUDED.status, reason_code = EXCLUDED.reason_code, "
    "computed_at = now()"
)
#: ``refreshed_at`` is the database clock read BEFORE the Moodle reads (``_CLOCK``), as the
#: teacher_gate job does: a slow read never makes its facts look fresher than they are (a reset
#: and the grader checks compare it with the same clock)
_CLOCK = text("SELECT clock_timestamp()")
_UPSERT_INPUTS = text(
    "INSERT INTO assess_gate_inputs (site_id, student_userid, cmid, inputs, refreshed_at) "
    "VALUES (:s, :u, :c, CAST(:i AS jsonb), coalesce(:t, now())) "
    "ON CONFLICT (site_id, student_userid, cmid) "
    "DO UPDATE SET inputs = EXCLUDED.inputs, refreshed_at = EXCLUDED.refreshed_at"
)
_DONE = text(
    "UPDATE assess_render_checks SET done_at = now() WHERE site_id = :s "
    "AND student_userid = :u AND cmid = :c AND done_at IS NULL"
)


class GradersSource(Protocol):
    async def graders(self, target: StartTarget) -> Sequence[GraderLike] | None: ...


@dataclass(frozen=True)
class RenderDeps:
    settings: VektraAssessSettings
    source: StartSource
    privacy: PrivacyCheck = privacy_not_configured
    #: the course's graders (plan 10): ``graders`` (user ids, for the API's grader check) in
    #: gate_inputs; None = not read
    graders: GradersSource | None = None
    #: the configured transcript recipients (sealed teacher config): ``recipients``, masked
    recipients: ConfigRecipients | None = None


@dataclass(frozen=True)
class GradersRead:
    graders: tuple[int, ...]


async def evaluate_render(
    deps: RenderDeps,
    acquire: AcquireDb,
    site_id: UUID,
    userid: int,
    cmid: int,
    courseid_hint: int | None = None,
    seen: dict[str, Any] | None = None,
) -> tuple[GateDecision, StartTarget | None]:
    """The advisory decision and the target it was evaluated for (None: no configuration row).

    The target carries a scratch session id: the key of the source's resolution cache, so the
    grader read that follows reuses the course the gates resolved; the handler forgets it after.
    """
    settings = deps.settings
    async with acquire() as conn:
        act = (await conn.execute(_DB, {"s": site_id, "u": userid, "c": cmid})).first()
        privacy = await deps.privacy(conn, site_id) or (
            settings.privacy_allow_unset and settings.env == "development"
        )
    if act is None:
        return GateDecision(Code.NOT_ENABLED, ButtonState.NOT_ENABLED), None
    if seen is not None:
        # the REQ-091 date this evaluation used: a reset takes the Moodle half only while the
        # date is still the same
        seen["attested_at"] = act.attested_at
    target = StartTarget(
        session_id=uuid.uuid4(),  # no session: a scratch key for the source's resolution cache
        site_id=site_id,
        cmid=cmid,
        student_userid=userid,
        courseid_hint=courseid_hint,
        activity_type=str(act.activity_type),
        session_no=int(act.closed_sessions) + 1,
        flags=dict(act.flags or {}),
    )
    if not act.enabled or act.cap_minutes is None:
        return GateDecision(Code.NOT_ENABLED, ButtonState.NOT_ENABLED), target
    if int(act.open_sessions):
        return GateDecision(Code.SESSION_OPEN, ButtonState.ACTIVE), target
    db = DbGateFacts(
        now=int(act.db_now),
        end_at=act.end_at,
        teacher_opening_at=act.attested_at,
        closed_sessions=int(act.closed_sessions),
        reset_grants=int(act.reset_grants),
        privacy_configured=bool(privacy),
    )
    try:
        facts = await deps.source.gate_facts(target)
    except Exception as exc:  # unreadable, never a guess
        log.warning("assess_render_facts_error", error_type=type(exc).__name__)
        facts = MoodleGateFacts()
    return evaluate_start_gates(gate_inputs(target, db, facts)), target


async def read_graders(deps: RenderDeps, target: StartTarget) -> GradersRead | None:
    """The course's graders (who may download and resend), for the activity's REAL type (a
    quiz's cmid is resolved among the course's quizzes). Read even when the activity is disabled
    or a session is open: a teacher still downloads earlier transcripts. None when no source is
    configured or Moodle could not be read."""
    if deps.graders is None:
        return None
    try:
        graders = await deps.graders.graders(target)
    except Exception as exc:
        log.warning("assess_render_graders_error", error_type=type(exc).__name__)
        return None
    if graders is None:
        return None
    return GradersRead(tuple(sorted({g.userid for g in graders})))


async def read_recipients(
    deps: RenderDeps, acquire: AcquireDb, site_id: UUID, cmid: int
) -> list[dict[str, str]] | None:
    """The configured recipients as ``/state`` shows them (masked); None when unreadable."""
    if deps.recipients is None:
        return None
    try:
        async with acquire() as conn:
            got = await deps.recipients.read(conn, site_id, cmid)
            await conn.rollback()
    except Exception as exc:
        log.warning("assess_render_recipients_error", error_type=type(exc).__name__)
        return None
    return state_view(got) if got is not None else None


async def store_render(
    acquire: AcquireDb,
    site_id: UUID,
    userid: int,
    cmid: int,
    decision: GateDecision,
    graders: GradersRead | None = None,
    recipients: list[dict[str, str]] | None = None,
    observed_at: Any = None,
    attested_at: Any = None,
) -> None:
    inputs: dict[str, Any] = {
        "code": decision.code.value,
        "state": decision.state.value,
        "opens_at": decision.opens_at,
        "session_no_next": decision.session_no_next,
    }
    # the full evaluation ran (not the disabled / open-session short cut): an OK decision, or a
    # failing one with its reasons. The Moodle-side ones are what a reset re-checks (REQ-087).
    if decision.code is Code.OK or decision.reasons:
        inputs["moodle_reasons"] = [
            c.value for c in decision.reasons if c not in DB_SIDE
        ]
        inputs["attested_at"] = attested_at
    if graders is not None:
        # user ids for the grader check (plan 10 authz)
        inputs["graders"] = list(graders.graders)
    if recipients is not None:
        # role + masked address for the pre-session screen: never a full address
        inputs["recipients"] = recipients
    params = {"s": site_id, "u": userid, "c": cmid}
    async with acquire() as conn:
        async with conn.begin():
            await conn.execute(
                _UPSERT_STATE,
                {**params, "st": decision.state.value, "r": decision.code.value},
            )
            await conn.execute(
                _UPSERT_INPUTS,
                {**params, "i": json.dumps(inputs, sort_keys=True), "t": observed_at},
            )
            await conn.execute(_DONE, params)


def make_render_handler(deps: RenderDeps) -> Callable[[JobContext], Awaitable[None]]:
    async def handle(ctx: JobContext) -> None:
        try:
            site_id = UUID(str(ctx.args["site_id"]))
            userid = int(ctx.args["student_userid"])
            cmid = int(ctx.args["cmid"])
        except (KeyError, ValueError):
            return  # a malformed job is a stale no-op
        hint = ctx.args.get("courseid")
        course = int(hint) if hint else None
        async with ctx.acquire() as conn:
            observed_at = (await conn.execute(_CLOCK)).scalar()
            await conn.rollback()
        seen: dict[str, Any] = {}
        decision, target = await evaluate_render(
            deps, ctx.acquire, site_id, userid, cmid, course, seen
        )
        try:
            graders = await read_graders(deps, target) if target else None
        finally:
            # the scratch key must not stay in the source's bounded resolution cache
            forget = getattr(deps.source, "forget", None)
            if target is not None and callable(forget):
                forget(target.session_id)
        recipients = await read_recipients(deps, ctx.acquire, site_id, cmid)
        await store_render(
            ctx.acquire,
            site_id,
            userid,
            cmid,
            decision,
            graders,
            recipients,
            observed_at=observed_at,
            attested_at=seen.get("attested_at"),
        )

    return handle


_STALE_ASKED = text(
    "SELECT DISTINCT rc.site_id, rc.student_userid, rc.cmid FROM assess_render_checks rc "
    "LEFT JOIN assess_render_state rs ON rs.site_id = rc.site_id "
    "AND rs.student_userid = rc.student_userid AND rs.cmid = rc.cmid "
    "WHERE rc.requested_at > now() - interval '1 day' "
    "AND (rs.computed_at IS NULL OR rs.computed_at < now() - make_interval(secs => :fresh)) "
    "LIMIT :n"
)
_CLEANUP = (
    "DELETE FROM assess_render_checks WHERE done_at IS NOT NULL "
    f"AND requested_at < now() - interval '{RETENTION_DAYS} days'",
    "DELETE FROM assess_render_state rs WHERE rs.computed_at < now() - "
    f"interval '{RETENTION_DAYS} days' AND NOT EXISTS (SELECT 1 FROM assess_render_checks rc "
    "WHERE rc.site_id = rs.site_id AND rc.student_userid = rs.student_userid "
    f"AND rc.cmid = rs.cmid AND rc.requested_at > now() - interval '{RETENTION_DAYS} days')",
    "DELETE FROM assess_gate_inputs gi WHERE gi.refreshed_at < now() - "
    f"interval '{RETENTION_DAYS} days' AND NOT EXISTS (SELECT 1 FROM assess_render_checks rc "
    "WHERE rc.site_id = gi.site_id AND rc.student_userid = gi.student_userid "
    f"AND rc.cmid = gi.cmid AND rc.requested_at > now() - interval '{RETENTION_DAYS} days')",
)


async def gate_refresh_tick(
    acquire: AcquireDb, fresh_s: int, *, batch: int = 200
) -> dict[str, Any]:
    """Re-queue stale render checks of recently asked pairs; delete week-old unused rows."""
    queued = deleted = 0
    async with acquire() as conn:
        async with conn.begin():
            rows = (
                await conn.execute(_STALE_ASKED, {"fresh": fresh_s, "n": batch})
            ).all()
            for r in rows:
                queued += await enqueue_job(
                    conn,
                    JobKind.GATE_REFRESH,
                    {
                        "site_id": str(r.site_id),
                        "student_userid": int(r.student_userid),
                        "cmid": int(r.cmid),
                    },
                    dedupe_key=render_dedupe_key(r.site_id, r.student_userid, r.cmid),
                )
            for stmt in _CLEANUP:
                res = await conn.execute(text(stmt))
                deleted += int(res.rowcount or 0)
    if queued or deleted:
        log.info("assess_gate_refresh", queued=queued, deleted=deleted)
    return {"queued": queued, "deleted": deleted}
