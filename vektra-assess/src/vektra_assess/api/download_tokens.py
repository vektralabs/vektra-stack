"""Transcript download tokens (ARCH-067, ADR-0030): ``aud=transcript``, single use, <= 120 s.

Signed with the same ``assess_token_signing`` keyring as the discussion tokens but a different
audience, so a discussion token is never a download token and vice versa. Claims: ``sub`` (the
owner's or the grader's Moodle user id), ``tid``, ``site``, ``role`` (``owner`` | ``grader``),
``jti``. The jti is stored hashed in ``assess_transcript_download_tokens`` at mint and consumed
atomically at download, so a token works exactly once. ``verify`` raises ``TokenError`` for every
failure, mapped to one 401 envelope.
"""

from __future__ import annotations

import hashlib
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from typing import Literal
from uuid import UUID

import jwt

from vektra_assess.tokens import TokenError, new_jti
from vektra_shared.tokens import Keyring, decode_jwt, encode_jwt

AUDIENCE = "transcript"
MAX_TTL_S = 120
Role = Literal["owner", "grader"]
_REQUIRE = ("sub", "tid", "site", "role", "jti")


@dataclass(frozen=True)
class DownloadClaims:
    sub: int
    tid: UUID
    site: UUID
    role: Role
    jti: str


@dataclass(frozen=True)
class MintedDownload:
    token: str
    jti_hash: str
    expires_at: datetime
    ttl_s: int


def jti_hash(jti: str) -> str:
    return hashlib.sha256(jti.encode("ascii")).hexdigest()


class DownloadTokens:
    def __init__(self, keyring: Keyring, *, issuer: str, ttl_s: int) -> None:
        if not 0 < ttl_s <= MAX_TTL_S:
            raise ValueError("a download token lives at most 120 s (ARCH-067)")
        self._ring, self._iss, self._ttl = keyring, issuer, ttl_s

    def mint(self, *, sub: int, tid: UUID, site: UUID, role: Role) -> MintedDownload:
        jti = new_jti()
        token = encode_jwt(
            {
                "sub": str(sub),
                "tid": str(tid),
                "site": str(site),
                "role": role,
                "jti": jti,
            },
            key=self._ring.signing_key,
            audience=AUDIENCE,
            issuer=self._iss,
            ttl=timedelta(seconds=self._ttl),
        )
        exp = int(jwt.decode(token, options={"verify_signature": False})["exp"])
        return MintedDownload(
            token, jti_hash(jti), datetime.fromtimestamp(exp, tz=UTC), self._ttl
        )

    def verify(self, token: str) -> DownloadClaims:
        if not token or len(token) > 4096:
            raise TokenError("token_shape")
        try:
            claims = decode_jwt(
                token,
                keyring=self._ring,
                audience=AUDIENCE,
                issuer=self._iss,
                require=_REQUIRE,
            )
        except jwt.InvalidTokenError as exc:
            raise TokenError(type(exc).__name__) from None
        try:
            sub_raw, role, jti = claims["sub"], claims["role"], claims["jti"]
            if not isinstance(sub_raw, str) or not (
                sub_raw.isascii() and sub_raw.isdigit()
            ):
                raise ValueError
            if role not in ("owner", "grader") or not isinstance(jti, str):
                raise ValueError
            if int(claims["exp"]) - int(claims["iat"]) > MAX_TTL_S:
                raise ValueError
            return DownloadClaims(
                int(sub_raw),
                UUID(str(claims["tid"])),
                UUID(str(claims["site"])),
                role,
                jti,
            )
        except (KeyError, ValueError, TypeError):
            raise TokenError("claims") from None
