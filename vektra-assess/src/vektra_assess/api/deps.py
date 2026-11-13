"""Per-process state of the student routes: settings, the token service, the bearer token. The
request's site binding (ADR-0033 D9.2) is ``vektra_assess.api.site_binding``.

``bearer_token`` reads the ``Authorization`` header and nothing else: never the query string,
never a cookie (ADR-0030). A request carrying ``?token=`` or ``?access_token=`` without the header
is a 401 like any other request without a token.
"""

from __future__ import annotations

from fastapi import Request

from vektra_assess.api.common import fail
from vektra_assess.api.material import DekCache
from vektra_assess.config import VektraAssessSettings
from vektra_assess.config import parse_version as parse_version
from vektra_assess.config import version_at_least as version_at_least
from vektra_assess.errors import assess_unavailable, token_invalid
from vektra_assess.keys import AssessKeyring
from vektra_assess.tokens import SIGNING_SECRET, AssessTokens


def settings_of(request: Request) -> VektraAssessSettings:
    settings = getattr(request.app.state, "assess_settings", None)
    if settings is None:
        settings = VektraAssessSettings()
        request.app.state.assess_settings = settings
    return settings


def tokens_of(request: Request) -> AssessTokens:
    tokens = getattr(request.app.state, "assess_tokens", None)
    if tokens is not None:
        return tokens  # type: ignore[no-any-return]
    keys = getattr(request.app.state, "assess_keys", None) or {}
    signing = keys.get(SIGNING_SECRET)
    if not isinstance(signing, AssessKeyring):
        raise fail(assess_unavailable("token_signing_key"))
    tokens = AssessTokens.from_settings(signing, settings_of(request))
    request.app.state.assess_tokens = tokens
    return tokens


HANDOFF_PRIVATE = "assess_handoff_private"


def handoff_private_of(request: Request) -> AssessKeyring:
    keys = getattr(request.app.state, "assess_keys", None) or {}
    ring = keys.get(HANDOFF_PRIVATE)
    if not isinstance(ring, AssessKeyring):
        raise fail(assess_unavailable("handoff_private_key"))
    return ring


def dek_cache_of(request: Request) -> DekCache:
    cache = getattr(request.app.state, "assess_dek_cache", None)
    if cache is None:
        cache = DekCache()
        request.app.state.assess_dek_cache = cache
    return cache


def bearer_token(request: Request) -> str:
    """The bearer token from the Authorization header ONLY; anything else is a 401."""
    header = request.headers.get("authorization", "")
    scheme, _, value = header.partition(" ")
    if scheme.lower() != "bearer" or not value.strip():
        raise fail(token_invalid())
    return value.strip()
