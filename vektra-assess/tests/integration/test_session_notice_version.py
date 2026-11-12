"""Plugin request P3: the mint echoes the privacy-notice version the student saw.

``POST /tokens`` may carry ``privacy_notice_version`` (the ``pre_session.privacy.notice_version``
of ``GET /state``). Absent: the mint is exactly as before. Present: it must equal the site's
current notice version (the deployment stand-in for a site with no settings, as ``/state`` shows
it), or the mint is refused 409 ``ERR-ASSESS-029`` ``privacy_notice_changed`` and writes no token
row. The version travels in the pre-session token, and Start refuses with the same error when the
notice changed between the mint and the Start (no session, audited).
"""

from __future__ import annotations

import uuid

import jwt
import pytest
from support.world import configure_activity, err, world

pytestmark = pytest.mark.integration

CMID = 501
SITE_VERSION = "2026-11"


async def n(pg, sql: str, **params) -> int:  # type: ignore[no-untyped-def]
    return int((await pg.run("super", sql, params))[0][0])


async def _clean(pg) -> None:  # type: ignore[no-untyped-def]
    for table in ("assess_jobs", "assess_start_attempts", "assess_token_jti"):
        await pg.run("super", f"DELETE FROM {table}")
    await pg.run("super", "DELETE FROM assess_sessions WHERE state = 'STARTING'")


async def _configure_privacy(pg, site: uuid.UUID, version: str) -> None:  # type: ignore[no-untyped-def]
    await pg.run(
        "super",
        "INSERT INTO assess_privacy_config (site_id, retention_days, legal_basis_label, "
        "legal_basis_text, notice_url, notice_text, notice_version, updated_by) VALUES "
        "(:s, 30, 'Art. 6(1)(e) GDPR', 'Valutazione.', 'https://uni.example/privacy', "
        "'Informativa.', :v, 'test') ON CONFLICT (site_id) DO UPDATE SET "
        "notice_version = EXCLUDED.notice_version",
        {"s": site, "v": version},
    )


async def start(w, token: str):  # type: ignore[no-untyped-def]
    return await w.client.post(
        "/api/v1/assess/sessions", headers={"Authorization": f"Bearer {token}"}
    )


def _claims(r) -> dict:  # type: ignore[no-untyped-def]
    return jwt.decode(r.json()["token"], options={"verify_signature": False})


async def test_mint_without_the_field_is_unchanged(pg) -> None:  # type: ignore[no-untyped-def]
    await _clean(pg)
    async with world(pg) as w:
        r = await w.mint(sub=101, cmid=CMID)
        assert r.status_code == 201, r.text
        assert "notice_version" not in _claims(r)
        # a null is the same as absent
        r = await w.mint(sub=101, cmid=CMID, privacy_notice_version=None)
        assert r.status_code == 201, r.text
        assert "notice_version" not in _claims(r)
        assert await n(pg, "SELECT count(*) FROM assess_token_jti") == 2


async def test_mint_with_the_current_version_carries_it_in_the_token(pg) -> None:  # type: ignore[no-untyped-def]
    await _clean(pg)
    async with world(pg) as w:
        # no site settings (development): the deployment stand-in /state shows, "1"
        r = await w.mint(sub=101, cmid=CMID, privacy_notice_version="1")
        assert r.status_code == 201, r.text
        assert _claims(r)["notice_version"] == "1"
        await _configure_privacy(pg, w.site, SITE_VERSION)
        r = await w.mint(sub=101, cmid=CMID, privacy_notice_version=SITE_VERSION)
        assert r.status_code == 201, r.text
        assert _claims(r)["notice_version"] == SITE_VERSION
        assert await n(pg, "SELECT count(*) FROM assess_token_jti") == 2


@pytest.mark.parametrize("configured", [False, True])
async def test_mint_with_a_stale_version_is_refused_and_writes_no_row(
    pg, configured: bool
) -> None:  # type: ignore[no-untyped-def]
    await _clean(pg)
    async with world(pg) as w:
        if configured:
            await _configure_privacy(pg, w.site, SITE_VERSION)
        r = await w.mint(sub=101, cmid=CMID, privacy_notice_version="2025-01")
        assert r.status_code == 409, r.text
        e = err(r)
        assert e["code"] == "ERR-ASSESS-029"
        assert e["details"]["reason"] == "privacy_notice_changed"
        assert "token" not in r.json()
        assert await n(pg, "SELECT count(*) FROM assess_token_jti") == 0
        assert (
            await n(
                pg,
                "SELECT count(*) FROM assess_audit_events WHERE event='assess_token_mint' "
                "AND site_id = :s",
                s=w.site,
            )
            == 0
        )


@pytest.mark.parametrize(
    ("value", "code"),
    [
        ("x" * 41, "too_long"),
        ("", "empty"),
        ("   ", "empty"),
        ("v\x01", "control_character"),
        (7, "not_a_string"),
    ],
)
async def test_an_invalid_value_is_a_400_naming_the_field(pg, value, code) -> None:  # type: ignore[no-untyped-def]
    await _clean(pg)
    async with world(pg) as w:
        r = await w.mint(sub=101, cmid=CMID, privacy_notice_version=value)
        assert r.status_code == 400, r.text
        e = err(r)
        assert e["code"] == "ERR-ASSESS-014"
        assert e["details"]["reason"] == f"{code}:privacy_notice_version"
        assert await n(pg, "SELECT count(*) FROM assess_token_jti") == 0


async def test_start_with_the_matching_version_records_both(pg) -> None:  # type: ignore[no-untyped-def]
    await _clean(pg)
    async with world(pg) as w:
        await configure_activity(pg, w.site, CMID)
        await _configure_privacy(pg, w.site, SITE_VERSION)
        r = await w.mint(sub=111, cmid=CMID, privacy_notice_version=SITE_VERSION)
        assert r.status_code == 201, r.text
        s = await start(w, r.json()["token"])
        assert s.status_code == 202, s.text
        sid = uuid.UUID(s.json()["session_id"])
        assert (
            await n(
                pg,
                "SELECT count(*) FROM assess_audit_events WHERE session_id = :s "
                "AND event = 'assess_start' AND data->>'notice_version' = :v "
                "AND data->>'presented_notice_version' = :v",
                s=sid,
                v=SITE_VERSION,
            )
            == 1
        )


async def test_start_without_a_presented_version_audits_none(pg) -> None:  # type: ignore[no-untyped-def]
    await _clean(pg)
    async with world(pg) as w:
        await configure_activity(pg, w.site, CMID)
        s = await start(w, await w.presession(sub=112, cmid=CMID))
        assert s.status_code == 202, s.text
        data = (
            await pg.run(
                "super",
                "SELECT data FROM assess_audit_events WHERE session_id = :s "
                "AND event = 'assess_start'",
                {"s": uuid.UUID(s.json()["session_id"])},
            )
        )[0][0]
        assert data["notice_version"] == "1"
        assert "presented_notice_version" not in data


async def test_notice_changed_between_mint_and_start_refuses_the_start(pg) -> None:  # type: ignore[no-untyped-def]
    await _clean(pg)
    async with world(pg) as w:
        await configure_activity(pg, w.site, CMID)
        await _configure_privacy(pg, w.site, SITE_VERSION)
        r = await w.mint(sub=113, cmid=CMID, privacy_notice_version=SITE_VERSION)
        assert r.status_code == 201, r.text
        await _configure_privacy(pg, w.site, "2026-12")
        s = await start(w, r.json()["token"])
        assert s.status_code == 409, s.text
        e = err(s)
        assert e["code"] == "ERR-ASSESS-029"
        assert e["details"]["reason"] == "privacy_notice_changed"
        assert (
            await n(
                pg, "SELECT count(*) FROM assess_sessions WHERE student_userid = 113"
            )
            == 0
        )
        assert await n(pg, "SELECT count(*) FROM assess_jobs") == 0
        assert await n(pg, "SELECT count(*) FROM assess_start_attempts") == 0
        # nothing consumed: the transaction rolled back
        assert (
            await n(
                pg,
                "SELECT count(*) FROM assess_token_jti WHERE consumed_at IS NOT NULL",
            )
            == 0
        )
        assert (
            await n(
                pg,
                "SELECT count(*) FROM assess_audit_events WHERE event = "
                "'assess_start_refused' AND reason_code = 'privacy_notice_changed' "
                "AND site_id = :s AND (data->>'cmid')::int = :c",
                s=w.site,
                c=CMID,
            )
            == 1
        )


async def test_the_state_mint_path_is_untouched(pg) -> None:  # type: ignore[no-untyped-def]
    await _clean(pg)
    async with world(pg) as w:
        await _configure_privacy(pg, w.site, SITE_VERSION)
        token = await w.presession(sub=114, cmid=CMID)
        r = await w.client.get(
            "/api/v1/assess/state", headers={"Authorization": f"Bearer {token}"}
        )
        assert r.status_code == 200, r.text
