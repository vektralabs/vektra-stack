"""Shared helpers of the assess routes: envelopes, the engine, and body parsing without 422.

``parse_body`` reads the JSON body itself and validates it with the route's model, so a malformed
or over-length body is a 400 ``ERR-ASSESS-014`` envelope and never FastAPI's default 422 body
(DEBT-035). The validation error text is never echoed: it could quote the student's input.
"""

from __future__ import annotations

import json
from datetime import UTC, datetime
from typing import Any

from fastapi import HTTPException, Request
from pydantic import BaseModel, ValidationError
from sqlalchemy import text
from sqlalchemy.ext.asyncio import AsyncEngine

from vektra_assess.api.site_binding import bind_site
from vektra_assess.errors import assess_unavailable, bad_request, key_not_bound
from vektra_assess.gates import Opening
from vektra_shared.errors import ErrorResponse, http_status_for

#: no assess request body is legitimately larger than this (a turn is capped far below it)
MAX_BODY_BYTES = 64 * 1024


#: the site a key is bound to for ``purpose`` (``mint`` | ``teacher`` | ``privacy-*``), whether it
#: is disabled, and its plugin minimum. A SECURITY DEFINER function (revision a0002): the API
#: role's own reads of ``assess_site_keys`` are bound to one site by RLS, and this lookup is what
#: names the site, so it runs before the binding
SITE_OF_KEY = text(
    "SELECT id, min_plugin_version, disabled FROM assess_site_of_key(:k, :p)"
)


async def key_site_row(conn: Any, key: Any, purpose: str) -> Any:
    """The key's site row for ``purpose`` (id, min_plugin_version, disabled), or None. Binds
    nothing: the caller decides first whether the site is acceptable."""
    return (await conn.execute(SITE_OF_KEY, {"k": key.key_id, "p": purpose})).first()


async def site_row_of_key(conn: Any, key: Any, purpose: str) -> Any:
    """The ENABLED site row a key is bound to for ``purpose``, now bound for this request and
    this transaction (ADR-0033 D9.2); 403 ``ERR-ASSESS-007`` when it is bound to none (one rule
    for every site-bound route)."""
    row = await key_site_row(conn, key, purpose)
    if row is None or row.disabled:
        raise fail(key_not_bound(purpose))
    await bind_site(conn, row.id)
    return row


async def site_of_key(conn: Any, key: Any, purpose: str) -> Any:
    """The enabled site a key is bound to for ``purpose``, bound for the rest of the request."""
    return (await site_row_of_key(conn, key, purpose)).id


def iso_time(value: Any) -> str | None:
    """The one epoch / datetime -> ISO 8601 rule of the assess views: epoch seconds as an
    ``int`` (0 included: 1970-01-01T00:00:00+00:00, a value, not "absent"), or an aware
    ``datetime``, as ISO 8601; ``None``, an epoch outside datetime's range and anything else (a
    bool, a float, a string) is None.
    """
    if isinstance(value, datetime):
        return value.isoformat()
    if isinstance(value, int) and not isinstance(value, bool):
        try:
            return datetime.fromtimestamp(value, UTC).isoformat()
        except (OverflowError, ValueError, OSError):
            return None  # beyond datetime's range (an LMS value): absent, never a 500
    return None


def opening_json(view: Opening) -> dict[str, Any]:
    """``opening`` of the teacher config view and of the session list's gate panel (one shape
    for both pages): ``{at, source, fallback_required}``."""
    return {
        "at": iso_time(view.at),
        "source": view.source,
        "fallback_required": view.fallback_required,
    }


def fail(err: ErrorResponse, headers: dict[str, str] | None = None) -> HTTPException:
    hdrs = dict(headers or {})
    if err.retry_after is not None:
        hdrs.setdefault("Retry-After", str(err.retry_after))
    return HTTPException(
        status_code=http_status_for(err), detail=err.to_envelope(), headers=hdrs or None
    )


def engine_of(request: Request) -> AsyncEngine:
    """The API engine. Every transaction of it is bound to the request's site at its start:
    ``site_binding.install_site_binding`` is called once where the engine is created
    (``vektra_app.assess_wiring`` step 12)."""
    engine = getattr(request.app.state, "assess_engine", None)
    if engine is None:
        raise fail(assess_unavailable("assess_database"))
    return engine  # type: ignore[no-any-return]


async def read_capped_body(
    request: Request, *, max_bytes: int = MAX_BODY_BYTES
) -> bytes:
    """The raw body, streamed with the cap applied per chunk: an oversized body is refused as
    soon as it passes ``max_bytes``, never buffered whole first (a declared Content-Length over
    the cap is refused at once, a chunked body as soon as it crosses it)."""
    declared = request.headers.get("content-length", "")
    if declared.isascii() and declared.isdigit():
        # leading zeros stripped, and a length too long to be a size at all is over the cap by
        # itself: ``int()`` refuses more than 4300 digits
        digits = declared.lstrip("0") or "0"
        if len(digits) > len(str(max_bytes)) or int(digits) > max_bytes:
            raise fail(bad_request("body_too_large"))
    buf = bytearray()
    async for chunk in request.stream():
        if len(buf) + len(chunk) > max_bytes:
            raise fail(bad_request("body_too_large"))
        buf += chunk
    return bytes(buf)


def json_object(raw: bytes) -> dict[str, Any]:
    """Decode a JSON object; anything else (bad UTF-8 or JSON, nesting deep enough to exhaust
    the decoder's recursion, a non-object) is the 400 envelope, never a 500."""
    try:
        doc: Any = json.loads(raw or b"null")
    except (UnicodeDecodeError, ValueError, RecursionError):
        raise fail(bad_request("malformed_json")) from None
    if not isinstance(doc, dict):
        raise fail(bad_request("body_not_object"))
    return doc


def validate_body[M: BaseModel](doc: dict[str, Any], model: type[M]) -> M:
    try:
        return model.model_validate(doc)
    except ValidationError as exc:
        # the first failing field's location and type only, never the input value
        errs = exc.errors()
        loc = ".".join(str(p) for p in errs[0]["loc"])[:60] if errs else ""
        kind = str(errs[0]["type"])[:40] if errs else "invalid"
        raise fail(bad_request(f"{kind}:{loc}" if loc else kind)) from None


async def parse_body[M: BaseModel](
    request: Request, model: type[M], *, max_bytes: int = MAX_BODY_BYTES
) -> M:
    raw = await read_capped_body(request, max_bytes=max_bytes)
    return validate_body(json_object(raw), model)
