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

from fastapi import Request

from vektra_assess.api.common import fail
from vektra_assess.api.deps import bearer_token, tokens_of
from vektra_assess.errors import token_invalid
from vektra_assess.tokens import PreClaims, SessionClaims, TokenError


async def verify_assess_token_pre(request: Request) -> PreClaims:
    try:
        return tokens_of(request).verify(bearer_token(request), stage="pre")
    except TokenError:
        raise fail(token_invalid()) from None


async def verify_assess_token_session(request: Request) -> SessionClaims:
    try:
        return tokens_of(request).verify(bearer_token(request), stage="session")
    except TokenError:
        raise fail(token_invalid()) from None
