"""Shared JWT helpers with mandatory audience, issuer and key id (NFR-014).

Every Vektra token (learn widget, assess discussion and transcript tokens) goes through
these two functions. They exist so that a token minted for one audience can never be
accepted by another, and so that an unbound decode cannot be written by accident:

- every parameter is keyword-only, and ``audience``, ``issuer`` and ``require`` have no
  default, so forgetting one is a ``TypeError`` at the call site, not a silent unbound
  decode;
- the algorithm is pinned to HS256 (``alg: none`` and any other algorithm are rejected);
- the ``kid`` header is mandatory and must name a key of the verifying keyring;
- ``exp``, ``iat``, ``aud`` and ``iss`` are always required, whatever ``require`` says.

All failures raise subclasses of ``jwt.InvalidTokenError``, so existing callers that catch
it keep working.
"""

from __future__ import annotations

from collections.abc import Mapping, Sequence
from dataclasses import dataclass, field
from datetime import UTC, datetime, timedelta
from typing import Any

import jwt

ALGORITHM = "HS256"
ALWAYS_REQUIRED: frozenset[str] = frozenset({"exp", "iat", "aud", "iss"})
MIN_KEY_BYTES = 32


class UnknownKeyIdError(jwt.InvalidTokenError):
    """The token's ``kid`` header is missing or names no key of the keyring."""


@dataclass(frozen=True)
class SigningKey:
    """One HMAC key with its key id. The key bytes never appear in ``repr``."""

    kid: str
    secret: bytes = field(repr=False)

    def __post_init__(self) -> None:
        if not self.kid:
            raise ValueError("SigningKey.kid must be non-empty")
        if len(self.secret) < MIN_KEY_BYTES:
            raise ValueError(
                f"SigningKey {self.kid!r}: secret must be at least {MIN_KEY_BYTES} bytes"
            )


@dataclass(frozen=True)
class Keyring:
    """The verification keys of one token family, plus the active signing kid.

    Rotation (ARCH-071) adds a kid and flips ``active``; old kids stay verifiable until
    they are removed.
    """

    active: str
    keys: Mapping[str, SigningKey] = field(repr=False)

    def __post_init__(self) -> None:
        if self.active not in self.keys:
            raise ValueError(
                f"Keyring active kid {self.active!r} is not in the keyring"
            )
        for kid, key in self.keys.items():
            if key.kid != kid:
                raise ValueError(
                    f"Keyring entry {kid!r} holds a key with kid {key.kid!r}"
                )

    @classmethod
    def single(cls, key: SigningKey) -> Keyring:
        return cls(active=key.kid, keys={key.kid: key})

    @property
    def signing_key(self) -> SigningKey:
        return self.keys[self.active]

    def __repr__(self) -> str:
        return f"Keyring(active={self.active!r}, kids={sorted(self.keys)!r})"


def encode_jwt(
    claims: Mapping[str, Any],
    *,
    key: SigningKey,
    audience: str,
    issuer: str,
    ttl: timedelta,
) -> str:
    """Sign ``claims`` with ``key``; sets ``aud``, ``iss``, ``iat``, ``exp`` and ``kid``."""
    if not audience or not issuer:
        raise ValueError("encode_jwt: audience and issuer must be non-empty")
    if ttl <= timedelta(0):
        raise ValueError("encode_jwt: ttl must be positive")
    reserved = {"aud", "iss", "iat", "exp"} & set(claims)
    if reserved:
        raise ValueError(f"encode_jwt: claims must not set {sorted(reserved)}")
    now = datetime.now(UTC)
    payload = {
        **claims,
        "aud": audience,
        "iss": issuer,
        "iat": int(now.timestamp()),
        "exp": int((now + ttl).timestamp()),
    }
    return jwt.encode(
        payload, key.secret, algorithm=ALGORITHM, headers={"kid": key.kid}
    )


def decode_jwt(
    token: str,
    *,
    keyring: Keyring,
    audience: str,
    issuer: str,
    require: Sequence[str],
    leeway_s: int = 0,
) -> dict[str, Any]:
    """Verify ``token`` against ``keyring`` and return its claims.

    Raises a ``jwt.InvalidTokenError`` subclass on any failure.
    """
    if not audience or not issuer:
        raise ValueError("decode_jwt: audience and issuer must be non-empty")
    header = jwt.get_unverified_header(token)
    if header.get("alg") != ALGORITHM:
        raise jwt.InvalidAlgorithmError(
            f"algorithm {header.get('alg')!r} is not allowed"
        )
    kid = header.get("kid")
    if not isinstance(kid, str) or kid not in keyring.keys:
        raise UnknownKeyIdError("token kid is missing or unknown")
    claims: dict[str, Any] = jwt.decode(
        token,
        keyring.keys[kid].secret,
        algorithms=[ALGORITHM],
        audience=audience,
        issuer=issuer,
        leeway=leeway_s,
        options={
            "require": sorted(ALWAYS_REQUIRED | set(require)),
            "verify_signature": True,
            "verify_exp": True,
            "verify_iat": True,
            "verify_aud": True,
            "verify_iss": True,
            # aud must be exactly the audience string, not a list that contains it
            "strict_aud": True,
        },
    )
    return claims
