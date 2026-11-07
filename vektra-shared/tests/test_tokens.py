"""Negative matrix for vektra_shared.tokens (NFR-014, plan assess-foundations 3.2).

Each case asserts the exception TYPE: a decode that fails for the wrong reason (say, a
signature error where an audience error was expected) would hide a missing check.
"""

from __future__ import annotations

import base64
import json
from datetime import UTC, datetime, timedelta

import jwt
import pytest

from vektra_shared.tokens import (
    Keyring,
    SigningKey,
    UnknownKeyIdError,
    decode_jwt,
    encode_jwt,
)

KEY = SigningKey(kid="k1", secret=b"a" * 32)
OTHER = SigningKey(kid="k1", secret=b"b" * 32)  # same kid, different keyring
RING = Keyring.single(KEY)
AUD, ISS = "discussion", "vektra"


def _ok(**claims: object) -> str:
    return encode_jwt(
        claims, key=KEY, audience=AUD, issuer=ISS, ttl=timedelta(minutes=5)
    )


def _decode(token: str, **kw: object) -> dict:
    args = {"keyring": RING, "audience": AUD, "issuer": ISS, "require": ["sub"]}
    args.update(kw)
    return decode_jwt(token, **args)  # type: ignore[arg-type]


def _raw(
    payload: dict,
    headers: dict | None = None,
    key: bytes = KEY.secret,
    alg: str = "HS256",
) -> str:
    return jwt.encode(payload, key, algorithm=alg, headers=headers)


def _base(**over: object) -> dict:
    now = int(datetime.now(UTC).timestamp())
    p = {"sub": "u1", "aud": AUD, "iss": ISS, "iat": now, "exp": now + 300}
    p.update(over)
    return {k: v for k, v in p.items() if v is not None}


def test_round_trip() -> None:
    claims = _decode(_ok(sub="u1"))
    assert claims["sub"] == "u1" and claims["aud"] == AUD and claims["iss"] == ISS
    assert jwt.get_unverified_header(_ok(sub="u1"))["kid"] == "k1"


def test_wrong_aud() -> None:
    with pytest.raises(jwt.InvalidAudienceError):
        _decode(_raw(_base(aud="vektra-learn-widget"), {"kid": "k1"}))


def test_missing_aud() -> None:
    with pytest.raises(jwt.MissingRequiredClaimError, match="aud"):
        _decode(_raw(_base(aud=None), {"kid": "k1"}))


def test_list_aud_containing_audience_rejected() -> None:
    with pytest.raises(jwt.InvalidAudienceError):
        _decode(_raw(_base(aud=[AUD, "other"]), {"kid": "k1"}))


def test_wrong_iss() -> None:
    with pytest.raises(jwt.InvalidIssuerError):
        _decode(_raw(_base(iss="evil"), {"kid": "k1"}))


def test_missing_exp() -> None:
    with pytest.raises(jwt.MissingRequiredClaimError, match="exp"):
        _decode(_raw(_base(exp=None), {"kid": "k1"}))


def test_expired() -> None:
    past = int((datetime.now(UTC) - timedelta(hours=1)).timestamp())
    with pytest.raises(jwt.ExpiredSignatureError):
        _decode(_raw(_base(iat=past - 10, exp=past), {"kid": "k1"}))


def test_alg_none() -> None:
    header = base64.urlsafe_b64encode(
        json.dumps({"alg": "none", "typ": "JWT", "kid": "k1"}).encode()
    ).rstrip(b"=")
    body = base64.urlsafe_b64encode(json.dumps(_base()).encode()).rstrip(b"=")
    token = f"{header.decode()}.{body.decode()}."
    with pytest.raises(jwt.InvalidAlgorithmError):
        _decode(token)


def test_alg_hs512() -> None:
    with pytest.raises(jwt.InvalidAlgorithmError):
        _decode(_raw(_base(), {"kid": "k1"}, key=b"a" * 64, alg="HS512"))


def test_unknown_kid() -> None:
    with pytest.raises(UnknownKeyIdError):
        _decode(_raw(_base(), {"kid": "k9"}))


def test_missing_kid_header() -> None:
    with pytest.raises(UnknownKeyIdError):
        _decode(_raw(_base()))


def test_key_from_different_keyring() -> None:
    token = encode_jwt(
        {"sub": "u1"}, key=OTHER, audience=AUD, issuer=ISS, ttl=timedelta(minutes=5)
    )
    with pytest.raises(jwt.InvalidSignatureError):
        _decode(token)


def test_decode_without_audience_is_type_error() -> None:
    with pytest.raises(TypeError):
        decode_jwt(_ok(sub="u1"), keyring=RING, issuer=ISS, require=[])  # type: ignore[call-arg]


def test_encode_without_audience_is_type_error() -> None:
    with pytest.raises(TypeError):
        encode_jwt({}, key=KEY, issuer=ISS, ttl=timedelta(minutes=1))  # type: ignore[call-arg]


def test_require_is_merged_with_always_required() -> None:
    token = _raw(_base(sub=None), {"kid": "k1"})
    with pytest.raises(jwt.MissingRequiredClaimError, match="sub"):
        _decode(token)
    # an empty require list still enforces iat
    with pytest.raises(jwt.MissingRequiredClaimError, match="iat"):
        _decode(_raw(_base(iat=None), {"kid": "k1"}), require=[])


def test_encode_refuses_reserved_claims_and_bad_ttl() -> None:
    with pytest.raises(ValueError):
        encode_jwt(
            {"aud": "x"}, key=KEY, audience=AUD, issuer=ISS, ttl=timedelta(minutes=1)
        )
    with pytest.raises(ValueError):
        encode_jwt({}, key=KEY, audience=AUD, issuer=ISS, ttl=timedelta(0))


def test_key_validation_and_redaction() -> None:
    with pytest.raises(ValueError):
        SigningKey(kid="k", secret=b"short")
    with pytest.raises(ValueError):
        Keyring(active="missing", keys={"k1": KEY})
    assert "aaaa" not in repr(KEY) and "aaaa" not in repr(RING)


def test_rotation_old_kid_still_verifies() -> None:
    new = SigningKey(kid="k2", secret=b"c" * 32)
    ring = Keyring(active="k2", keys={"k1": KEY, "k2": new})
    old_token = _ok(sub="u1")
    assert (
        decode_jwt(old_token, keyring=ring, audience=AUD, issuer=ISS, require=[])["sub"]
        == "u1"
    )
    assert ring.signing_key is new
