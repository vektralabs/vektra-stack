"""M1.2: discussion token mint / verify matrix (ARCH-067)."""

from __future__ import annotations

import os
import time
import uuid
from datetime import UTC, datetime, timedelta

import jwt
import pytest
from pydantic import ValidationError

from vektra_assess.config import VektraAssessSettings
from vektra_assess.keys import AssessKeyring
from vektra_assess.tokens import (
    AUDIENCE,
    AssessTokens,
    PreClaims,
    SessionClaims,
    TokenError,
)

SITE = uuid.uuid4()
SID = uuid.uuid4()


def ring(*kids: str, active: str | None = None) -> AssessKeyring:
    return AssessKeyring(
        "assess_token_signing", active or kids[-1], {k: os.urandom(32) for k in kids}
    )


def tokens(r: AssessKeyring | None = None, **kw: object) -> AssessTokens:
    return AssessTokens.from_settings(
        r or ring("k1"), VektraAssessSettings.model_validate(kw)
    )


def test_presession_roundtrip_and_claims() -> None:
    t = tokens()
    m = t.mint_presession(sub=42, site=SITE, cmid=7)
    c = t.verify(m.token, stage="pre")
    assert isinstance(c, PreClaims)
    assert (c.sub, c.site, c.cmid, c.jti) == (42, SITE, 7, m.jti)
    assert c.exp - c.iat <= 120
    raw = jwt.decode(m.token, options={"verify_signature": False})
    assert raw["aud"] == AUDIENCE and raw["loginas"] is False and raw["stage"] == "pre"
    assert jwt.get_unverified_header(m.token)["kid"] == "k1"


def test_session_roundtrip_and_deadline_cap() -> None:
    t = tokens(**{"VEKTRA_ASSESS_SESSION_TOKEN_TTL_S": 900})
    now = datetime.now(UTC)
    m = t.mint_session(
        sub=42,
        site=SITE,
        cmid=7,
        sid=SID,
        seq=1,
        epoch=3,
        deadline=now + timedelta(seconds=30),
    )
    c = t.verify(m.token, stage="session")
    assert isinstance(c, SessionClaims)
    assert (c.sid, c.seq, c.epoch) == (SID, 1, 3)
    assert 30 + 60 - 1 <= m.ttl_s <= 30 + 60  # deadline + grace, below the 900 s TTL
    with pytest.raises(TokenError):
        t.mint_session(
            sub=42,
            site=SITE,
            cmid=7,
            sid=SID,
            seq=1,
            epoch=3,
            deadline=now - timedelta(seconds=61),
        )


def test_stages_do_not_cross() -> None:
    t = tokens()
    pre = t.mint_presession(sub=1, site=SITE, cmid=2).token
    ses = t.mint_session(
        sub=1, site=SITE, cmid=2, sid=SID, seq=1, epoch=1, deadline=None
    ).token
    with pytest.raises(TokenError):
        t.verify(pre, stage="session")
    with pytest.raises(TokenError):
        t.verify(ses, stage="pre")


def test_rotation_previous_kid_verifies_unknown_kid_does_not() -> None:
    old = ring("k1")
    minted = tokens(old).mint_presession(sub=1, site=SITE, cmid=2).token
    rotated = AssessKeyring(
        "assess_token_signing",
        "k2",
        {"k1": old.keys["k1"], "k2": os.urandom(32)},
    )
    assert tokens(rotated).verify(minted, stage="pre").sub == 1
    new_token = tokens(rotated).mint_presession(sub=1, site=SITE, cmid=2).token
    assert jwt.get_unverified_header(new_token)["kid"] == "k2"
    with pytest.raises(TokenError):
        tokens(ring("k3")).verify(minted, stage="pre")


def _forge(claims: dict[str, object], key: bytes, kid: str = "k1", **hdr: str) -> str:
    return jwt.encode(claims, key, algorithm="HS256", headers={"kid": kid, **hdr})


def _base(**over: object) -> dict[str, object]:
    now = int(time.time())
    claims: dict[str, object] = {
        "sub": "5",
        "site": str(SITE),
        "cmid": 2,
        "jti": "abcdefghijkl",
        "stage": "pre",
        "loginas": False,
        "aud": AUDIENCE,
        "iss": "vektra-assess",
        "iat": now,
        "exp": now + 60,
    }
    claims.update(over)
    return {k: v for k, v in claims.items() if v is not None}


@pytest.mark.parametrize(
    "over",
    [
        {"loginas": None},  # absent
        {"loginas": True},
        {"loginas": 0},
        {"aud": "learn"},
        {"iss": "someone-else"},
        {"exp": int(time.time()) - 5},
        {"exp": int(time.time()) + 600},  # pre-session longer than 120 s
        {"sub": "not-a-number"},
        {"site": "not-a-uuid"},
        {"cmid": "2"},
        {"jti": None},
        {"stage": None},
    ],
)
def test_forged_or_malformed_claims_are_refused(over: dict[str, object]) -> None:
    r = ring("k1")
    tok = _forge(_base(**over), r.keys["k1"])
    with pytest.raises(TokenError):
        tokens(r).verify(tok, stage="pre")


def test_a_well_formed_forgery_with_the_right_key_is_the_only_pass() -> None:
    r = ring("k1")
    assert tokens(r).verify(_forge(_base(), r.keys["k1"]), stage="pre").sub == 5


def test_wrong_key_none_alg_and_garbage_are_refused() -> None:
    r = ring("k1")
    with pytest.raises(TokenError):
        tokens(r).verify(_forge(_base(), os.urandom(32)), stage="pre")
    unsigned = jwt.encode(_base(), None, algorithm="none", headers={"kid": "k1"})
    with pytest.raises(TokenError):
        tokens(r).verify(unsigned, stage="pre")
    for junk in ("", "x", "a.b.c", "x" * 5000):
        with pytest.raises(TokenError):
            tokens(r).verify(junk, stage="pre")


def test_presession_ttl_over_120_refuses_startup() -> None:
    with pytest.raises(ValidationError):
        VektraAssessSettings.model_validate({"VEKTRA_ASSESS_PRESESSION_TTL_S": 121})
    assert (
        VektraAssessSettings.model_validate(
            {"VEKTRA_ASSESS_PRESESSION_TTL_S": 120}
        ).presession_ttl_s
        == 120
    )


def test_session_exp_never_passes_deadline_plus_grace() -> None:
    """CodeRabbit 4493109426: the expiry is bounded on the clock that encodes it, for any
    sub-second deadline, so exp <= deadline + grace always holds."""
    t = tokens(**{"VEKTRA_ASSESS_SESSION_TOKEN_TTL_S": 900})
    for frac in (0.0, 0.1, 0.5, 0.9, 0.999):
        deadline = datetime.now(UTC) + timedelta(seconds=20 + frac)
        m = t.mint_session(
            sub=1, site=SITE, cmid=2, sid=SID, seq=1, epoch=1, deadline=deadline
        )
        exp = jwt.decode(m.token, options={"verify_signature": False})["exp"]
        assert exp <= deadline.timestamp() + 60, frac
        assert m.expires_at.timestamp() <= deadline.timestamp() + 60


def test_an_encoder_clock_ahead_of_the_minting_clock_cannot_stretch_exp(
    monkeypatch,
) -> None:  # type: ignore[no-untyped-def]
    """The encoder reads its own clock a moment later than the TTL was computed: truncation of
    both must still keep exp <= deadline + grace (frozen clocks, deterministic)."""
    import vektra_shared.tokens as shared_tokens
    from vektra_assess import tokens as mod

    def clock(epoch: float) -> type[datetime]:
        class Frozen(datetime):
            @classmethod
            def now(cls, tz=None):  # type: ignore[no-untyped-def, override]
                return datetime.fromtimestamp(epoch, tz=tz)

        return Frozen

    monkeypatch.setattr(mod, "datetime", clock(1_000_000_000.90))
    monkeypatch.setattr(shared_tokens, "datetime", clock(1_000_000_001.05))
    deadline = datetime.fromtimestamp(1_000_000_010.95, tz=UTC)
    m = tokens(**{"VEKTRA_ASSESS_SESSION_TOKEN_TTL_S": 900}).mint_session(
        sub=1, site=SITE, cmid=2, sid=SID, seq=1, epoch=1, deadline=deadline
    )
    exp = jwt.decode(m.token, options={"verify_signature": False, "verify_exp": False})[
        "exp"
    ]
    assert exp <= deadline.timestamp() + 60
    assert m.expires_at.timestamp() == exp
