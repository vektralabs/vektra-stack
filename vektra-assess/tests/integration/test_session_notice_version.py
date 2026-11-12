"""Plugin request P3: the Start mint echoes the privacy-notice version the student saw.

``POST /tokens`` may carry ``privacy_notice_version`` (the ``pre_session.privacy.notice_version``
of ``GET /state``). Absent: the mint is exactly as before. Present: its FORMAT is checked (the
rule of the stored ``notice_version``, after the login-as assertion) and it travels in the
pre-session token; the mint never compares it, so the same token still serves ``/state`` and a
resume whatever it carries. Only Start compares it with the site's current version: a mismatch is
409 ``ERR-ASSESS-029`` ``privacy_notice_changed``, no session, nothing consumed, audited with
both versions. The operator cannot change what the student is shown without a new version.
"""

from __future__ import annotations

import uuid

import jwt
import pytest
from sqlalchemy.ext.asyncio import create_async_engine
from support.sessions import seed_session
from support.world import auth, configure_activity, err, world

pytestmark = pytest.mark.integration

CMID = 501
SITE_VERSION = "2026-11"
SETTINGS = {
    "retention_days": 30,
    "legal_basis_label": "Art. 6(1)(e) GDPR",
    "legal_basis_text": "Valutazione.",
    "notice_url": "https://uni.example/privacy",
    "notice_text": "Informativa.",
    "notice_version": SITE_VERSION,
}


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


@pytest.fixture
async def worker(pg):  # type: ignore[no-untyped-def]
    from integration.test_session_preflight import _reset

    await _reset(pg)
    engine = create_async_engine(pg.url("worker"))
    yield engine
    await engine.dispose()


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


async def test_mint_carries_any_well_formed_version_without_comparing(pg) -> None:  # type: ignore[no-untyped-def]
    """Review finding 2: the mint validates the format only; a version that is not the site's
    current one still gets its token (Start is where the comparison happens)."""
    await _clean(pg)
    async with world(pg) as w:
        await _configure_privacy(pg, w.site, SITE_VERSION)
        for version in (SITE_VERSION, "2025-01"):
            r = await w.mint(sub=101, cmid=CMID, privacy_notice_version=version)
            assert r.status_code == 201, r.text
            assert _claims(r)["notice_version"] == version
        assert await n(pg, "SELECT count(*) FROM assess_token_jti") == 2


@pytest.mark.parametrize(
    ("value", "code"),
    [
        ("x" * 41, "too_long"),
        ("", "empty"),
        ("   ", "empty"),
        ("v\x01", "control_character"),
        ("v\x7f", "control_character"),
        ("v\x85", "control_character"),
        (7, "not_a_string"),
        (["v1"], "not_a_string"),
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


async def test_a_login_as_mint_is_audited_first_whatever_the_version(pg) -> None:  # type: ignore[no-untyped-def]
    """Review finding 4: a malformed version never hides a login-as attempt behind a 400."""
    await _clean(pg)
    async with world(pg) as w:
        r = await w.mint(
            sub=101, cmid=CMID, loginas=True, privacy_notice_version="x" * 99
        )
        assert r.status_code == 403, r.text
        assert err(r)["code"] == "ERR-ASSESS-006"
        assert (
            await n(
                pg,
                "SELECT count(*) FROM assess_audit_events WHERE event = "
                "'assess_loginas_rejected' AND site_id = :s",
                s=w.site,
            )
            == 1
        )
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


async def test_start_with_the_development_stand_in(pg) -> None:  # type: ignore[no-untyped-def]
    """No site settings (development): the version /state shows is the deployment value."""
    await _clean(pg)
    async with world(pg) as w:
        await configure_activity(pg, w.site, CMID)
        r = await w.mint(sub=115, cmid=CMID, privacy_notice_version="1")
        s = await start(w, r.json()["token"])
        assert s.status_code == 202, s.text


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


@pytest.mark.parametrize("change_between", [True, False])
async def test_a_stale_version_refuses_the_start(pg, change_between: bool) -> None:  # type: ignore[no-untyped-def]
    """The notice changed between the mint and the Start, or the plugin echoed a version that
    was already stale at mint: Start refuses, nothing written, audited with both versions."""
    await _clean(pg)
    async with world(pg) as w:
        await configure_activity(pg, w.site, CMID)
        await _configure_privacy(pg, w.site, SITE_VERSION)
        presented = SITE_VERSION if change_between else "2025-01"
        r = await w.mint(sub=113, cmid=CMID, privacy_notice_version=presented)
        assert r.status_code == 201, r.text
        current = "2026-12" if change_between else SITE_VERSION
        if change_between:
            await _configure_privacy(pg, w.site, current)
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
        rows = await pg.run(
            "super",
            "SELECT data FROM assess_audit_events WHERE event = 'assess_start_refused' "
            "AND reason_code = 'privacy_notice_changed' AND site_id = :s",
            {"s": w.site},
        )
        assert len(rows) == 1
        assert rows[0][0] == {
            "cmid": CMID,
            "presented_notice_version": presented,
            "current_notice_version": current,
        }


async def test_state_and_resume_ignore_the_claim(pg, worker) -> None:  # type: ignore[no-untyped-def]
    """Review finding 2: a token carrying a stale version still serves /state and a resume."""
    async with world(pg) as w:
        await _configure_privacy(pg, w.site, SITE_VERSION)
        s = await seed_session(w, worker, "ACTIVE")
        for _ in range(2):
            r = await w.mint(sub=s.sub, cmid=s.cmid, privacy_notice_version="stale-1")
            assert r.status_code == 201, r.text
            token = r.json()["token"]
            if _ == 0:
                got = await w.client.get(
                    "/api/v1/assess/state", headers={"Authorization": f"Bearer {token}"}
                )
            else:
                got = await w.client.get(
                    f"/api/v1/assess/sessions/{s.session_id}",
                    headers={"Authorization": f"Bearer {token}"},
                )
            assert got.status_code == 200, got.text


async def test_the_state_mint_path_is_untouched(pg) -> None:  # type: ignore[no-untyped-def]
    await _clean(pg)
    async with world(pg) as w:
        await _configure_privacy(pg, w.site, SITE_VERSION)
        token = await w.presession(sub=114, cmid=CMID)
        r = await w.client.get(
            "/api/v1/assess/state", headers={"Authorization": f"Bearer {token}"}
        )
        assert r.status_code == 200, r.text


@pytest.mark.parametrize(
    "field",
    [
        "notice_text",
        "notice_url",
        "retention_days",
        "legal_basis_label",
        "legal_basis_text",
    ],
)
async def test_shown_content_cannot_change_without_a_new_version(
    pg, field: str
) -> None:  # type: ignore[no-untyped-def]
    """Review finding 1: a change the student would see needs a new ``notice_version``, or the
    echo could not tell the two notices apart."""
    async with world(pg) as w:
        url = f"/api/v1/assess/sites/{w.site}/privacy"
        assert (
            await w.client.put(url, json=SETTINGS, headers=auth("admin"))
        ).status_code == 200
        changed = {
            **SETTINGS,
            field: 31
            if field == "retention_days"
            else (
                "https://uni.example/privacy/v2"
                if field == "notice_url"
                else str(SETTINGS[field]) + " v2"
            ),
        }
        r = await w.client.put(url, json=changed, headers=auth("admin"))
        assert r.status_code == 400, r.text
        e = err(r)
        assert e["code"] == "ERR-ASSESS-028"
        assert e["details"]["fields"] == [
            {"field": "notice_version", "code": "must_change"}
        ]
        assert (
            await n(
                pg,
                "SELECT count(*) FROM assess_privacy_config WHERE site_id = :s "
                "AND retention_days = 30 AND notice_text = 'Informativa.' "
                "AND notice_url = 'https://uni.example/privacy'",
                s=w.site,
            )
            == 1
        )
        # the same change with a new version is stored; a re-save of the same row is fine
        r = await w.client.put(
            url, json={**changed, "notice_version": "2026-12"}, headers=auth("admin")
        )
        assert r.status_code == 200, r.text
        r = await w.client.put(
            url, json={**changed, "notice_version": "2026-12"}, headers=auth("admin")
        )
        assert r.status_code == 200, r.text


async def test_a_new_version_alone_is_accepted(pg) -> None:  # type: ignore[no-untyped-def]
    async with world(pg) as w:
        url = f"/api/v1/assess/sites/{w.site}/privacy"
        assert (
            await w.client.put(url, json=SETTINGS, headers=auth("admin"))
        ).status_code == 200
        r = await w.client.put(
            url, json={**SETTINGS, "notice_version": "2026-12"}, headers=auth("admin")
        )
        assert r.status_code == 200, r.text
