"""Operator endpoints (plan assess-privacy-retention 12b, REQ-080). Exact ``admin`` scope.

``POST /api/v1/assess/ops/smtp-test``
    Queues ONE test send to the configured operator address (``VEKTRA_ASSESS_OPERATOR_EMAIL``).
    The request carries no recipient: an empty body (or ``{}``) only; any field is a 400. One test
    may be pending at a time (429 ``smtp_test_pending`` otherwise). 202 ``{"id"}``: the test id.
    503 ``ERR-ASSESS-001`` naming ``smtp_relay`` when the relay or the operator address is not
    configured.
``GET /api/v1/assess/ops/smtp-test/{id}``
    ``{"id", "status", "error_class", "smtp_code", "created_at", "sent_at"}``: ``queued`` until the
    worker has written its row (the API cannot read jobs, so an id that was never queued also
    reads ``queued``), then ``PENDING`` while it sends, ``SENT`` or ``FAILED`` with the error
    class (``tls``, ``connect``, ``temp_4xx``, ``perm_5xx``, ``auth``, ``relay_busy``,
    ``interrupted``). No address, no message.

No route reads the query string. Both are audited in the worker (outcome) or here (request).
"""

from __future__ import annotations

from typing import Any
from uuid import UUID, uuid4

from fastapi import APIRouter, Depends, Request
from fastapi.responses import JSONResponse
from sqlalchemy import text

from vektra_assess.api.common import (
    engine_of,
    fail,
    iso_time,
    json_object,
    read_capped_body,
)
from vektra_assess.api.deps import settings_of
from vektra_assess.audit import AssessAuditEvent, write_event
from vektra_assess.errors import assess_unavailable, bad_request, rate_limited
from vektra_assess.jobs import JobKind, enqueue_job
from vektra_shared.auth import ApiKeyInfo, require_exact_scope

router = APIRouter(prefix="/ops")
_admin = require_exact_scope("admin")

#: one test pending at a time: a second request while one waits is a 429
PENDING_KEY = "smtp_test:pending"

_RESULT = text(
    "SELECT status, last_error_class, smtp_code, created_at, sent_at FROM assess_deliveries "
    "WHERE id = :i AND kind = 'smtp_test'"
)


@router.post("/smtp-test", status_code=202)
async def queue_smtp_test(
    request: Request, key: ApiKeyInfo = Depends(_admin)
) -> JSONResponse:
    if json_object(await read_capped_body(request, max_bytes=256) or b"{}"):
        raise fail(
            bad_request("no_fields_accepted")
        )  # never a recipient from the request
    settings = settings_of(request)
    if not (settings.smtp_host and settings.smtp_from and settings.operator_email):
        raise fail(assess_unavailable("smtp_relay"))
    test_id = uuid4()
    async with engine_of(request).begin() as conn:
        queued = await enqueue_job(
            conn,
            JobKind.SMTP_TEST_SEND,
            {"test_id": str(test_id)},
            dedupe_key=PENDING_KEY,
            max_attempts=1,
        )
        if not queued:
            raise fail(rate_limited(30, "smtp_test_pending"))
        await write_event(
            conn,
            AssessAuditEvent.SMTP_TEST_REQUESTED,
            actor="api",
            data={"test_id": str(test_id), "key_id": str(key.key_id)},
        )
    return JSONResponse({"id": str(test_id)}, status_code=202)


def _test_id(raw: str) -> UUID:
    try:
        return UUID(raw)
    except ValueError:
        raise fail(bad_request("id")) from None


@router.get("/smtp-test/{test_id}")
async def read_smtp_test(
    test_id: str, request: Request, key: ApiKeyInfo = Depends(_admin)
) -> dict[str, Any]:
    tid = _test_id(test_id)
    async with engine_of(request).connect() as conn:
        row = (await conn.execute(_RESULT, {"i": tid})).first()
    if row is None:
        return {"id": str(tid), "status": "queued", "error_class": None,
                "smtp_code": None, "created_at": None, "sent_at": None}  # fmt: skip
    return {
        "id": str(tid),
        "status": row.status,
        "error_class": row.last_error_class,
        "smtp_code": row.smtp_code,
        "created_at": iso_time(row.created_at),
        "sent_at": iso_time(row.sent_at),
    }
