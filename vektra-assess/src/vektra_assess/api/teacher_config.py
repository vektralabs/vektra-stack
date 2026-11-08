"""Teacher configuration of one activity (plan assess-teacher-ops, REQ-077, INT-001, ARCH-071).

``PUT /api/v1/assess/activities/{cmid}/config`` and ``GET`` the same path, ``assess:teacher`` keys
only (exact scope: an ``admin`` key is 403), bound to their site. The acting Moodle user is
``actor_userid`` (body) or ``X-Assess-Actor`` (GET), verified as a fresh EDITOR of the activity
(``api.teachers``); the course and type the plugin sends are hints for the worker's refresh only.

PUT: BR-013 / REQ-077 validated here (``teacher.validation``), all field errors at once (400
``ERR-ASSESS-022``), optimistic concurrency on ``expected_version`` (409 ``ERR-ASSESS-023``), one
transaction: the config row (version + 1), each given secret field sealed to the worker's public
key (``crypto.teacher_config.store_sealed_field``), and one audit row per changed field, old/new.
Secret fields are audited by the database (set / replaced / cleared, recipient refs), never their
content; nothing secret is logged. A session that has already started keeps what it copied at
Start (duration, cap, snapshot), so a change never reaches it.

GET: the non-secret values, ``impostata`` / ``non impostata`` for each secret field (plus the
recipients' count), the computed opening condition, the end-date warning, the review-options
warning and the number of active sessions. It never returns a solution, a rubric or an address.
"""

from __future__ import annotations

import json
from datetime import UTC, datetime
from typing import Any
from uuid import UUID

import structlog
from fastapi import APIRouter, Depends, Request
from fastapi.responses import JSONResponse
from sqlalchemy import text

from vektra_assess import privacy
from vektra_assess.api.common import SITE_OF_KEY, engine_of, fail, parse_body
from vektra_assess.api.deps import settings_of
from vektra_assess.api.teachers import TeacherCheck, TeacherGate, check_fresh_editor
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
    key_not_bound,
    teacher_unverified,
)
from vektra_assess.keys import AssessKeyring
from vektra_assess.teacher import validation as v
from vektra_assess.teacher.schemas import (
    ConfigPut,
    FieldError,
    TriState,
    field_errors,
    state,
)
from vektra_shared.auth import ApiKeyInfo, require_exact_scope

log = structlog.get_logger(__name__)
router = APIRouter()

_guard = require_exact_scope("assess:teacher")

#: two 20,000-code-point texts of up to 4 UTF-8 bytes, JSON-escaped, plus the rest of the form
MAX_CONFIG_BODY_BYTES = 256 * 1024
PUBLIC_KEY = "assess_teacher_config_public"

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
    "CAST(:f AS jsonb), false, :end, :cap, 1, :u, now())"
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


def _cmid(raw: str) -> int:
    if not raw.isdigit() or not 0 < int(raw) < 2**63:
        raise fail(bad_request("cmid"))
    return int(raw)


async def _site_of(conn: Any, key: ApiKeyInfo) -> UUID:
    site = (await conn.execute(SITE_OF_KEY, {"k": key.key_id, "p": "teacher"})).scalar()
    if site is None:
        raise fail(key_not_bound())
    return site  # type: ignore[no-any-return]


async def _verify(
    conn: Any,
    site: UUID,
    cmid: int,
    actor: int,
    courseid: int | None,
    kind: str | None,
    max_age_s: int,
    route: str,
) -> TeacherGate:
    gate = await check_fresh_editor(
        conn,
        site=site,
        cmid=cmid,
        actor_userid=actor,
        max_age_s=max_age_s,
        courseid_hint=courseid,
        kind_hint=kind,
    )
    if not gate.ok:
        await write_event(
            conn,
            AssessAuditEvent.TEACHER_UNVERIFIED,
            actor="api",
            site_id=site,
            reason_code=gate.check.value,
            data={"cmid": cmid, "actor": actor, "route": route, "role": "editor"},
        )
    return gate


def _refusal(gate: TeacherGate) -> Any:
    return fail(
        teacher_unverified(stale=gate.check is TeacherCheck.STALE, role="editor")
    )


def _facts(gate: TeacherGate) -> v.ActivityFacts:
    """The worker-written facts, typed; a missing or mistyped one is None (unreadable)."""
    f = gate.facts

    def flag(name: str) -> bool | None:
        value = f.get(name)
        return value if isinstance(value, bool) else None

    close = f.get("close_at")
    method = f.get("grading_method")
    return v.ActivityFacts(
        activity_type=str(f.get("activity_type")),
        close_at=close
        if isinstance(close, int) and not isinstance(close, bool)
        else None,
        team_submission=flag("team_submission"),
        grading_method=method if isinstance(method, str) else None,
        moodle_rubric=flag("moodle_rubric"),
    )


def _default_source(kind: str) -> str:
    return "moodle" if kind == "assign" else "none"


def _rubric_source(flags: dict[str, Any], kind: str) -> str:
    source = flags.get("rubric_source")
    return str(source) if source in v.SOURCES.get(kind, ()) else _default_source(kind)


async def _setup_green(conn: Any, site: UUID) -> bool:
    items = (await conn.execute(_SETUP, {"s": site})).scalar()
    return (
        isinstance(items, list)
        and bool(items)
        and all(isinstance(i, dict) and i.get("result") == "OK" for i in items)
    )


def _epoch(value: datetime | None) -> int | None:
    return int(value.timestamp()) if value is not None else None


def _iso(epoch: int | None) -> str | None:
    return datetime.fromtimestamp(epoch, UTC).isoformat() if epoch else None


@router.put("/activities/{cmid}/config")
async def put_config(
    cmid: str, request: Request, key: ApiKeyInfo = Depends(_guard)
) -> JSONResponse:
    c = _cmid(cmid)
    body = await parse_body(request, ConfigPut, max_bytes=MAX_CONFIG_BODY_BYTES)
    settings = settings_of(request)
    secrets_given = [
        f for f in (SOLUTION, PASTED_RUBRIC, TRANSCRIPT_RECIPIENTS) if body.given(f)
    ]
    public = (getattr(request.app.state, "assess_keys", None) or {}).get(PUBLIC_KEY)
    if secrets_given and not isinstance(public, AssessKeyring):
        raise fail(assess_unavailable("teacher_config_key"))
    refused: Any = None
    async with engine_of(request).begin() as conn:
        site = await _site_of(conn, key)
        gate = await _verify(
            conn, site, c, body.actor_userid, body.courseid, body.activity_type,
            settings.teacher_gate_max_age_s, "put_config",
        )  # fmt: skip
        if not gate.ok:
            refused = _refusal(gate)
        else:
            view = await _save(conn, site, c, body, gate, settings, public)
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
        "rubric_source": _rubric_source(flags, kind),
        "show_solution": flags.get("show_solution") is True,
        "available_until": row.end_at if row is not None else None,
    }
    new: dict[str, Any] = dict(old)
    errors: list[FieldError] = []

    if body.given("enabled"):
        new["enabled"] = bool(body.enabled)
    if body.given("cap_minutes"):
        new["cap_minutes"] = body.cap_minutes
        if body.cap_minutes is not None:
            errors += v.check_cap(body.cap_minutes)
    if body.given("rubric_source"):
        if body.rubric_source is None:
            new["rubric_source"] = _default_source(kind)
        else:
            new["rubric_source"] = body.rubric_source
            errors += v.check_rubric_source(body.rubric_source, kind)
    if body.given("show_solution"):
        new["show_solution"] = bool(body.show_solution)
    if body.given("rag_enabled") and body.rag_enabled is not None:
        errors += v.check_rag(body.rag_enabled)
    if body.given("available_until"):
        end = body.available_until
        new["available_until"] = _epoch(end) if end is not None else None
        if end is not None:
            opening = v.effective_opening(
                facts.close_at, row.attested_at if row is not None else None
            )
            errors += v.check_end_date(end, opening)

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
        raise fail(config_invalid(field_errors(unique)))

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
        await conn.execute(_INSERT, {**params, "t": kind})
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
    opening = v.effective_opening(facts.close_at, attested)
    if opening is None:
        source = "unknown"
    elif attested is not None and attested >= (facts.close_at or 0):
        source = "attested"  # REQ-091: the teacher's date decides (or ties the close)
    else:
        source = "activity_close"
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
        "rubric_source": _rubric_source(flags, kind),
        "show_solution": flags.get("show_solution") is True,
        "rag_enabled": False,
        "available_until": _iso(end_at),
        "solution_state": state(sealed.get(SOLUTION, (False, None))[0]),
        "pasted_rubric_state": state(sealed.get(PASTED_RUBRIC, (False, None))[0]),
        "transcript_recipients_state": state(rec_set),
        "transcript_recipients_count": int(rec_n or 0) if rec_set else 0,
        "opening": {"at": _iso(opening), "source": source},
        "warnings": {
            # REQ-077: the effective opening moved past the end date
            "available_until_not_after_opening": bool(
                end_at and opening and end_at <= opening
            ),
            # quiz detail is reduced where the review HTML cannot be filtered (ARCH-070)
            "review_options_hide_content": kind == "quiz" and review != "readable",
        },
        "active_sessions": active,
        "updated_at": row.updated_at.isoformat() if row is not None else None,
    }


def _header_int(request: Request, name: str) -> int | None:
    raw = request.headers.get(name, "")
    return int(raw) if raw.isdigit() and 0 < int(raw) < 2**63 else None


@router.get("/activities/{cmid}/config")
async def get_config(
    cmid: str, request: Request, key: ApiKeyInfo = Depends(_guard)
) -> JSONResponse:
    c = _cmid(cmid)
    actor = _header_int(request, "X-Assess-Actor")
    if actor is None:
        raise fail(bad_request("x_assess_actor"))
    courseid = _header_int(request, "X-Assess-Course")
    kind = request.headers.get("X-Assess-Activity-Type")
    settings = settings_of(request)
    refused: Any = None
    async with engine_of(request).begin() as conn:
        site = await _site_of(conn, key)
        gate = await _verify(
            conn, site, c, actor, courseid, kind if kind in v.SOURCES else None,
            settings.teacher_gate_max_age_s, "get_config",
        )  # fmt: skip
        if not gate.ok:
            refused = _refusal(gate)
        else:
            view = await _view(conn, site, c, gate)
    if refused is not None:
        raise refused
    return JSONResponse(view, headers={"Cache-Control": "no-store"})
