"""Discussion tokens (ARCH-067, ADR-0030): pre-session and session-bound, one signing keyring.

Every token is HS256, signed with the ACTIVE kid of the ``assess_token_signing`` keyring and
verified against every kid it holds (rotation: the current and the previous kid stay valid until
the previous is removed). ``aud`` is always ``discussion`` and ``iss`` is
``VEKTRA_ASSESS_TOKEN_ISSUER``; decoding goes only through ``vektra_shared.tokens.decode_jwt``, so
aud, iss, exp, iat and kid are always required.

- *Pre-session* (minted by ``POST /tokens``): ``sub, site, cmid, jti, loginas=false,
  stage="pre"``, ``exp <= iat + 120 s``. Single use: Start or resume consumes its jti.
- *Session-bound* (returned by Start and resume): ``sub, site, cmid, sid, seq, epoch, jti,
  loginas=false, stage="session"``, ``exp <= min(deadline + grace, iat + session TTL)``. ``seq``
  is the session number N; ``epoch`` the session's revocation counter.

``verify`` raises ``TokenError`` for EVERY failure (signature, kid, aud, iss, expiry, stage, a
missing or non-false ``loginas``, a malformed claim). The routes map it to one 401 envelope, so a
caller cannot tell which check refused it. A token without ``loginas: false`` is refused here, not
merely at mint (REQ-084).
"""

from __future__ import annotations

import base64
import secrets
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from typing import Literal, overload
from uuid import UUID

import jwt

from vektra_assess.config import VektraAssessSettings
from vektra_assess.keys import AssessKeyring
from vektra_shared.tokens import Keyring, SigningKey, decode_jwt, encode_jwt

AUDIENCE = "discussion"
SIGNING_SECRET = "assess_token_signing"
Stage = Literal["pre", "session"]

# ``loginas`` is NOT in the decoder's required list: its absence must be reported as a login-as
# refusal (REQ-084), not as a generic malformed token, so ``verify`` checks it itself
_COMMON = ("sub", "site", "cmid", "jti", "stage")
_REQUIRE: dict[str, tuple[str, ...]] = {
    "pre": _COMMON,
    "session": (*_COMMON, "sid", "seq", "epoch"),
}


class TokenError(Exception):
    """Any token failure. The message is a content-free code, never the token or a claim.

    ``site`` is set only for a ``loginas`` refusal of a token whose SIGNATURE verified (the site
    claim is then the issuer's own statement), so the route can audit it against its site.
    """

    def __init__(self, code: str, *, site: UUID | None = None) -> None:
        super().__init__(code)
        self.code = code
        self.site = site


@dataclass(frozen=True)
class PreClaims:
    sub: int
    site: UUID
    cmid: int
    jti: str
    iat: int
    exp: int


@dataclass(frozen=True)
class SessionClaims:
    sub: int
    site: UUID
    cmid: int
    sid: UUID
    seq: int
    epoch: int
    jti: str
    iat: int
    exp: int


@dataclass(frozen=True)
class Minted:
    token: str
    jti: str
    expires_at: datetime
    ttl_s: int


def new_jti() -> str:
    return base64.urlsafe_b64encode(secrets.token_bytes(18)).decode("ascii")


def keyring_from(assess: AssessKeyring) -> Keyring:
    """The shared verification keyring of every kid; signing uses ``assess.active``."""
    return Keyring(
        active=assess.active,
        keys={kid: SigningKey(kid, key) for kid, key in assess.keys.items()},
    )


def _int(claims: dict[str, object], name: str) -> int:
    value = claims.get(name)
    if isinstance(value, bool) or not isinstance(value, int) or value < 0:
        raise TokenError(f"claim_{name}")
    return value


def _uuid(claims: dict[str, object], name: str) -> UUID:
    value = claims.get(name)
    if not isinstance(value, str):
        raise TokenError(f"claim_{name}")
    try:
        return UUID(value)
    except ValueError:
        raise TokenError(f"claim_{name}") from None


class AssessTokens:
    def __init__(
        self,
        keyring: Keyring,
        *,
        issuer: str,
        presession_ttl_s: int,
        session_ttl_s: int,
        grace_s: int,
    ) -> None:
        if not 0 < presession_ttl_s <= 120:
            raise ValueError("a pre-session token lives at most 120 s (ARCH-067)")
        self._ring = keyring
        self._iss = issuer
        self._pre_ttl = presession_ttl_s
        self._session_ttl = session_ttl_s
        self._grace = grace_s

    @classmethod
    def from_settings(
        cls, signing: AssessKeyring, settings: VektraAssessSettings
    ) -> AssessTokens:
        return cls(
            keyring_from(signing),
            issuer=settings.token_issuer,
            presession_ttl_s=settings.presession_ttl_s,
            session_ttl_s=settings.session_token_ttl_s,
            grace_s=settings.reply_grace_s,
        )

    def _mint(
        self, claims: dict[str, object], ttl_s: int, not_after: float | None = None
    ) -> Minted:
        """Encode with ``ttl_s``; with ``not_after`` (epoch seconds) the ENCODED ``exp`` is
        checked against it and the token re-encoded one second shorter until it fits: the TTL is
        computed on one clock reading and ``encode_jwt`` reads its own, so truncation of both
        could otherwise push ``exp`` past the bound."""
        jti = new_jti()
        while True:
            if ttl_s <= 0:
                raise TokenError("past_deadline")
            token = encode_jwt(
                {**claims, "jti": jti, "loginas": False},
                key=self._ring.signing_key,
                audience=AUDIENCE,
                issuer=self._iss,
                ttl=timedelta(seconds=ttl_s),
            )
            exp = int(jwt.decode(token, options={"verify_signature": False})["exp"])
            if not_after is None or exp <= not_after:
                return Minted(token, jti, datetime.fromtimestamp(exp, tz=UTC), ttl_s)
            ttl_s -= 1

    def mint_presession(self, *, sub: int, site: UUID, cmid: int) -> Minted:
        return self._mint(
            {"sub": str(sub), "site": str(site), "cmid": cmid, "stage": "pre"},
            self._pre_ttl,
        )

    def mint_session(
        self,
        *,
        sub: int,
        site: UUID,
        cmid: int,
        sid: UUID,
        seq: int,
        epoch: int,
        deadline: datetime | None,
    ) -> Minted:
        """A session-bound token. With a deadline, ``exp`` never passes deadline + grace (on the
        encoding clock); a session already past that has nothing left to authorize and gets no
        token (``TokenError``)."""
        ttl = self._session_ttl
        not_after: float | None = None
        if deadline is not None:
            not_after = deadline.timestamp() + self._grace
            left = int(not_after - datetime.now(UTC).timestamp())
            if left <= 0:
                raise TokenError("past_deadline")
            ttl = min(ttl, left)
        return self._mint(
            {
                "sub": str(sub),
                "site": str(site),
                "cmid": cmid,
                "sid": str(sid),
                "seq": seq,
                "epoch": epoch,
                "stage": "session",
            },
            ttl,
            not_after,
        )

    @overload
    def verify(self, token: str, *, stage: Literal["pre"]) -> PreClaims: ...

    @overload
    def verify(self, token: str, *, stage: Literal["session"]) -> SessionClaims: ...

    def verify(self, token: str, *, stage: Stage) -> PreClaims | SessionClaims:
        if not token or len(token) > 4096:
            raise TokenError("token_shape")
        try:
            claims = decode_jwt(
                token,
                keyring=self._ring,
                audience=AUDIENCE,
                issuer=self._iss,
                require=_REQUIRE[stage],
            )
        except jwt.InvalidTokenError as exc:
            raise TokenError(type(exc).__name__) from None
        if claims.get("stage") != stage:
            raise TokenError("stage")
        if claims.get("loginas") is not False:
            try:
                site_claim: UUID | None = _uuid(claims, "site")
            except TokenError:
                site_claim = None
            raise TokenError("loginas", site=site_claim)
        sub_raw = claims.get("sub")
        if not isinstance(sub_raw, str) or not (
            sub_raw.isascii() and sub_raw.isdigit()
        ):
            raise TokenError("claim_sub")
        jti = claims.get("jti")
        if not isinstance(jti, str) or not 8 <= len(jti) <= 64:
            raise TokenError("claim_jti")
        sub, site, cmid = int(sub_raw), _uuid(claims, "site"), _int(claims, "cmid")
        iat, exp = _int(claims, "iat"), _int(claims, "exp")
        if stage == "pre":
            if exp - iat > 120:
                raise TokenError("presession_ttl")
            return PreClaims(sub, site, cmid, jti, iat, exp)
        return SessionClaims(
            sub,
            site,
            cmid,
            _uuid(claims, "sid"),
            _int(claims, "seq"),
            _int(claims, "epoch"),
            jti,
            iat,
            exp,
        )
