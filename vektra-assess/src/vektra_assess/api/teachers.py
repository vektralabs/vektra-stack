"""Is this teacher an EDITOR of this activity, NOW? (plan assess-teacher-ops, INT-001, REQ-077.)

The plugin names the acting LMS user (``actor_userid``); Vektra never trusts that claim on its
own. It reads the activity-level facts the worker last wrote into ``assess_teacher_gate`` for
(site, cmid) (``worker.teacher_gate``: the cmid resolved inside the hinted course, its editors,
graders and REQ-077 enable facts) and accepts them only while at most ``max_age_s`` old.

- absent, stale, or the editor list unreadable: refused (fail closed) and a ``teacher_gate`` job
  is queued for the given course / type hint, so a retry a few seconds later can succeed; with
  no usable hint nothing can be queued: ``HINT_MISSING`` (the route answers 400, it never
  promises a check that was not queued);
- fresh but the cmid was not found in the hinted course (``activity_not_in_course``, the one
  definitive unresolved answer): refused, nothing queued; when the hint differs from the one that
  failed, or the row records a transient failure (site, reader, LMS), a refresh is queued and
  the request is told to retry (a wrong hint never blocks a right one for long);
- fresh and the actor is not listed: refused, nothing queued (the LMS must change first).

The same row answers both roles: ``editors`` (the LMS's activity-editor capability: the
configuration, REQ-077) and, since 11b, ``graders`` (``mod/assign:grade``: the session list,
reset, terminate, extra time and opening fallback, REQ-076). ``can_view_overrides`` reads the
``override_viewers`` list of the same row (fail closed: absent or unreadable is False). The
grader check of the transcript routes stays ``api.graders`` (per student, plan 10).

``verify_teacher`` is the one route-level entry: the check, the audited refusal
(``assess_teacher_unverified``), and the refusal envelope to raise AFTER the transaction commits
(so the audit row and the queued refresh survive the refusal).

Freshness for the plugin (live E2E follow-up, 2026-11-13). The row is a cache, so it can be
older than a change the teacher just made in the LMS (a cut-off date set a moment ago):

- ``POST /activities/{cmid}/gate-refresh`` (``api.teacher_config``) lets the plugin ask for a
  refresh when a teacher page opens, so the first click finds fresh facts;
- a refusal that a refresh may lift (stale or absent facts; an enable refusal decided on an LMS
  fact read more than ``FACT_FRESH_S`` ago) queues the refresh and says when to retry, in
  ``error.details.retry_after_s`` (``RETRY_AFTER_S``, never above ``errors.RETRY_AFTER_MAX_S``)
  and the ``Retry-After`` header. The queued job wakes the worker at once (``NOTIFY``), so the
  facts are normally re-read before the retry. A refusal decided on a fact read in the last
  ``FACT_FRESH_S`` is an answer: it carries no retry. The API role cannot write this table
  (SELECT only), so "invalidating" a fact means exactly that: the refusal is not final until a
  refresh newer than ``FACT_FRESH_S`` has answered. Fail closed throughout: a stale fact is never
  used to ACCEPT anything the freshness bound did not accept before.

Review of #221. A retry is promised only when a refresh that surely reads AFTER the refusal was
queued by it: the API cannot see whether a pending job is ``ready`` (it covers the change) or
``leased`` (it may have read before), so a refusal queues the refresh, or beside a pending one a
single follow-up (``jobs.teacher_gate_followup_dedupe_key``), and promises a retry only when one
of the two was inserted now. With both pending (a worker that is down or slow) nothing is queued
and the refusal is final: at most two retry hints in a row without a fresh read. A resolved row
is refreshed with ITS course and type (the worker's own resolution), never a request's hint: a
wrong course in a request cannot turn the activity's facts into ``activity_not_in_course``.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from enum import StrEnum
from typing import Any, Literal
from uuid import UUID

import structlog
from fastapi import HTTPException, Request
from sqlalchemy import text

from vektra_assess.api.common import fail
from vektra_assess.audit import AssessAuditEvent, write_event
from vektra_assess.errors import (
    bad_request,
    retry_headers,
    teacher_unverified,
)
from vektra_assess.jobs import (
    JobKind,
    enqueue_job,
    teacher_gate_dedupe_key,
    teacher_gate_followup_dedupe_key,
)

log = structlog.get_logger(__name__)

KINDS = ("quiz", "assign")
#: the only unresolved outcome that is an answer: the worker read the hinted course and the cmid
#: is not among its activities of that type (the provider's in-course resolution). Every
#: other unresolved reason (site unknown, reader error, LMS unreachable) is transient: retried.
NOT_IN_COURSE = "activity_not_in_course"

#: the one read of an activity's cached facts (the role checks and ``gate-refresh``)
GATE_ROW = text(
    "SELECT inputs, refreshed_at, extract(epoch FROM now() - refreshed_at) AS age_s "
    "FROM assess_teacher_gate WHERE site_id = :s AND cmid = :c"
)

#: how long after a refusal that queued a refresh the plugin should retry: the job wakes the
#: worker at once (NOTIFY) and one teacher_gate read is a handful of LMS calls
RETRY_AFTER_S = 3
#: facts read at most this long ago are an ANSWER: a refusal decided on them carries no retry
#: and queues nothing. Above ``RETRY_AFTER_S``, so the retry a refusal asked for normally finds
#: the refreshed row inside this window and gets a final answer (no retry loop)
FACT_FRESH_S = 5
#: ``gate-refresh`` coalescing: a row of the activity this recent is not read again
REFRESH_MIN_INTERVAL_S = 10


Role = Literal["editor", "grader"]
#: the facts key listing the users of each role
_ROLE_KEY: dict[str, str] = {"editor": "editors", "grader": "graders"}


class TeacherCheck(StrEnum):
    OK = "ok"
    NOT_AN_EDITOR = "not_an_editor"
    NOT_A_GRADER = "not_a_grader"
    STALE = "stale"  # absent, older than the bound, or unreadable: refresh queued
    # a refresh is needed but the request gives no usable course / type hint: nothing could be
    # queued, so the caller must not be told to retry (400, not "a check has been queued")
    HINT_MISSING = "hint_missing"


@dataclass(frozen=True)
class TeacherGate:
    """The outcome and, when fresh, the facts (ids and flags only)."""

    check: TeacherCheck
    facts: dict[str, Any] = field(default_factory=dict)
    #: the row's age in seconds when it was checked (None: no row)
    age_s: float | None = None
    #: a refresh that reads after this check was queued by it (STALE): a retry may pass
    refresh_queued: bool = False

    @property
    def ok(self) -> bool:
        return self.check is TeacherCheck.OK

    @property
    def just_read(self) -> bool:
        """The facts were read in the last ``FACT_FRESH_S``: a refusal on them is final."""
        return self.age_s is not None and self.age_s <= FACT_FRESH_S


def usable_hint(courseid: int | None, kind: str | None) -> bool:
    return bool(courseid) and int(courseid or 0) >= 1 and kind in KINDS


async def queue_teacher_gate(
    conn: Any,
    site: UUID,
    cmid: int,
    courseid: int | None,
    kind: str | None,
    *,
    follow_up: bool = False,
) -> bool:
    """Queue a refresh for this hint; True only when a job was inserted NOW (it reads after the
    caller's transaction). False: one is already pending. ``follow_up``: beside a pending one,
    queue the single follow-up (``teacher_gate_followup_dedupe_key``), so a refusal can be sure
    of a read after it even when the pending job is already running; False again when both are
    pending. The caller checks ``usable_hint`` first: without one nothing can be resolved."""
    if courseid is None or kind is None or not usable_hint(courseid, kind):
        raise ValueError("a teacher_gate refresh needs a course and type hint")
    args: dict[str, str | int] = {
        "site_id": str(site),
        "cmid": int(cmid),
        "courseid": int(courseid),
        "activity_type": str(kind),
    }
    keys = [teacher_gate_dedupe_key(site, cmid, int(courseid), str(kind))]
    if follow_up:
        keys.append(
            teacher_gate_followup_dedupe_key(site, cmid, int(courseid), str(kind))
        )
    for key in keys:
        if await enqueue_job(conn, JobKind.TEACHER_GATE, args, dedupe_key=key):
            return True
    return False


def hint_of(facts: dict[str, Any]) -> tuple[int, str] | None:
    """The ``[courseid, type]`` hint a stored row answers (the worker's own record of what it
    resolved), or None when it carries no usable one."""
    hint = facts.get("hint")
    if (
        isinstance(hint, list)
        and len(hint) == 2
        and isinstance(hint[0], int)
        and not isinstance(hint[0], bool)
        and usable_hint(hint[0], hint[1])
    ):
        return int(hint[0]), str(hint[1])
    return None


def refresh_hint(
    facts: dict[str, Any], courseid: int | None, kind: str | None
) -> tuple[int, str] | None:
    """The hint a refresh of this row uses: a RESOLVED row's own (the activity's real course;
    a request's hint can only be wrong where it differs), else the request's when usable."""
    if facts.get("resolved") is True and (own := hint_of(facts)) is not None:
        return own
    if courseid is not None and kind is not None and usable_hint(courseid, kind):
        return int(courseid), str(kind)
    return None


async def check_fresh_teacher(
    conn: Any,
    *,
    site: UUID,
    cmid: int,
    actor_userid: int,
    max_age_s: int,
    courseid_hint: int | None,
    kind_hint: str | None,
    role: Role,
) -> TeacherGate:
    refused = (
        TeacherCheck.NOT_AN_EDITOR if role == "editor" else TeacherCheck.NOT_A_GRADER
    )
    row = (await conn.execute(GATE_ROW, {"s": site, "c": int(cmid)})).first()
    facts = dict(row.inputs) if row is not None and isinstance(row.inputs, dict) else {}
    age = float(row.age_s) if row is not None and row.age_s is not None else None
    fresh = age is not None and age <= max_age_s

    async def refresh() -> TeacherGate:
        hint = refresh_hint(facts, courseid_hint, kind_hint)
        if hint is None:
            return TeacherGate(TeacherCheck.HINT_MISSING)
        queued = await queue_teacher_gate(conn, site, cmid, *hint, follow_up=True)
        return TeacherGate(TeacherCheck.STALE, refresh_queued=queued)

    if not fresh:
        return await refresh()
    if facts.get("resolved") is not True:
        # a definitive answer for THIS hint: not a teacher of anything here. Another hint, or a
        # transient failure: ask again (dedupe per hint)
        if (
            facts.get("hint") == [courseid_hint, kind_hint]
            and facts.get("reason") == NOT_IN_COURSE
        ):
            return TeacherGate(refused, age_s=age)
        return await refresh()
    members = facts.get(_ROLE_KEY[role])
    if not isinstance(members, list):
        return (
            await refresh()
        )  # unreadable (or written before the role was read): ask again
    if int(actor_userid) not in {
        int(e) for e in members if isinstance(e, int) and not isinstance(e, bool)
    }:
        return TeacherGate(refused, facts, age)
    return TeacherGate(TeacherCheck.OK, facts, age)


def gate_hint(gate: TeacherGate) -> tuple[int, str] | None:
    """The hint the verified row answers (``hint_of`` its facts)."""
    return hint_of(gate.facts)


async def refresh_after_fact_refusal(
    engine: Any, site: UUID, cmid: int, gate: TeacherGate
) -> bool:
    """A refusal decided on an LMS fact of ``gate`` that was NOT just read: queue a refresh of
    the row (its own transaction: the refused request's rolls back) and return whether the
    caller may tell the plugin to retry: only when a job that reads after this refusal was
    inserted now (a pending one may already have read: ``queue_teacher_gate`` ``follow_up``).
    Never raises: a failed queue only means no retry."""
    if gate.just_read:
        return False
    hint = gate_hint(gate)
    if hint is None:
        return False
    try:
        async with engine.begin() as conn:
            return await queue_teacher_gate(conn, site, cmid, *hint, follow_up=True)
    except Exception as exc:  # the refusal stands; only the retry hint is lost
        log.warning("assess_teacher_gate_queue_failed", error_type=type(exc).__name__)
        return False


def can_view_overrides(gate: TeacherGate, actor_userid: int) -> bool:
    """REQ-076 gate panel: whether the actor may see WHO holds the override that sets the
    activity's opening (``override_viewers``: holders of the override-management capability,
    worker-read). Absent, unreadable or not listed: False (fail closed: the holder is omitted,
    never the reason, which is never stored at all)."""
    viewers = gate.facts.get("override_viewers") if gate.ok else None
    if not isinstance(viewers, list):
        return False
    return int(actor_userid) in {
        int(v) for v in viewers if isinstance(v, int) and not isinstance(v, bool)
    }


# --- route helpers ---------------------------------------------------------------------------


def ascii_id(raw: str) -> int | None:
    """A positive BIGINT written in ASCII digits only (``str.isdigit`` also accepts "²" or
    Arabic-Indic digits, which ``int`` then rejects or reinterprets)."""
    if not 0 < len(raw) <= 19 or not all("0" <= ch <= "9" for ch in raw):
        return None
    value = int(raw)
    return value if 0 < value < 2**63 else None


def path_id(raw: str, name: str) -> int:
    """A path id (``cmid``, ``userid``) or the 400 envelope naming it."""
    value = ascii_id(raw)
    if value is None:
        raise fail(bad_request(name))
    return value


def header_int(request: Request, name: str) -> int | None:
    return ascii_id(request.headers.get(name, ""))


async def verify_teacher(
    conn: Any,
    *,
    site: UUID,
    cmid: int,
    actor: int,
    courseid: int | None,
    kind: str | None,
    max_age_s: int,
    route: str,
    role: Role,
) -> tuple[TeacherGate, HTTPException | None]:
    """The fresh role check of one teacher request. On a refusal the ``teacher_unverified``
    audit row is written in the caller's transaction and the envelope is returned, to be raised
    once that transaction has committed."""
    gate = await check_fresh_teacher(
        conn,
        site=site,
        cmid=cmid,
        actor_userid=actor,
        max_age_s=max_age_s,
        courseid_hint=courseid,
        kind_hint=kind if kind in KINDS else None,
        role=role,
    )
    if gate.ok:
        return gate, None
    await write_event(
        conn,
        AssessAuditEvent.TEACHER_UNVERIFIED,
        actor="api",
        site_id=site,
        reason_code=gate.check.value,
        data={"cmid": cmid, "actor": actor, "route": route, "role": role},
    )
    if gate.check is TeacherCheck.HINT_MISSING:
        # nothing was queued: name the missing hint, never "retry in a few seconds"
        return gate, fail(bad_request("teacher_gate_hint"))
    stale = gate.check is TeacherCheck.STALE
    err = teacher_unverified(
        stale=stale,
        role=role,
        # a retry is promised only for a refresh this check queued (bounded: two at most)
        retry_after_s=RETRY_AFTER_S if stale and gate.refresh_queued else None,
    )
    return gate, fail(err, retry_headers(err))
