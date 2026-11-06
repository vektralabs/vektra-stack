"""Per-process state of the student routes: settings, the token service, the bearer token.

``bearer_token`` reads the ``Authorization`` header and nothing else: never the query string,
never a cookie (ADR-0030). A request carrying ``?token=`` or ``?access_token=`` without the header
is a 401 like any other request without a token.
"""

from __future__ import annotations

from fastapi import Request

from vektra_assess.api.common import fail
from vektra_assess.config import VektraAssessSettings
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


def bearer_token(request: Request) -> str:
    """The bearer token from the Authorization header ONLY; anything else is a 401."""
    header = request.headers.get("authorization", "")
    scheme, _, value = header.partition(" ")
    if scheme.lower() != "bearer" or not value.strip():
        raise fail(token_invalid())
    return value.strip()


def parse_version(raw: str) -> tuple[int, ...] | None:
    """``1.2.3`` -> ``(1, 2, 3)``; a suffix after ``-`` or ``+`` is ignored; junk -> None."""
    core = raw.strip().split("-", 1)[0].split("+", 1)[0]
    parts = core.split(".")
    if not 1 <= len(parts) <= 4 or not all(p.isdigit() and len(p) <= 10 for p in parts):
        return None
    nums = [int(p) for p in parts]
    return tuple(nums + [0] * (4 - len(nums)))


def version_at_least(raw: str, *minimums: str | None) -> bool:
    got = parse_version(raw)
    if got is None:
        return False
    for minimum in minimums:
        if minimum is None:
            continue
        need = parse_version(minimum)
        if need is None or got < need:
            return False  # an unparsable minimum fails closed
    return True
