"""Shared helpers of the assess routes: envelopes, the engine, and body parsing without 422.

``parse_body`` reads the JSON body itself and validates it with the route's model, so a malformed
or over-length body is a 400 ``ERR-ASSESS-014`` envelope and never FastAPI's default 422 body
(DEBT-035). The validation error text is never echoed: it could quote the student's input.
"""

from __future__ import annotations

import json
from typing import Any

from fastapi import HTTPException, Request
from pydantic import BaseModel, ValidationError
from sqlalchemy.ext.asyncio import AsyncEngine

from vektra_assess.errors import assess_unavailable, bad_request
from vektra_shared.errors import ErrorResponse, http_status_for

#: no assess request body is legitimately larger than this (a turn is capped far below it)
MAX_BODY_BYTES = 64 * 1024


def fail(err: ErrorResponse, headers: dict[str, str] | None = None) -> HTTPException:
    hdrs = dict(headers or {})
    if err.retry_after is not None:
        hdrs.setdefault("Retry-After", str(err.retry_after))
    return HTTPException(
        status_code=http_status_for(err), detail=err.to_envelope(), headers=hdrs or None
    )


def engine_of(request: Request) -> AsyncEngine:
    engine = getattr(request.app.state, "assess_engine", None)
    if engine is None:
        raise fail(assess_unavailable("assess_database"))
    return engine  # type: ignore[no-any-return]


async def parse_body[M: BaseModel](
    request: Request, model: type[M], *, max_bytes: int = MAX_BODY_BYTES
) -> M:
    raw = await request.body()
    if len(raw) > max_bytes:
        raise fail(bad_request("body_too_large"))
    try:
        doc: Any = json.loads(raw or b"null")
    except (UnicodeDecodeError, json.JSONDecodeError):
        raise fail(bad_request("malformed_json")) from None
    if not isinstance(doc, dict):
        raise fail(bad_request("body_not_object"))
    try:
        return model.model_validate(doc)
    except ValidationError as exc:
        # the first failing field's location and type only, never the input value
        errs = exc.errors()
        loc = ".".join(str(p) for p in errs[0]["loc"])[:60] if errs else ""
        kind = str(errs[0]["type"])[:40] if errs else "invalid"
        raise fail(bad_request(f"{kind}:{loc}" if loc else kind)) from None
