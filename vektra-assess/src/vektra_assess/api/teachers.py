"""Is this teacher an EDITOR of this activity, NOW? (plan assess-teacher-ops, INT-001, REQ-077.)

The plugin names the acting Moodle user (``actor_userid``); Vektra never trusts that claim on its
own. It reads the activity-level facts the worker last wrote into ``assess_teacher_gate`` for
(site, cmid) (``worker.teacher_gate``: the cmid resolved inside the hinted course, its editors,
graders and REQ-077 enable facts) and accepts them only while at most ``max_age_s`` old.

- absent, stale, or the editor list unreadable: refused (fail closed) and a ``teacher_gate`` job
  is queued for the given course / type hint, so a retry a few seconds later can succeed; with
  no usable hint nothing can be queued: ``HINT_MISSING`` (the route answers 400, it never
  promises a check that was not queued);
- fresh but the cmid was not found in the hinted course (``activity_not_in_course``, the one
  definitive unresolved answer): refused, nothing queued; when the hint differs from the one that
  failed, or the row records a transient failure (site, reader, Moodle), a refresh is queued and
  the request is told to retry (a wrong hint never blocks a right one for long);
- fresh and the actor is not listed: refused, nothing queued (Moodle must change first).

The same row answers both roles: ``editors`` (``moodle/course:manageactivities``: the
configuration, REQ-077) and, since 11b, ``graders`` (``mod/assign:grade``: the session list,
reset, terminate, extra time and opening fallback, REQ-076). ``can_view_overrides`` reads the
``override_viewers`` list of the same row (fail closed: absent or unreadable is False). The
grader check of the transcript routes stays ``api.graders`` (per student, plan 10).

``verify_teacher`` is the one route-level entry: the check, the audited refusal
(``assess_teacher_unverified``), and the refusal envelope to raise AFTER the transaction commits
(so the audit row and the queued refresh survive the refusal).
"""

from __future__ import annotations

from dataclasses import dataclass, field
from enum import StrEnum
from typing import Any, Literal
from uuid import UUID

from fastapi import HTTPException, Request
from sqlalchemy import text

from vektra_assess.api.common import fail
from vektra_assess.audit import AssessAuditEvent, write_event
from vektra_assess.errors import bad_request, teacher_unverified
from vektra_assess.jobs import JobKind, enqueue_job, teacher_gate_dedupe_key

KINDS = ("quiz", "assign")
#: the only unresolved outcome that is an answer: the worker read the hinted course and the cmid
#: is not among its activities of that type (``moodle.readers.resolve.resolve_in_course``). Every
#: other unresolved reason (site unknown, reader error, Moodle unreachable) is transient: retried.
NOT_IN_COURSE = "activity_not_in_course"

_GATE = text(
    "SELECT inputs, extract(epoch FROM now() - refreshed_at) AS age_s "
    "FROM assess_teacher_gate WHERE site_id = :s AND cmid = :c"
)


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

    @property
    def ok(self) -> bool:
        return self.check is TeacherCheck.OK


def usable_hint(courseid: int | None, kind: str | None) -> bool:
    return bool(courseid) and int(courseid or 0) >= 1 and kind in KINDS


async def queue_teacher_gate(
    conn: Any, site: UUID, cmid: int, courseid: int | None, kind: str | None
) -> bool:
    """Queue a refresh for this hint (False: a refresh for it is already pending). The caller
    checks ``usable_hint`` first: without one nothing can be resolved."""
    if courseid is None or kind is None or not usable_hint(courseid, kind):
        raise ValueError("a teacher_gate refresh needs a course and type hint")
    return await enqueue_job(
        conn,
        JobKind.TEACHER_GATE,
        {
            "site_id": str(site),
            "cmid": int(cmid),
            "courseid": int(courseid),
            "activity_type": str(kind),
        },
        dedupe_key=teacher_gate_dedupe_key(site, cmid, int(courseid), str(kind)),
    )


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
    row = (await conn.execute(_GATE, {"s": site, "c": int(cmid)})).first()
    facts = dict(row.inputs) if row is not None and isinstance(row.inputs, dict) else {}
    fresh = row is not None and row.age_s is not None and float(row.age_s) <= max_age_s

    async def refresh() -> TeacherGate:
        if not usable_hint(courseid_hint, kind_hint):
            return TeacherGate(TeacherCheck.HINT_MISSING)
        await queue_teacher_gate(conn, site, cmid, courseid_hint, kind_hint)
        return TeacherGate(TeacherCheck.STALE)

    if not fresh:
        return await refresh()
    if facts.get("resolved") is not True:
        # a definitive answer for THIS hint: not a teacher of anything here. Another hint, or a
        # transient failure: ask again (dedupe per hint)
        if (
            facts.get("hint") == [courseid_hint, kind_hint]
            and facts.get("reason") == NOT_IN_COURSE
        ):
            return TeacherGate(refused)
        return await refresh()
    members = facts.get(_ROLE_KEY[role])
    if not isinstance(members, list):
        return (
            await refresh()
        )  # unreadable (or written before the role was read): ask again
    if int(actor_userid) not in {
        int(e) for e in members if isinstance(e, int) and not isinstance(e, bool)
    }:
        return TeacherGate(refused, facts)
    return TeacherGate(TeacherCheck.OK, facts)


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
    return gate, fail(
        teacher_unverified(stale=gate.check is TeacherCheck.STALE, role=role)
    )
