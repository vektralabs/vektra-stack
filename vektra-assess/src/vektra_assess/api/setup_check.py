"""REQ-079 setup-check endpoints (the LMS web-service plan (20261101-154950), group F).

POST queues a check (report row + ``setup_check`` job + NOTIFY, one transaction); the worker
runs it. GET returns the content-free report. Callable by an operator ``admin`` key or by the
site's OWN ``assess:teacher`` key: ``require_any_exact_scope`` accepts exactly those two (no
superscope), and a narrow key must be bound to the site in ``assess_site_keys``. Unknown site,
unknown report and "not your site" are all the same 404 (no cross-site probing).

Rate limit: one setup check per site per 5 minutes, enforced in the database (a report row
younger than 5 minutes), so it holds across restarts and replicas; the in-memory sliding
window of ``vektra_admin.rate_limit`` is a fixed 60 s and cannot express it.
"""

from __future__ import annotations

from datetime import datetime
from typing import Any
from uuid import UUID

from fastapi import APIRouter, Depends, HTTPException, Request
from fastapi.responses import JSONResponse
from sqlalchemy import text
from sqlalchemy.ext.asyncio import AsyncConnection, AsyncEngine

from vektra_assess.errors import (
    assess_unavailable,
    setup_check_not_found,
    setup_check_too_soon,
)
from vektra_shared.auth import ApiKeyInfo, require_any_exact_scope
from vektra_shared.errors import ErrorResponse, http_status_for

MIN_INTERVAL_S = 300
HEARTBEAT_MAX_AGE_S = 60

router = APIRouter(prefix="/sites/{site_id}/setup-check")
_guard = require_any_exact_scope("admin", "assess:teacher")


def _fail(err: ErrorResponse, headers: dict[str, str] | None = None) -> HTTPException:
    return HTTPException(
        status_code=http_status_for(err), detail=err.to_envelope(), headers=headers
    )


def _engine(request: Request) -> AsyncEngine:
    engine = getattr(request.app.state, "assess_engine", None)
    if engine is None:
        raise _fail(assess_unavailable("assess_database"))
    return engine  # type: ignore[no-any-return]


async def _authorize(conn: AsyncConnection, key: ApiKeyInfo, site_id: UUID) -> None:
    exists = (
        await conn.execute(
            text("SELECT 1 FROM assess_sites WHERE id = :s"), {"s": site_id}
        )
    ).first()
    if exists is None:
        raise _fail(setup_check_not_found())
    if "admin" in key.scopes:
        return
    bound = (
        await conn.execute(
            text(
                "SELECT 1 FROM assess_site_keys WHERE api_key_id = :k AND site_id = :s "
                "AND purpose = 'teacher'"
            ),
            {"k": key.key_id, "s": site_id},
        )
    ).first()
    if bound is None:
        raise _fail(setup_check_not_found())


@router.post("", status_code=202)
async def request_setup_check(
    site_id: UUID, request: Request, key: ApiKeyInfo = Depends(_guard)
) -> JSONResponse:
    async with _engine(request).begin() as conn:
        await _authorize(conn, key, site_id)
        await conn.execute(
            text("SELECT pg_advisory_xact_lock(hashtextextended(:k, 0))"),
            {"k": f"setup_check:{site_id}"},
        )
        age = (
            await conn.execute(
                text(
                    "SELECT extract(epoch FROM now() - beat_at) FROM assess_heartbeat "
                    "WHERE component = 'worker'"
                )
            )
        ).scalar()
        if age is None or float(age) > HEARTBEAT_MAX_AGE_S:
            raise _fail(assess_unavailable("worker_heartbeat"))
        recent = (
            await conn.execute(
                text(
                    "SELECT extract(epoch FROM now() - max(created_at)) "
                    "FROM assess_setup_check_reports WHERE site_id = :s "
                    "AND created_at > now() - make_interval(secs => :w)"
                ),
                {"s": site_id, "w": MIN_INTERVAL_S},
            )
        ).scalar()
        if recent is not None:
            wait = int(MIN_INTERVAL_S - float(recent))
            raise _fail(setup_check_too_soon(wait), {"Retry-After": str(max(wait, 1))})
        report_id = (
            await conn.execute(
                text(
                    "INSERT INTO assess_setup_check_reports (site_id, requested_by_key_id) "
                    "VALUES (:s, :k) RETURNING id"
                ),
                {"s": site_id, "k": key.key_id},
            )
        ).scalar_one()
        await conn.execute(
            text(
                "INSERT INTO assess_jobs (kind, args, dedupe_key) VALUES ('setup_check', "
                "jsonb_build_object('site_id', CAST(:s AS text), 'report_id', CAST(:r AS text)), :d)"
            ),
            {
                "s": str(site_id),
                "r": str(report_id),
                "d": f"setup_check:{site_id}:{report_id}",
            },
        )
        await conn.execute(text("SELECT pg_notify('assess_jobs', '')"))
    return JSONResponse(status_code=202, content={"report_id": str(report_id)})


def _iso(value: datetime | None) -> str | None:
    return value.isoformat() if value is not None else None


@router.get("/{report_id}")
async def get_setup_check(
    site_id: UUID, report_id: UUID, request: Request, key: ApiKeyInfo = Depends(_guard)
) -> dict[str, Any]:
    async with _engine(request).connect() as conn:
        await _authorize(conn, key, site_id)
        row = (
            await conn.execute(
                text(
                    "SELECT status, lms_release, items, created_at, finished_at "
                    "FROM assess_setup_check_reports WHERE id = :r AND site_id = :s"
                ),
                {"r": report_id, "s": site_id},
            )
        ).one_or_none()
    if row is None:
        raise _fail(setup_check_not_found())
    # content-free by construction: the worker stores only {name, result, reason_code}
    items = [
        {k: i.get(k) for k in ("name", "result", "reason_code")}
        for i in (row.items or [])
    ]
    return {
        "report_id": str(report_id),
        "site_id": str(site_id),
        "status": row.status,
        "lms_release": row.lms_release,
        "items": items,
        "created_at": _iso(row.created_at),
        "finished_at": _iso(row.finished_at),
    }
