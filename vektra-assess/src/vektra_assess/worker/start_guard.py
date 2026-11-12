"""After-rollback and after-commit rules of Start (REQ-071 abuse cap, plan 8 M5.4).

Both run in the caller's transaction (the rollback's or the commit's), so the notification jobs,
the breaker state and their audit rows exist exactly when the outcome does.

- **3 consecutive system rollbacks** for one (student, activity): the third enqueues the
  ``teacher_notify`` and ``operator_notify`` jobs and audits ``assess_rollback_cap_hit`` (once per
  streak). The student then reads "Problema tecnico persistente" (the event stream maps the
  rollback reason to ``technical_problem_persistent`` while the streak lasts, see
  ``api.events``).
- **Per-site Start breaker**: ``start_breaker_rollbacks`` system rollbacks of one site within
  ``start_breaker_window_s`` open it (``assess_start_breaker``, written by the worker only;
  ``assess_start_breaker_open`` audited on the change). The API reads it and fast-refuses Starts
  without consuming anything; after ``start_breaker_cooldown_s`` it is half-open: the API lets one
  Start through at a time (none STARTING on the site). A commit closes it
  (``assess_start_breaker_close``); another system rollback re-opens it for a new cooldown.

System rollbacks are the server-attributable ones; a gate refusal, a missing configuration or
the admission limit is not a technical problem and never counts.
"""

from __future__ import annotations

import time
from dataclasses import dataclass
from typing import Any
from uuid import UUID

from sqlalchemy import text

from vektra_assess.audit import AssessAuditEvent, write_event
from vektra_assess.jobs import JobKind, enqueue_job
from vektra_assess.start_rules import (
    SYSTEM_ROLLBACK_REASONS,
    SYSTEM_ROLLBACK_SQL,
    is_system_rollback,
)


@dataclass(frozen=True)
class StartLimits:
    rollback_notify_after: int = 3
    breaker_rollbacks: int = 10
    breaker_window_s: int = 300
    breaker_cooldown_s: int = 120

    @classmethod
    def from_settings(cls, s: Any) -> StartLimits:
        return cls(
            rollback_notify_after=s.rollback_notify_after,
            breaker_rollbacks=s.start_breaker_rollbacks,
            breaker_window_s=s.start_breaker_window_s,
            breaker_cooldown_s=s.start_breaker_cooldown_s,
        )


_LAST_OUTCOMES = text(
    "SELECT outcome, reason_code FROM assess_start_attempts "
    "WHERE site_id = :s AND cmid = :c AND student_userid = :u "
    "AND outcome IN ('committed', 'rolled_back') ORDER BY id DESC LIMIT :n"
)
_SITE_ROLLBACKS = text(
    "SELECT count(*) FROM assess_start_attempts WHERE site_id = :s "
    "AND outcome = 'rolled_back' AND " + SYSTEM_ROLLBACK_SQL + " "
    "AND created_at > now() - make_interval(secs => :w)"
)
_SITE_LOCK = text(
    "SELECT pg_advisory_xact_lock(hashtextextended('assess_start_breaker:' || "
    "CAST(:s AS text), 0))"
)
_PROBE_FAILED = text(
    "UPDATE assess_start_breaker SET opened_at = now(), "
    "half_open_at = now() + make_interval(secs => :cd), updated_at = now() "
    "WHERE site_id = :s AND state = 'open' AND half_open_at <= now()"
)
_OPEN = text(
    "INSERT INTO assess_start_breaker (site_id, state, opened_at, half_open_at, updated_at) "
    "VALUES (:s, 'open', now(), now() + make_interval(secs => :cd), now()) "
    "ON CONFLICT (site_id) DO UPDATE SET state = 'open', opened_at = now(), "
    "half_open_at = now() + make_interval(secs => :cd), updated_at = now() "
    "RETURNING (xmax = 0) AS inserted"
)
_STATE = text("SELECT state FROM assess_start_breaker WHERE site_id = :s FOR UPDATE")
#: only the half-open probe closes the breaker: a Start requested after the cooldown ended
#: (an older Start admitted before the breaker opened keeps the cooldown when it commits)
_CLOSE = text(
    "UPDATE assess_start_breaker SET state = 'closed', updated_at = now() "
    "WHERE site_id = :s AND state = 'open' AND half_open_at <= "
    "(SELECT created_at FROM assess_sessions WHERE id = :sid)"
)


async def streak(conn: Any, site_id: UUID, cmid: int, userid: int, n: int) -> int:
    """Consecutive system rollbacks at the end of the (student, activity) history, up to ``n``."""
    rows = (
        await conn.execute(
            _LAST_OUTCOMES, {"s": site_id, "c": cmid, "u": userid, "n": n}
        )
    ).all()
    count = 0
    for outcome, reason in rows:
        if outcome != "rolled_back" or not is_system_rollback(reason):
            break
        count += 1
    return count


#: rollback reasons that mean the pre-flight could not read the LMS (unreachable, timeout, a
#: client error: the readers turn all of them into Unreadable, which surfaces as these codes)
LMS_UNREACHABLE_REASONS = frozenset(
    {
        "unreadable",
        "capture_failed_policy_input_unreadable",
        "capture_failed_attempt_unreadable",
        "capture_failed_submission_unreadable",
    }
)
_ALERTED = text("SELECT 1 FROM assess_jobs WHERE dedupe_key = :k LIMIT 1")
#: one operator alert per site per window, however many Starts roll back in it
LMS_ALERT_WINDOW_S = 900


async def alert_lms_unreachable(
    conn: Any, site_id: UUID, reason: str, *, now: float | None = None
) -> None:
    """REQ-071 "operator alerted on WS failure": a content-free ``operator_notify`` job in the
    rollback's own transaction (delivery: plan transcript-delivery). Deduplicated per site by a
    fixed 15-minute bucket of the worker clock in the job's ``dedupe_key``: a burst of Starts
    against an unreachable LMS queues one alert per site per window; the next window alerts
    again while the failure lasts."""
    bucket = int((time.time() if now is None else now) // LMS_ALERT_WINDOW_S)
    key = f"operator_notify:lms_unreachable:{site_id}:{bucket}"
    # the unique dedupe index covers pending jobs only; an alert already delivered in this
    # window (state done) must not be sent again either
    if (await conn.execute(_ALERTED, {"k": key})).first() is not None:
        return
    await enqueue_job(
        conn,
        JobKind.OPERATOR_NOTIFY,
        {"site_id": str(site_id), "reason": reason, "kind": "lms_unreachable"},
        dedupe_key=key,
    )


async def after_rollback(
    conn: Any,
    *,
    site_id: UUID,
    cmid: int,
    userid: int,
    reason: str,
    limits: StartLimits,
) -> None:
    if reason in LMS_UNREACHABLE_REASONS:
        await alert_lms_unreachable(conn, site_id, reason)
    if not is_system_rollback(reason):
        return
    # the streak, counted over one more than the threshold: notify when it reaches it exactly
    n = limits.rollback_notify_after
    if await streak(conn, site_id, cmid, userid, n + 1) == n:
        args: dict[str, str | int] = {
            "site_id": str(site_id),
            "cmid": cmid,
            "student_userid": userid,
            "reason": "rollback_cap",
        }
        for kind in (JobKind.TEACHER_NOTIFY, JobKind.OPERATOR_NOTIFY):
            await enqueue_job(conn, kind, args)
        await write_event(
            conn,
            AssessAuditEvent.ROLLBACK_CAP_HIT,
            actor="worker",
            site_id=site_id,
            reason_code=reason,
            data={"cmid": cmid, "streak": n},
        )
    # serialise the per-site count through commit: two rollbacks
    # reaching the threshold together would otherwise each miss the other's uncommitted row.
    # The same per-site key as the API's half-open Start check; held until this commit, and
    # the count below is a new statement (a new snapshot) taken after the lock
    await conn.execute(_SITE_LOCK, {"s": str(site_id)})
    # half-open (the cooldown is over): only one Start at a time goes through, so a system
    # rollback now is the probe failing; the cooldown restarts whatever the window count
    # says
    await conn.execute(_PROBE_FAILED, {"s": site_id, "cd": limits.breaker_cooldown_s})
    recent = int(
        (
            await conn.execute(
                _SITE_ROLLBACKS,
                {
                    "s": site_id,
                    "sys": list(SYSTEM_ROLLBACK_REASONS),
                    "w": limits.breaker_window_s,
                },
            )
        ).scalar_one()
    )
    if recent < limits.breaker_rollbacks:
        return
    before = (await conn.execute(_STATE, {"s": site_id})).scalar()
    await conn.execute(_OPEN, {"s": site_id, "cd": limits.breaker_cooldown_s})
    if before != "open":
        await write_event(
            conn,
            AssessAuditEvent.START_BREAKER_OPEN,
            actor="worker",
            site_id=site_id,
            reason_code=reason,
            data={"rollbacks": recent, "window_s": limits.breaker_window_s},
        )


async def after_commit(conn: Any, site_id: UUID, session_id: UUID) -> None:
    """A committed half-open probe closes the site's breaker."""
    if (await conn.execute(_CLOSE, {"s": site_id, "sid": session_id})).rowcount:
        await write_event(
            conn, AssessAuditEvent.START_BREAKER_CLOSE, actor="worker", site_id=site_id
        )
