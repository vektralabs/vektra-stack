"""Privacy settings, subject export and erasure (plan assess-privacy-retention, INT-001).

``PUT|GET /api/v1/assess/sites/{site}/privacy`` (exact ``admin``)
    The REQ-089 settings of a site (values supplied by the DPO, OQ-020). The PUT replaces the
    whole row; any invalid field is a 400 ``ERR-ASSESS-028`` naming the fields, never echoing the
    values. A change to anything the student is shown (``SHOWN_FIELDS``) with the same
    ``notice_version`` is refused the same way (``notice_version`` / ``must_change``): the version
    is what a Start compares (plugin request P3). Audited old/new as counts and flags (``assess_privacy_config_changed``).
``DELETE /api/v1/assess/privacy/subjects/{userid}`` (exact ``assess:privacy-erase``, site-bound)
``DELETE /api/v1/assess/privacy/activities/{cmid}/subjects`` (same key, optional ``{"userids"}``)
    OQ-033. ``VEKTRA_ASSESS_ERASURE_MODE=retain_exam_evidence``: 200 ``outcome=retained`` with
    the configured legal basis, audited, nothing deleted. ``erase``: an erasure request and an
    ``erasure`` job; the answer waits up to ``VEKTRA_ASSESS_ERASURE_WAIT_S`` for the worker and
    returns its counts, or 503 ``ERR-ASSESS-027`` (``pending``: the same request again is
    idempotent; ``failed``: the count check found rows left: the deletion was rolled back, though live sessions closed beforehand stay closed). Never a
    2xx without an erasure or a ``retained`` outcome. The API itself deletes nothing.
``GET /api/v1/assess/privacy/subjects/{userid}/transcripts`` (exact ``assess:privacy-export``)
    REQ-093 (b): the subject's transcripts of this site, whatever the enrolment or course access
    now, as the exact bytes the in-course download serves (base64, with their SHA-256), at most
    ``EXPORT_PAGE`` per page, the cursor in ``X-Assess-Cursor``. Audited with counts only.
``GET /api/v1/assess/privacy/subjects/{userid}/activities`` (exact ``assess:privacy-export``)
    Every activity of this site where the subject has any row the subject erasure deletes
    (``privacy.subject.SUBJECT_TABLES``, the erasure's own list), with its sessions (number,
    state, close reason, times), resets (after which session, when) and extra time: the plugin's
    ``get_contexts_for_userid`` and the export of what has no transcript. Ids, states, codes and
    times only: no reset reason, no teacher note. ``EXPORT_PAGE`` activities per page, the cmid
    cursor in ``X-Assess-Cursor``. Audited with counts only.
``GET /api/v1/assess/privacy/activities/{cmid}/subjects`` (exact ``assess:privacy-export``)
    The user ids with any assess row for that activity of this site (the plugin's
    ``get_users_in_context``).

Keys are site-bound (``assess_site_keys`` purpose ``privacy-export`` / ``privacy-erase``); the
admin superscope never reaches the subject routes, and an assess key never reaches the
settings. No route reads the query string; every refusal is an explicit envelope.
"""

from __future__ import annotations

import asyncio
import base64
from typing import Any
from uuid import UUID

import structlog
from fastapi import APIRouter, Depends, Request
from fastapi.responses import JSONResponse
from sqlalchemy import text

from vektra_assess.api.common import (
    engine_of,
    fail,
    iso_time,
    json_object,
    read_capped_body,
    site_of_key,
)
from vektra_assess.api.deps import settings_of
from vektra_assess.api.state import limiter_of
from vektra_assess.api.teachers import ascii_id, header_int, path_id
from vektra_assess.api.transcripts import transcript_key_of
from vektra_assess.audit import AssessAuditEvent, write_event
from vektra_assess.errors import (
    assess_unavailable,
    bad_request,
    erasure_not_completed,
    privacy_config_invalid,
    rate_limited,
    transcript_integrity,
)
from vektra_assess.jobs import JobKind, enqueue_job
from vektra_assess.privacy import config as privacy_config
from vektra_assess.privacy.subject import (
    ACTIVITY_SUBJECTS,
    SUBJECT_ACTIVITIES,
    subject_ref,
)
from vektra_assess.transcript import store
from vektra_shared.auth import ApiKeyInfo, require_exact_scope

log = structlog.get_logger(__name__)
router = APIRouter()

_admin = require_exact_scope("admin")
_erase = require_exact_scope("assess:privacy-erase")
_export = require_exact_scope("assess:privacy-export")

EXPORT_PAGE = 10
#: an activity erasure names at most this many users in one request
MAX_USERIDS = 1000
_POLL_S = 0.2


def _limit(request: Request, key: ApiKeyInfo) -> None:
    allowed, _ = limiter_of(request, "privacy").check(
        ("privacy", key.key_id), settings_of(request).privacy_rpm
    )
    if not allowed:
        raise fail(rate_limited(60, "privacy_rate"))


def _site_id(raw: str) -> UUID:
    try:
        return UUID(raw)
    except ValueError:
        raise fail(bad_request("site_id")) from None


# --- settings (operator) ----------------------------------------------------------------------

_SITE = text("SELECT id FROM assess_sites WHERE id = :s")
_UPSERT = text(
    "INSERT INTO assess_privacy_config (site_id, retention_days, legal_basis_label, "
    "legal_basis_text, notice_url, notice_text, notice_version, updated_by, updated_at) "
    "VALUES (:s, :retention_days, :legal_basis_label, :legal_basis_text, :notice_url, "
    ":notice_text, :notice_version, :by, now()) ON CONFLICT (site_id) DO UPDATE SET "
    "retention_days = EXCLUDED.retention_days, legal_basis_label = EXCLUDED.legal_basis_label, "
    "legal_basis_text = EXCLUDED.legal_basis_text, notice_url = EXCLUDED.notice_url, "
    "notice_text = EXCLUDED.notice_text, notice_version = EXCLUDED.notice_version, "
    "updated_by = EXCLUDED.updated_by, updated_at = EXCLUDED.updated_at"
)
_CURRENT = text(
    "SELECT retention_days, legal_basis_label, legal_basis_text, notice_url, notice_text, "
    "notice_version, updated_by, updated_at FROM assess_privacy_config WHERE site_id = :s "
    "FOR UPDATE"
)


def _view(row: Any) -> dict[str, Any]:
    if row is None:
        return {"configured": False}
    out = {name: getattr(row, name) for name in privacy_config.FIELDS}
    out["updated_by"] = row.updated_by
    out["updated_at"] = iso_time(row.updated_at)
    out["configured"] = privacy_config.passes(row)
    out["mailbox_notice"] = privacy_config.MAILBOX_SENTENCE
    return out


@router.put("/sites/{site_id}/privacy")
async def put_privacy(
    site_id: str, request: Request, key: ApiKeyInfo = Depends(_admin)
) -> JSONResponse:
    site = _site_id(site_id)
    doc = json_object(await read_capped_body(request))
    errors = privacy_config.field_errors(doc)
    if errors:
        raise fail(privacy_config_invalid(errors))
    async with engine_of(request).begin() as conn:
        if (await conn.execute(_SITE, {"s": site})).scalar() is None:
            raise fail(bad_request("unknown_site"))
        old = (await conn.execute(_CURRENT, {"s": site})).first()
        if old is not None and privacy_config.version_must_change(old, doc):
            raise fail(privacy_config_invalid(privacy_config.MUST_CHANGE))
        await conn.execute(
            _UPSERT,
            {
                "s": site,
                "by": f"api_key:{key.key_id}",
                **{f: doc[f] for f in privacy_config.FIELDS},
            },
        )
        changed = {
            f"changed_{f}": old is None or getattr(old, f) != doc[f]
            for f in privacy_config.FIELDS
        }
        await write_event(
            conn,
            AssessAuditEvent.PRIVACY_CONFIG_CHANGED,
            actor="api",
            site_id=site,
            reason_code="created" if old is None else "updated",
            data={
                "old_retention_days": int(old.retention_days)
                if old is not None
                else None,
                "new_retention_days": int(doc["retention_days"]),
                "old_notice_version": str(old.notice_version)
                if old is not None
                else None,
                "new_notice_version": str(doc["notice_version"]),
                "key_id": key.key_id,
                **changed,
            },
        )
        row = (await conn.execute(_CURRENT, {"s": site})).first()
    return JSONResponse(content=_view(row))


@router.get("/sites/{site_id}/privacy")
async def get_privacy(
    site_id: str, request: Request, key: ApiKeyInfo = Depends(_admin)
) -> JSONResponse:
    site = _site_id(site_id)
    async with engine_of(request).connect() as conn:
        if (await conn.execute(_SITE, {"s": site})).scalar() is None:
            raise fail(bad_request("unknown_site"))
        row = (await conn.execute(privacy_config.READ, {"s": site})).first()
    return JSONResponse(content=_view(row))


# --- erasure (the plugin's privacy provider) ------------------------------------------------

_QUEUE = text(
    "INSERT INTO assess_erasure_records (site_id, subject_userid_hash, scope, cmid, "
    "subject_userids) VALUES (:s, :h, :scope, :cmid, :users) RETURNING id"
)
#: a request for the same scope still waiting for the worker: a retry waits on it rather than
#: queuing another (which would pile up rows carrying the same ids); a day later the purge tick
#: records an abandoned one failed
_PENDING = text(
    "SELECT id FROM assess_erasure_records WHERE site_id = :s AND scope = :scope "
    "AND completed_at IS NULL AND requested_at > now() - interval '1 day' "
    "AND cmid IS NOT DISTINCT FROM :cmid "
    "AND subject_userids IS NOT DISTINCT FROM CAST(:users AS bigint[]) "
    "ORDER BY id DESC LIMIT 1"
)
_DONE = text(
    "SELECT completed_at, outcome, rows_deleted FROM assess_erasure_records WHERE id = :i"
)


async def _retained(
    request: Request, site: UUID, key: ApiKeyInfo, scope: str, cmid: int | None
) -> JSONResponse:
    async with engine_of(request).begin() as conn:
        basis = await privacy_config.read(conn, site)
        await write_event(
            conn,
            AssessAuditEvent.ERASURE_RETAINED,
            actor="api",
            site_id=site,
            reason_code=scope,
            data={"cmid": cmid, "key_id": key.key_id},
        )
    return JSONResponse(
        content={
            "outcome": "retained",
            "legal_basis": basis.legal_basis_label if basis is not None else None,
            "counts": {"sessions": 0, "transcripts": 0, "deliveries_abandoned": 0},
        }
    )


async def _erase_and_wait(
    request: Request,
    site: UUID,
    *,
    scope: str,
    cmid: int | None,
    userids: list[int] | None,
    subject_hash: str | None,
) -> JSONResponse:
    engine = engine_of(request)
    erasure_id = await _pending_or_queue(
        engine, site, scope=scope, cmid=cmid, userids=userids, subject_hash=subject_hash
    )
    return await _wait(request, engine, erasure_id)


#: one request per scope at a time: two identical requests arriving together would both miss
#: the pending lookup and queue twice
_SCOPE_LOCK = text("SELECT pg_advisory_xact_lock(hashtextextended(:k, 0))")


async def _pending_or_queue(
    engine: Any,
    site: UUID,
    *,
    scope: str,
    cmid: int | None,
    userids: list[int] | None,
    subject_hash: str | None,
) -> int:
    """The pending request of the same scope, or a new one with its job, under a lock taken for
    the transaction so the lookup and the insert are one step."""
    users = ",".join(str(u) for u in userids) if userids is not None else "*"
    async with engine.begin() as conn:
        await conn.execute(_SCOPE_LOCK, {"k": f"erasure:{site}:{scope}:{cmid}:{users}"})
        pending = (
            await conn.execute(
                _PENDING, {"s": site, "scope": scope, "cmid": cmid, "users": userids}
            )
        ).scalar()
        if pending is not None:
            # its job may have died (attempts spent waiting for work in flight): queue it again;
            # the dedupe key keeps one ready or leased job per request
            await enqueue_job(
                conn,
                JobKind.ERASURE,
                {"erasure_id": int(pending)},
                dedupe_key=f"erasure:{pending}",
            )
            return int(pending)
        erasure_id = (
            await conn.execute(
                _QUEUE,
                {
                    "s": site,
                    "h": subject_hash,
                    "scope": scope,
                    "cmid": cmid,
                    "users": userids,
                },
            )
        ).scalar_one()
        await enqueue_job(
            conn,
            JobKind.ERASURE,
            {"erasure_id": int(erasure_id)},
            dedupe_key=f"erasure:{erasure_id}",
        )
    return int(erasure_id)


async def _read_done(engine: Any, erasure_id: int) -> Any:
    async with engine.connect() as conn:
        return (await conn.execute(_DONE, {"i": erasure_id})).first()


async def _wait(request: Request, engine: Any, erasure_id: int) -> JSONResponse:
    loop = asyncio.get_running_loop()
    deadline = loop.time() + settings_of(request).erasure_wait_s
    while True:
        remaining = deadline - loop.time()
        if remaining <= 0:
            raise fail(erasure_not_completed("pending"))
        try:
            # the deadline covers the pool wait and the query too: a busy pool answers
            # pending on time, never after the pool's own timeout
            row = await asyncio.wait_for(_read_done(engine, erasure_id), remaining)
        except TimeoutError:
            raise fail(erasure_not_completed("pending")) from None
        if row is not None and row.completed_at is not None:
            break
        await asyncio.sleep(min(_POLL_S, max(deadline - loop.time(), 0)))
    if row.outcome != "erased":
        raise fail(erasure_not_completed("failed", retry_after_s=60))
    counts = dict(row.rows_deleted or {})
    return JSONResponse(
        content={
            "outcome": "erased",
            "counts": {
                "sessions": int(counts.get("sessions", 0)),
                "transcripts": int(counts.get("transcripts", 0)),
                "deliveries_abandoned": int(counts.get("deliveries_abandoned", 0)),
            },
        }
    )


@router.delete("/privacy/subjects/{userid}")
async def erase_subject(
    userid: str, request: Request, key: ApiKeyInfo = Depends(_erase)
) -> JSONResponse:
    user = path_id(userid, "userid")
    _limit(request, key)
    async with engine_of(request).connect() as conn:
        site = await site_of_key(conn, key, "privacy-erase")
    if settings_of(request).erasure_mode == "retain_exam_evidence":
        return await _retained(request, site, key, "subject", None)
    return await _erase_and_wait(
        request,
        site,
        scope="subject",
        cmid=None,
        userids=[user],
        subject_hash=subject_ref(site, user),
    )


def _userids(doc: dict[str, Any]) -> list[int] | None:
    if set(doc) - {"userids"}:
        raise fail(bad_request("unknown_field"))
    if "userids" not in doc:
        return None  # the whole activity
    raw = doc["userids"]
    # an explicit null is a failed selection on the caller's side, never "everyone"
    if not isinstance(raw, list) or not raw or len(raw) > MAX_USERIDS:
        raise fail(bad_request("userids"))
    out: list[int] = []
    for item in raw:
        value = (
            ascii_id(str(item))
            if isinstance(item, int) and not isinstance(item, bool)
            else None
        )
        if value is None:
            raise fail(bad_request("userids"))
        out.append(value)
    return sorted(set(out))


@router.delete("/privacy/activities/{cmid}/subjects")
async def erase_activity(
    cmid: str, request: Request, key: ApiKeyInfo = Depends(_erase)
) -> JSONResponse:
    activity = path_id(cmid, "cmid")
    raw = await read_capped_body(request)
    users = _userids(json_object(raw)) if raw.strip() else None
    _limit(request, key)
    async with engine_of(request).connect() as conn:
        site = await site_of_key(conn, key, "privacy-erase")
    if settings_of(request).erasure_mode == "retain_exam_evidence":
        return await _retained(request, site, key, "activity", activity)
    return await _erase_and_wait(
        request, site, scope="activity", cmid=activity, userids=users, subject_hash=None
    )


# --- export and listing ----------------------------------------------------------------------

#: no enrolment or course check (REQ-093 (b)): the subject's own transcripts of this site
_EXPORT = text(
    "SELECT t.id, s.cmid, s.courseid_hint, s.session_no, s.closed_at, t.filename, "
    "t.content_hash, t.ciphertext, t.dek_wrapped FROM assess_transcripts t "
    "JOIN assess_sessions s ON s.id = t.session_id WHERE s.site_id = :s "
    "AND s.student_userid = :u AND t.ciphertext IS NOT NULL AND t.shredded_at IS NULL "
    "AND t.id > :after ORDER BY t.id LIMIT :n"
)
_NO_CURSOR = UUID(int=0)


@router.get("/privacy/subjects/{userid}/transcripts")
async def export_subject(
    userid: str, request: Request, key: ApiKeyInfo = Depends(_export)
) -> JSONResponse:
    user = path_id(userid, "userid")
    raw_cursor = request.headers.get("X-Assess-Cursor")
    after = _NO_CURSOR
    if raw_cursor:
        try:
            after = UUID(raw_cursor)
        except ValueError:
            raise fail(bad_request("cursor")) from None
    _limit(request, key)
    items: list[dict[str, Any]] = []
    async with engine_of(request).begin() as conn:
        site = await site_of_key(conn, key, "privacy-export")
        rows = (
            await conn.execute(
                _EXPORT, {"s": site, "u": user, "after": after, "n": EXPORT_PAGE + 1}
            )
        ).all()
        more = len(rows) > EXPORT_PAGE
        for r in rows[:EXPORT_PAGE]:
            try:
                body = store.open_verified(
                    transcript_key_of(request),
                    r.id,
                    bytes(r.ciphertext),
                    bytes(r.dek_wrapped),
                    str(r.content_hash),
                )
            except store.TranscriptKeyUnavailable:
                raise fail(assess_unavailable("transcript_key")) from None
            except store.TranscriptIntegrityError:
                log.error(
                    "assess.transcript.alert",
                    alert="integrity_mismatch_at_export",
                    transcript_id=str(r.id),
                )
                raise fail(transcript_integrity()) from None
            items.append(
                {
                    "transcript_id": str(r.id),
                    "cmid": int(r.cmid),
                    "course_id": int(r.courseid_hint)
                    if r.courseid_hint is not None
                    else None,
                    "session_no": int(r.session_no),
                    "closed_at": iso_time(r.closed_at),
                    "filename": r.filename,
                    "sha256": str(r.content_hash),
                    "content_base64": base64.b64encode(body).decode("ascii"),
                }
            )
        await write_event(
            conn,
            AssessAuditEvent.PRIVACY_EXPORT,
            actor="api",
            site_id=site,
            data={"transcripts": len(items), "page_more": more, "key_id": key.key_id},
        )
    return JSONResponse(
        content={
            "items": items,
            "next_cursor": items[-1]["transcript_id"] if more and items else None,
        },
        headers={"Cache-Control": "no-store"},
    )


_ACT_SESSIONS = text(
    "SELECT cmid, session_no, state, close_reason, created_at, closed_at, courseid_hint "
    "FROM assess_sessions WHERE site_id = :s AND student_userid = :u AND cmid = ANY(:c) "
    "ORDER BY cmid, session_no"
)
_ACT_RESETS = text(
    "SELECT cmid, session_no, granted_at FROM assess_reset_grants WHERE site_id = :s "
    "AND student_userid = :u AND cmid = ANY(:c) ORDER BY cmid, granted_at, session_no"
)
_ACT_EXTRA = text(
    "SELECT cmid, sum(extra_seconds) AS extra FROM assess_extra_time WHERE site_id = :s "
    "AND student_userid = :u AND cmid = ANY(:c) GROUP BY cmid"
)
#: the course of an activity with no session: the hint a mint recorded, if any
_ACT_COURSE = text(
    "SELECT cmid, max(courseid_hint) AS course FROM assess_token_jti WHERE site_id = :s "
    "AND student_userid = :u AND cmid = ANY(:c) GROUP BY cmid"
)


def _cursor_int(request: Request) -> int:
    """An integer ``X-Assess-Cursor`` (a cmid or a user id), 0 when absent; 400 when malformed."""
    after = header_int(request, "X-Assess-Cursor") or 0
    if request.headers.get("X-Assess-Cursor") and not after:
        raise fail(bad_request("cursor"))
    return after


@router.get("/privacy/subjects/{userid}/activities")
async def subject_activities(
    userid: str, request: Request, key: ApiKeyInfo = Depends(_export)
) -> JSONResponse:
    user = path_id(userid, "userid")
    after = _cursor_int(request)
    _limit(request, key)
    async with engine_of(request).begin() as conn:
        site = await site_of_key(conn, key, "privacy-export")
        cmids = [
            int(c)
            for c in (
                await conn.execute(
                    SUBJECT_ACTIVITIES,
                    {"s": site, "u": user, "after": after, "n": EXPORT_PAGE + 1},
                )
            ).scalars()
        ]
        more = len(cmids) > EXPORT_PAGE
        cmids = cmids[:EXPORT_PAGE]
        items: dict[int, dict[str, Any]] = {
            c: {
                "cmid": c,
                "course_id": None,
                "sessions": [],
                "resets": [],
                "extra_seconds": 0,
            }
            for c in cmids
        }
        if cmids:
            params = {"s": site, "u": user, "c": cmids}
            for row in (await conn.execute(_ACT_COURSE, params)).all():
                if row.course is not None:
                    items[int(row.cmid)]["course_id"] = int(row.course)
            for row in (await conn.execute(_ACT_SESSIONS, params)).all():
                item = items[int(row.cmid)]
                if row.courseid_hint is not None:
                    item["course_id"] = int(
                        row.courseid_hint
                    )  # the session's own hint wins
                item["sessions"].append(
                    {
                        "session_no": int(row.session_no),
                        "state": str(row.state),
                        "close_reason": row.close_reason,
                        "created_at": iso_time(row.created_at),
                        "closed_at": iso_time(row.closed_at),
                    }
                )
            for row in (await conn.execute(_ACT_RESETS, params)).all():
                items[int(row.cmid)]["resets"].append(
                    {
                        "after_session_no": int(row.session_no),
                        "granted_at": iso_time(row.granted_at),
                    }
                )
            for row in (await conn.execute(_ACT_EXTRA, params)).all():
                items[int(row.cmid)]["extra_seconds"] = int(row.extra or 0)
        await write_event(
            conn,
            AssessAuditEvent.PRIVACY_EXPORT,
            actor="api",
            site_id=site,
            data={
                "kind": "activities",
                "activities": len(cmids),
                "page_more": more,
                "key_id": key.key_id,
            },
        )
    return JSONResponse(
        content={
            "items": [items[c] for c in cmids],
            "next_cursor": str(cmids[-1]) if more and cmids else None,
        },
        headers={"Cache-Control": "no-store"},
    )


SUBJECTS_PAGE = 500


@router.get("/privacy/activities/{cmid}/subjects")
async def activity_subjects(
    cmid: str, request: Request, key: ApiKeyInfo = Depends(_export)
) -> JSONResponse:
    activity = path_id(cmid, "cmid")
    after = _cursor_int(request)
    _limit(request, key)
    async with engine_of(request).begin() as conn:
        site = await site_of_key(conn, key, "privacy-export")
        users = [
            int(u)
            for u in (
                await conn.execute(
                    ACTIVITY_SUBJECTS,
                    {"s": site, "c": activity, "after": after, "n": SUBJECTS_PAGE + 1},
                )
            ).scalars()
        ]
        more = len(users) > SUBJECTS_PAGE
        users = users[:SUBJECTS_PAGE]
        await write_event(
            conn,
            AssessAuditEvent.PRIVACY_SUBJECTS_LISTED,
            actor="api",
            site_id=site,
            data={"cmid": activity, "subjects": len(users), "key_id": key.key_id},
        )
    return JSONResponse(
        content={
            "cmid": activity,
            "userids": users,
            "next_cursor": str(users[-1]) if more and users else None,
        },
        headers={"Cache-Control": "no-store"},
    )
