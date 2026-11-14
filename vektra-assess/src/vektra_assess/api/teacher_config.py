"""Teacher configuration of one activity (plan assess-teacher-ops, REQ-077, INT-001, ARCH-071).

``PUT /api/v1/assess/activities/{cmid}/config`` and ``GET`` the same path, ``assess:teacher`` keys
only (exact scope: an ``admin`` key is 403), bound to their site. The acting LMS user is
``actor_userid`` (body) or ``X-Assess-Actor`` (GET), verified as a fresh EDITOR of the activity
(``api.teachers``); the course and type the plugin sends are hints for the worker's refresh only.

PUT: BR-013 / REQ-077 validated here (``teacher.validation``), all field errors at once (400
``ERR-ASSESS-022``), optimistic concurrency on ``expected_version`` (409 ``ERR-ASSESS-023``), one
transaction: the config row (version + 1), each given secret field sealed to the worker's public
key (``crypto.teacher_config.store_sealed_field``), and one audit row per changed field, old/new.
Secret fields are audited by the database (set / replaced / cleared, recipient refs), never their
content; nothing secret is logged. A session that has already started keeps what it copied at
Start (duration, cap, snapshot); only the transcript recipients are re-read at send time.

GET: the non-secret values, ``impostata`` / ``non impostata`` for each secret field (plus the
recipients' count), the computed opening condition, the end-date warning, the review-options
warning and the number of active sessions. It never returns a solution, a rubric or an address.

A PUT refused on an LMS fact (``teacher.validation.is_lms_fact_error``: no cut-off, no close
date, team submission, marking guide, no LMS rubric, unreadable, or an end date refused against
an opening that comes from the cached close date) queues a refresh of the facts; when every
error of the refusal is such a fact, the 400 carries ``error.details.retry_after_s`` and
``Retry-After`` (always: the plugin retries at most once, ``api.teachers``).

``POST /activities/{cmid}/gate-refresh``: the plugin opens a teacher page; queue a refresh of the
activity's facts now (coalesced, idempotent, never a refusal of the page). See ``gate_refresh``.
"""

from __future__ import annotations

import json
from datetime import UTC, datetime
from typing import Any
from uuid import UUID

import structlog
from fastapi import APIRouter, Depends, HTTPException, Request
from fastapi.responses import JSONResponse
from sqlalchemy import text

from vektra_assess import privacy
from vektra_assess.api.common import (
    engine_of,
    fail,
    iso_time,
    opening_json,
    parse_body,
    site_of_key,
)
from vektra_assess.api.deps import settings_of
from vektra_assess.api.teacher_ops import opening_for
from vektra_assess.api.teachers import (
    GATE_ROW,
    REFRESH_MIN_INTERVAL_S,
    RETRY_AFTER_S,
    TeacherGate,
    header_int,
    path_id,
    queue_teacher_gate,
    refresh_after_fact_refusal,
    refresh_hint,
    verify_teacher,
)
from vektra_assess.audit import AssessAuditEvent, write_event
from vektra_assess.crypto.teacher_config import (
    PASTED_RUBRIC,
    SOLUTION,
    TRANSCRIPT_RECIPIENTS,
    store_sealed_field,
)
from vektra_assess.delivery.recipients import parse_domains, recipient_ref
from vektra_assess.errors import (
    assess_unavailable,
    bad_request,
    config_conflict,
    config_invalid,
    retry_headers,
)
from vektra_assess.keys import AssessKeyring
from vektra_assess.teacher import validation as v
from vektra_assess.teacher.schemas import (
    MAX_RECIPIENT_CHARS,
    MAX_RECIPIENT_ITEMS,
    ConfigPut,
    FieldError,
    TeacherAction,
    TriState,
    field_errors,
    state,
)
from vektra_shared.auth import ApiKeyInfo, require_exact_scope

log = structlog.get_logger(__name__)
router = APIRouter()

_guard = require_exact_scope("assess:teacher")

#: the worst case of a VALID body: PHP's json_encode escapes every non-ASCII code point, an
#: astral one as a surrogate pair (12 bytes, ``\uXXXX\uXXXX``), so two texts at the 20,000
#: code-point limit, plus the recipients list at its schema bounds, plus the other fields. A text
#: within the limit therefore always reaches the field rules (``too_long``), never body_too_large.
_ESCAPED_CHAR = 12
MAX_CONFIG_BODY_BYTES = (
    2 * v.MAX_TEXT_CHARS * _ESCAPED_CHAR
    + MAX_RECIPIENT_ITEMS * (MAX_RECIPIENT_CHARS * _ESCAPED_CHAR + 4)
    + 4096
)
PUBLIC_KEY = "assess_teacher_config_public"
#: optional PUT fields that always have a value: ``null`` on them is a field error
NOT_NULLABLE = ("enabled", "rubric_source", "show_solution", "rag_enabled")

_CONFIG = text(
    "SELECT activity_type, enabled, flags, rag_enabled, cap_minutes, version, "
    "extract(epoch FROM end_at)::bigint AS end_at, "
    "extract(epoch FROM attested_opening_at)::bigint AS attested_at, updated_at "
    "FROM assess_activity_config WHERE site_id = :s AND cmid = :c"
)
_CONFIG_LOCK = text(str(_CONFIG) + " FOR UPDATE")
_SEALED = text(
    "SELECT field, is_set, item_count FROM assess_activity_config_sealed "
    "WHERE site_id = :s AND cmid = :c"
)
_INSERT = text(
    "INSERT INTO assess_activity_config (site_id, cmid, activity_type, enabled, flags, "
    "rag_enabled, end_at, cap_minutes, version, updated_by, updated_at) VALUES (:s, :c, :t, :e, "
    "CAST(:f AS jsonb), false, :end, :cap, 1, :u, now()) "
    # a first save has no row to lock: a concurrent first save that committed meanwhile makes
    # this a version conflict (409), never a unique violation (500)
    "ON CONFLICT (site_id, cmid) DO NOTHING"
)
_VERSION = text(
    "SELECT version FROM assess_activity_config WHERE site_id = :s AND cmid = :c"
)
_UPDATE = text(
    "UPDATE assess_activity_config SET enabled = :e, flags = CAST(:f AS jsonb), end_at = :end, "
    "cap_minutes = :cap, version = version + 1, updated_by = :u, updated_at = now() "
    "WHERE site_id = :s AND cmid = :c AND version = :v"
)
_SETUP = text(
    "SELECT items FROM assess_setup_check_reports WHERE site_id = :s AND status = 'done' "
    "ORDER BY finished_at DESC NULLS LAST, created_at DESC LIMIT 1"
)
_ACTIVE = text(
    "SELECT count(*) FROM assess_sessions WHERE site_id = :s AND cmid = :c "
    "AND state IN ('ACTIVE', 'OUTAGE')"
)
_REVIEW = text(
    "SELECT state FROM assess_site_capabilities WHERE site_id = :s "
    "AND flag = 'review_html_filterable'"
)


class _Invalid(Exception):
    """Field errors of a PUT: raised through the transaction (nothing is written), answered
    after it by ``_refuse_invalid``, which may queue a fact refresh in a transaction of its
    own."""

    def __init__(self, errors: list[FieldError], opening_source: str | None) -> None:
        super().__init__("invalid")
        self.errors = errors
        #: where the end date's floor came from (``gates.Opening.source``), when it was checked
        self.opening_source = opening_source


async def _refuse_invalid(
    request: Request,
    site: UUID,
    cmid: int,
    gate: TeacherGate,
    errors: list[FieldError],
    opening_source: str | None = None,
) -> HTTPException:
    """The 400 of a refused PUT. An LMS-fact refusal queues the refresh of the facts;
    ``retry_after_s`` only when EVERY error is such a fact (the retry can then pass as it is;
    any other error needs the teacher first). ``opening_source``: where the end date's floor
    came from, so an end date refused against the teacher's own attested opening is not
    taken for an LMS fact."""
    on_facts = [e for e in errors if v.is_lms_fact_error(e, gate.facts, opening_source)]
    queued = bool(on_facts) and await refresh_after_fact_refusal(
        engine_of(request), site, cmid, gate
    )
    retry = queued and len(on_facts) == len(errors)
    err = config_invalid(
        field_errors(errors), retry_after_s=RETRY_AFTER_S if retry else None
    )
    return fail(err, retry_headers(err))


def _facts(gate: TeacherGate) -> v.ActivityFacts:
    """The worker-written facts, typed; a missing or mistyped one is None (unreadable)."""
    f = gate.facts

    def flag(name: str) -> bool | None:
        value = f.get(name)
        return value if isinstance(value, bool) else None

    method = f.get("grading_method")
    return v.ActivityFacts(
        activity_type=str(f.get("activity_type")),
        close_at=v.epoch_fact(f, "close_at"),
        team_submission=flag("team_submission"),
        grading_method=method if isinstance(method, str) else None,
        lms_rubric=flag("lms_rubric"),
    )


async def _setup_green(conn: Any, site: UUID) -> bool:
    items = (await conn.execute(_SETUP, {"s": site})).scalar()
    return (
        isinstance(items, list)
        and bool(items)
        and all(isinstance(i, dict) and i.get("result") == "OK" for i in items)
    )


def _epoch(value: datetime | None) -> int | None:
    return int(value.timestamp()) if value is not None else None


@router.put("/activities/{cmid}/config")
async def put_config(
    cmid: str, request: Request, key: ApiKeyInfo = Depends(_guard)
) -> JSONResponse:
    c = path_id(cmid, "cmid")
    body = await parse_body(request, ConfigPut, max_bytes=MAX_CONFIG_BODY_BYTES)
    settings = settings_of(request)
    secrets_given = [
        f for f in (SOLUTION, PASTED_RUBRIC, TRANSCRIPT_RECIPIENTS) if body.given(f)
    ]
    public = (getattr(request.app.state, "assess_keys", None) or {}).get(PUBLIC_KEY)
    if secrets_given and not isinstance(public, AssessKeyring):
        raise fail(assess_unavailable("teacher_config_key"))
    refused: Any = None
    try:
        async with engine_of(request).begin() as conn:
            site = await site_of_key(conn, key, "teacher")
            gate, refused = await verify_teacher(
                conn, site=site, cmid=c, actor=body.actor_userid, courseid=body.courseid,
                kind=body.activity_type, max_age_s=settings.teacher_gate_max_age_s,
                route="put_config", role="editor",
            )  # fmt: skip
            if refused is None:
                view = await _save(conn, site, c, body, gate, settings, public)
    except _Invalid as invalid:  # rolled back: nothing of the PUT was written
        raise await _refuse_invalid(
            request, site, c, gate, invalid.errors, invalid.opening_source
        ) from None
    if refused is not None:
        raise refused  # after commit: the refusal stays audited, the refresh queued
    return JSONResponse(view, headers={"Cache-Control": "no-store"})


async def _save(
    conn: Any,
    site: UUID,
    cmid: int,
    body: ConfigPut,
    gate: TeacherGate,
    settings: Any,
    public: AssessKeyring | None,
) -> dict[str, Any]:
    facts = _facts(gate)
    kind = facts.activity_type
    row = (await conn.execute(_CONFIG_LOCK, {"s": site, "c": cmid})).first()
    current_version = int(row.version) if row is not None else 0
    if body.expected_version != current_version:
        raise fail(config_conflict(current_version))
    sealed = {
        r.field: (bool(r.is_set), r.item_count)
        for r in (await conn.execute(_SEALED, {"s": site, "c": cmid})).all()
    }
    flags = dict(row.flags or {}) if row is not None else {}
    old: dict[str, Any] = {
        "enabled": bool(row.enabled) if row is not None else False,
        "cap_minutes": row.cap_minutes if row is not None else None,
        "rubric_source": v.rubric_source_of(flags, kind),
        "show_solution": flags.get("show_solution") is True,
        "available_until": row.end_at if row is not None else None,
    }
    new: dict[str, Any] = dict(old)
    errors: list[FieldError] = []
    opening_source: str | None = None

    # ``null`` clears only what can be empty (the write-only fields, the cap, the end date); on a
    # setting that always has a value it is a field error, never a silent false (review 8)
    errors += v.check_not_null(
        name
        for name in NOT_NULLABLE
        if body.given(name) and getattr(body, name) is None
    )
    if body.enabled is not None:
        new["enabled"] = body.enabled
    if body.given("cap_minutes"):
        new["cap_minutes"] = body.cap_minutes
        if body.cap_minutes is not None:
            errors += v.check_cap(body.cap_minutes)
    if body.rubric_source is not None:
        new["rubric_source"] = body.rubric_source
        errors += v.check_rubric_source(body.rubric_source, kind)
    if body.show_solution is not None:
        new["show_solution"] = body.show_solution
    if body.rag_enabled is not None:
        errors += v.check_rag(body.rag_enabled)
    if body.given("available_until"):
        end = body.available_until
        new["available_until"] = None
        if end is not None:
            opening = await opening_for(
                conn, site, gate.facts, row.attested_at if row is not None else None
            )
            opening_source = opening.source
            # the end date must follow the opening the view shows (grace included); with no
            # opening possible (no close, or the close unreadable) it is opening_unknown
            end_errors = v.check_end_date(end, opening.floor)
            errors += end_errors
            # converted only once the rule accepted it (a naive or unrepresentable value is a
            # field error, never a conversion failure)
            if not end_errors:
                new["available_until"] = _epoch(end)

    # write-only fields: absent = unchanged, null = clear, value = replace (OQ-034)
    texts: dict[str, str | None] = {}
    for fld in (SOLUTION, PASTED_RUBRIC):
        mode = body.tri_state(fld)
        if mode is TriState.UNCHANGED:
            continue
        raw = getattr(body, fld)
        if mode is TriState.CLEAR or raw is None:
            texts[fld] = None
            continue
        norm, errs = v.check_text(fld, raw)
        errors += errs
        texts[fld] = norm
    recipients: tuple[str, ...] | None = None
    recipients_mode = body.tri_state(TRANSCRIPT_RECIPIENTS)
    if recipients_mode is TriState.REPLACE:
        recipients, errs = v.check_recipients(
            body.transcript_recipients or [], parse_domains(settings.recipient_domains)
        )
        errors += errs

    def is_set(fld: str) -> bool:
        if fld in texts:
            return texts[fld] is not None
        return sealed.get(fld, (False, None))[0]

    if recipients_mode is TriState.REPLACE:
        count = len(recipients or ())
    elif recipients_mode is TriState.CLEAR:
        count = 0
    else:
        set_, n = sealed.get(TRANSCRIPT_RECIPIENTS, (False, None))
        count = int(n or 0) if set_ else 0
    errors += v.check_pasted_rubric(str(new["rubric_source"]), is_set(PASTED_RUBRIC))
    if new["enabled"]:
        turning_on = body.enabled is True and not old["enabled"]
        errors += v.enable_refusals(
            v.Proposed(
                enabled=True,
                cap_minutes=new["cap_minutes"],
                rubric_source=str(new["rubric_source"]),
                pasted_rubric_set=is_set(PASTED_RUBRIC),
                recipients_count=count,
            ),
            facts,
            privacy_configured=await privacy.is_configured(conn, site),
            setup_green=await _setup_green(conn, site),
            turning_on=turning_on,
        )
    if errors:
        unique: list[FieldError] = []
        for e in errors:
            if e not in unique:
                unique.append(e)
        raise _Invalid(unique, opening_source)

    # --- write: the config row (version + 1), the sealed fields, the audit -------------------
    flags["show_solution"] = bool(new["show_solution"])
    flags["rubric_source"] = str(new["rubric_source"])
    end_at = (
        datetime.fromtimestamp(new["available_until"], UTC)
        if new["available_until"] is not None
        else None
    )
    params = {
        "s": site,
        "c": cmid,
        "e": bool(new["enabled"]),
        "f": json.dumps(flags, sort_keys=True),
        "end": end_at,
        "cap": new["cap_minutes"],
        "u": body.actor_userid,
    }
    if row is None:
        if not (await conn.execute(_INSERT, {**params, "t": kind})).rowcount:
            now = (await conn.execute(_VERSION, {"s": site, "c": cmid})).scalar()
            raise fail(config_conflict(int(now or 0)))
    else:
        done = (await conn.execute(_UPDATE, {**params, "v": current_version})).rowcount
        if (
            done != 1
        ):  # locked FOR UPDATE above: cannot happen, refuse rather than guess
            raise fail(config_conflict(current_version))
    version = current_version + 1
    for name in (
        "enabled",
        "cap_minutes",
        "rubric_source",
        "show_solution",
        "available_until",
    ):
        if old[name] != new[name] or (row is None and body.given(name)):
            await write_event(
                conn,
                AssessAuditEvent.TEACHER_CONFIG_CHANGED,
                actor="api",
                site_id=site,
                reason_code=name,
                data={
                    "field": name,
                    "old": old[name],
                    "new": new[name],
                    "cmid": cmid,
                    "actor": body.actor_userid,
                    "version": version,
                },
            )
    if public is not None:
        for fld, value in texts.items():
            await store_sealed_field(
                conn, public, site, cmid, fld, value, actor_userid=body.actor_userid
            )
        if recipients_mode is not TriState.UNCHANGED:
            addresses = recipients or ()
            await store_sealed_field(
                conn,
                public,
                site,
                cmid,
                TRANSCRIPT_RECIPIENTS,
                json.dumps(list(addresses)) if addresses else None,
                actor_userid=body.actor_userid,
                refs=[recipient_ref(site, a) for a in addresses] or None,
                item_count=len(addresses) or None,
            )
    log.info(
        "assess_teacher_config_saved",
        site_id=str(site),
        cmid=cmid,
        version=version,
        fields=sorted(
            body.model_fields_set - {"actor_userid", "courseid", "activity_type"}
        ),
    )
    return await _view(conn, site, cmid, gate)


async def _view(conn: Any, site: UUID, cmid: int, gate: TeacherGate) -> dict[str, Any]:
    facts = _facts(gate)
    kind = facts.activity_type
    row = (await conn.execute(_CONFIG, {"s": site, "c": cmid})).first()
    sealed = {
        r.field: (bool(r.is_set), r.item_count)
        for r in (await conn.execute(_SEALED, {"s": site, "c": cmid})).all()
    }
    flags = dict(row.flags or {}) if row is not None else {}
    attested = row.attested_at if row is not None else None
    end_at = row.end_at if row is not None else None
    # the opening Start waits for, by the gate's own rule (REQ-091 included); the warning
    # compares the end date with the same ``floor`` the PUT validates it against
    opening = await opening_for(conn, site, gate.facts, attested)
    floor = opening.floor
    rec_set, rec_n = sealed.get(TRANSCRIPT_RECIPIENTS, (False, None))
    review = (await conn.execute(_REVIEW, {"s": site})).scalar()
    active = int((await conn.execute(_ACTIVE, {"s": site, "c": cmid})).scalar() or 0)
    return {
        "cmid": cmid,
        "activity_type": kind,
        "version": int(row.version) if row is not None else 0,
        "configured": row is not None,
        "enabled": bool(row.enabled) if row is not None else False,
        "cap_minutes": row.cap_minutes if row is not None else None,
        "rubric_source": v.rubric_source_of(flags, kind),
        "show_solution": flags.get("show_solution") is True,
        "rag_enabled": False,
        "available_until": iso_time(end_at),
        "solution_state": state(sealed.get(SOLUTION, (False, None))[0]),
        "pasted_rubric_state": state(sealed.get(PASTED_RUBRIC, (False, None))[0]),
        "transcript_recipients_state": state(rec_set),
        "transcript_recipients_count": int(rec_n or 0) if rec_set else 0,
        "opening": opening_json(opening),
        "warnings": {
            # REQ-077: the effective opening moved past the end date
            "available_until_not_after_opening": bool(
                end_at and floor and end_at <= floor
            ),
            # quiz detail is reduced where the review HTML cannot be filtered (ARCH-070)
            "review_options_hide_content": kind == "quiz" and review != "readable",
        },
        "active_sessions": active,
        "updated_at": row.updated_at.isoformat() if row is not None else None,
    }


@router.get("/activities/{cmid}/config")
async def get_config(
    cmid: str, request: Request, key: ApiKeyInfo = Depends(_guard)
) -> JSONResponse:
    c = path_id(cmid, "cmid")
    actor = header_int(request, "X-Assess-Actor")
    if actor is None:
        raise fail(bad_request("x_assess_actor"))
    courseid = header_int(request, "X-Assess-Course")
    kind = request.headers.get("X-Assess-Activity-Type")
    settings = settings_of(request)
    refused: Any = None
    async with engine_of(request).begin() as conn:
        site = await site_of_key(conn, key, "teacher")
        gate, refused = await verify_teacher(
            conn, site=site, cmid=c, actor=actor, courseid=courseid, kind=kind,
            max_age_s=settings.teacher_gate_max_age_s, route="get_config", role="editor",
        )  # fmt: skip
        if refused is None:
            view = await _view(conn, site, c, gate)
    if refused is not None:
        raise refused
    return JSONResponse(view, headers={"Cache-Control": "no-store"})


# --- gate refresh (live E2E follow-up) ----------------------------------------------------------


@router.post("/activities/{cmid}/gate-refresh")
async def gate_refresh(
    cmid: str, request: Request, key: ApiKeyInfo = Depends(_guard)
) -> JSONResponse:
    """A teacher page of the activity opened (the Discussioni list, the activity settings, the
    grader's page): read the activity's LMS facts again NOW, so the teacher's first action finds
    them fresh instead of a "checking your role" refusal.

    ``assess:teacher`` key (exact scope), bound to its site; the body is the common teacher
    shape (``actor_userid``, ``courseid``, ``activity_type``). It verifies nobody and returns no
    fact: the refresh is per activity (every teacher of it benefits) and ``actor_userid`` is only
    checked for shape. The course and type are a hint used only while the activity has no
    RESOLVED row: a resolved row is refreshed with its own course and type (``refresh_hint``), so
    a wrong course in the body can never queue a read that says ``activity_not_in_course``.

    Coalesced per cmid: nothing is queued while a refresh for that hint is pending, or when the
    activity's facts were read less than ``REFRESH_MIN_INTERVAL_S`` ago (a resolved row, or an
    unresolved one for the same hint). Always 202 for a well-formed request: the plugin may
    ignore any failure and open the page.
    """
    c = path_id(cmid, "cmid")
    body = await parse_body(request, TeacherAction)
    async with engine_of(request).begin() as conn:
        site = await site_of_key(conn, key, "teacher")
        row = (await conn.execute(GATE_ROW, {"s": site, "c": c})).first()
        facts = (
            dict(row.inputs) if row is not None and isinstance(row.inputs, dict) else {}
        )
        hint = refresh_hint(facts, body.courseid, body.activity_type)
        if hint is None:  # unreachable: the schema validated the body's hint
            raise fail(bad_request("teacher_gate_hint"))
        recent = (
            row is not None
            and row.age_s is not None
            and float(row.age_s) <= REFRESH_MIN_INTERVAL_S
            and (facts.get("resolved") is True or facts.get("hint") == list(hint))
        )
        queued = (
            False
            if recent
            else await queue_teacher_gate(conn, site, c, *hint, follow_up=True)
        )
    log.info("assess_teacher_gate_refresh", site_id=str(site), cmid=c, queued=queued)
    return JSONResponse(
        {
            "cmid": c,
            "queued": queued,
            "refreshed_at": iso_time(row.refreshed_at) if row is not None else None,
        },
        status_code=202,
        headers={"Cache-Control": "no-store"},
    )
