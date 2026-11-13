"""Token verification dependencies of the student routes (COMP-002 fixed gate order).

The dependency names start with ``verify_assess_token``: the structural route test
(``vektra-app/tests/test_assess_route_scopes.py``) accepts an assess route only when it depends on
an exact scope or on one of these.

``verify_assess_token_pre`` is step 1 of the gate for the routes that take a pre-session token
(Start, ``/state``, resume): signature, kid, aud, iss, expiry, stage and ``loginas: false``. The
session routes go through ``session_gate`` (``vektra_assess.api.session_gate``), which runs every
step in order before any decrypt.
"""

from __future__ import annotations

from uuid import UUID

import jwt
from fastapi import Request

from vektra_assess.api.common import engine_of, fail
from vektra_assess.api.deps import bearer_token, tokens_of
from vektra_assess.api.site_binding import SiteBindingConflict, set_request_site
from vektra_assess.audit import AssessAuditEvent, write_event
from vektra_assess.errors import loginas_refused, token_invalid
from vektra_assess.tokens import PreClaims, SessionClaims, TokenError


async def _audit_token_failure(request: Request) -> None:
    """Step-1 failure on a session route: audited with the path sid only (no claim is trusted)."""
    try:
        sid: UUID | None = UUID(str(request.path_params.get("sid")))
    except ValueError:
        sid = None
    async with engine_of(request).begin() as conn:
        await write_event(
            conn,
            AssessAuditEvent.IN_SESSION_GATE_REJECTED,
            actor="api",
            session_id=sid,
            reason_code="token",
            data={"route": str(getattr(request.scope.get("route"), "name", ""))[:64]},
        )


async def _refuse_loginas(request: Request, exc: TokenError) -> None:
    """REQ-084: a signed token that does not assert ``loginas: false`` is a login-as refusal,
    audited as such (``assess_loginas_rejected``), 403, nothing written."""
    async with engine_of(request).begin() as conn:
        await write_event(
            conn,
            AssessAuditEvent.LOGINAS_REJECTED,
            actor="api",
            site_id=exc.site,
            reason_code="token_assertion_missing_or_false",
            data={"route": str(getattr(request.scope.get("route"), "name", ""))[:64]},
        )
    raise fail(loginas_refused())


def _bound[C: (PreClaims, SessionClaims)](claims: C) -> C:
    """ADR-0033 D9.2: the rest of the request is bound to the site the verified token names
    (each later transaction of the API engine sets ``app.assess_site_id`` at its start).

    A site the binding refuses (not a uuid, or another site already bound to this request) is a
    token failure like any other: the caller's ``except TokenError`` audits it and answers the
    401 envelope, never a 500."""
    try:
        set_request_site(claims.site)
    except (ValueError, TypeError, SiteBindingConflict):
        raise TokenError("claim_site") from None
    return claims


async def verify_assess_token_pre(request: Request) -> PreClaims:
    try:
        return _bound(tokens_of(request).verify(bearer_token(request), stage="pre"))
    except TokenError as exc:
        if exc.code == "loginas":
            await _refuse_loginas(request, exc)
        raise fail(token_invalid()) from None


async def verify_assess_token_session(request: Request) -> SessionClaims:
    try:
        return _bound(tokens_of(request).verify(_bearer(request), stage="session"))
    except TokenError as exc:
        if exc.code == "loginas":
            await _refuse_loginas(request, exc)
        await _audit_token_failure(request)
        raise fail(token_invalid()) from None


async def verify_assess_token_any(request: Request) -> SessionClaims | PreClaims:
    """``GET /sessions/{sid}``: a session token reads, a pre-session token resumes."""
    tokens = tokens_of(request)
    try:
        token = _bearer(request)
        stage = jwt.decode(token, options={"verify_signature": False}).get("stage")
        if stage == "pre":
            return _bound(tokens.verify(token, stage="pre"))
        return _bound(tokens.verify(token, stage="session"))
    except TokenError as exc:
        if exc.code == "loginas":
            await _refuse_loginas(request, exc)
        await _audit_token_failure(request)
        raise fail(token_invalid()) from None
    except jwt.InvalidTokenError:
        await _audit_token_failure(request)
        raise fail(token_invalid()) from None


def _bearer(request: Request) -> str:
    try:
        return bearer_token(request)
    except Exception:
        raise TokenError("no_bearer") from None
