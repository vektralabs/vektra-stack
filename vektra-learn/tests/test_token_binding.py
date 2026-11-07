"""Learn and assess tokens are mutually rejected; learn is no looser than before 0.8.

Plan assess-foundations task 3.5 (NFR-014). Before the shared helpers, validate_token
decoded HS256 with no audience, issuer, kid or required exp. Each case below was either
already rejected (and must stay so) or was accepted before and is rejected now.
"""

from __future__ import annotations

from datetime import UTC, datetime, timedelta
from unittest.mock import AsyncMock, MagicMock

import jwt
import pytest

from vektra_learn.service import (
    TOKEN_ISSUER,
    WIDGET_AUDIENCE,
    WIDGET_KID,
    LearnService,
    TokenRequest,
)
from vektra_shared.tokens import Keyring, SigningKey, decode_jwt, encode_jwt

SECRET = "learn-secret-for-binding-tests-0123"
SVC = LearnService(jwt_secret=SECRET)
LEARN_KEY = SigningKey(kid=WIDGET_KID, secret=SECRET.encode())


def _now() -> datetime:
    return datetime.now(UTC)


async def _minted() -> str:
    session = MagicMock()
    session.add = MagicMock()
    session.flush = AsyncMock()
    result = await SVC.generate_token(
        session, TokenRequest(student_id="s1", course_id="C1")
    )
    return result.token


async def test_discussion_audience_token_with_learn_secret_rejected() -> None:
    token = encode_jwt(
        {"sub": "s1", "course_id": "C1"},
        key=LEARN_KEY,
        audience="discussion",
        issuer=TOKEN_ISSUER,
        ttl=timedelta(minutes=5),
    )
    with pytest.raises(jwt.InvalidAudienceError):
        await SVC.validate_token(token)


async def test_learn_token_rejected_by_assess_decode() -> None:
    token = await _minted()
    # even with the very same key in the keyring, the audience does not match
    with pytest.raises(jwt.InvalidAudienceError):
        decode_jwt(
            token,
            keyring=Keyring.single(LEARN_KEY),
            audience="discussion",
            issuer=TOKEN_ISSUER,
            require=["sub"],
        )


async def test_minted_token_round_trips() -> None:
    claims = await SVC.validate_token(await _minted())
    assert claims["sub"] == "s1" and claims["aud"] == WIDGET_AUDIENCE


async def test_pre_upgrade_token_rejected_once() -> None:
    """The 0.7 shape (no aud/iss/kid) was accepted; it is now refused (widget re-mints)."""
    legacy = jwt.encode(
        {"sub": "s1", "course_id": "C1", "exp": _now() + timedelta(hours=1)},
        SECRET,
        algorithm="HS256",
    )
    with pytest.raises(jwt.InvalidTokenError):
        await SVC.validate_token(legacy)


async def test_missing_exp_now_rejected() -> None:
    """Previously accepted (exp was not required)."""
    token = jwt.encode(
        {
            "sub": "s1",
            "course_id": "C1",
            "aud": WIDGET_AUDIENCE,
            "iss": TOKEN_ISSUER,
            "iat": _now(),
        },
        SECRET,
        algorithm="HS256",
        headers={"kid": WIDGET_KID},
    )
    with pytest.raises(jwt.MissingRequiredClaimError, match="exp"):
        await SVC.validate_token(token)


async def test_missing_course_id_rejected() -> None:
    token = encode_jwt(
        {"sub": "s1"},
        key=LEARN_KEY,
        audience=WIDGET_AUDIENCE,
        issuer=TOKEN_ISSUER,
        ttl=timedelta(minutes=5),
    )
    with pytest.raises(jwt.MissingRequiredClaimError, match="course_id"):
        await SVC.validate_token(token)


async def test_still_rejected_cases() -> None:
    """Cases the 0.7 decoder already refused stay refused."""
    base = {
        "sub": "s1",
        "course_id": "C1",
        "aud": WIDGET_AUDIENCE,
        "iss": TOKEN_ISSUER,
        "iat": _now() - timedelta(hours=2),
    }
    expired = jwt.encode(
        {**base, "exp": _now() - timedelta(hours=1)},
        SECRET,
        algorithm="HS256",
        headers={"kid": WIDGET_KID},
    )
    with pytest.raises(jwt.ExpiredSignatureError):
        await SVC.validate_token(expired)
    forged = jwt.encode(
        {**base, "exp": _now() + timedelta(hours=1)},
        "another-secret-of-at-least-32-bytes",
        algorithm="HS256",
        headers={"kid": WIDGET_KID},
    )
    with pytest.raises(jwt.InvalidSignatureError):
        await SVC.validate_token(forged)
