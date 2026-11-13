"""M1.3 / M2.5: ``POST /api/v1/assess/tokens`` on a real database, as the API role."""

from __future__ import annotations

import jwt
import pytest
from support.world import ABSENT, err, world

from vektra_assess.tokens import AUDIENCE

pytestmark = pytest.mark.integration


async def _count(pg, sql: str, **params) -> int:  # type: ignore[no-untyped-def]
    return int((await pg.run("super", sql, params))[0][0])


async def test_mint_writes_one_jti_row_and_one_audit_row(pg) -> None:  # type: ignore[no-untyped-def]
    async with world(pg) as w:
        r = await w.mint(sub=101, cmid=501)
        assert r.status_code == 201, r.text
        assert r.headers["cache-control"] == "no-store"
        body = r.json()
        assert set(body) == {"token", "expires_in"}
        assert 0 < body["expires_in"] <= 120
        claims = jwt.decode(body["token"], options={"verify_signature": False})
        assert claims["aud"] == AUDIENCE and claims["stage"] == "pre"
        assert claims["site"] == str(w.site) and claims["sub"] == "101"
        row = (
            await pg.run(
                "super",
                "SELECT site_id, student_userid, cmid, courseid_hint, mint_key_id, "
                "consumed_at, expires_at > now() FROM assess_token_jti WHERE jti = :j",
                {"j": claims["jti"]},
            )
        )[0]
        assert tuple(row) == (w.site, 101, 501, 7, w.keys["mint"].key_id, None, True)
        assert (
            await _count(
                pg,
                "SELECT count(*) FROM assess_audit_events WHERE event='assess_token_mint' "
                "AND site_id=:s AND (data->>'cmid')::int = 501",
                s=w.site,
            )
            == 1
        )


async def test_the_site_comes_from_the_key_binding(pg) -> None:  # type: ignore[no-untyped-def]
    async with world(pg) as w:
        r = await w.mint(key="mint_other")
        assert r.status_code == 201
        claims = jwt.decode(r.json()["token"], options={"verify_signature": False})
        assert claims["site"] == str(w.other_site)


@pytest.mark.parametrize(
    ("key", "status"),
    [("admin", 403), ("teacher", 403), ("unbound_mint", 403), ("nobody", 401)],
)
async def test_only_a_bound_mint_key_mints(pg, key: str, status: int) -> None:  # type: ignore[no-untyped-def]
    async with world(pg) as w:
        before = await _count(pg, "SELECT count(*) FROM assess_token_jti")
        r = await w.mint(key=key)
        assert r.status_code == status, r.text
        assert await _count(pg, "SELECT count(*) FROM assess_token_jti") == before
        if key == "unbound_mint":
            assert err(r)["code"] == "ERR-ASSESS-007"


@pytest.mark.parametrize(
    "over",
    [
        {"loginas": True},
        {"loginas": ABSENT},
        {"realuser_id": 5},
        {"realuser_id": ABSENT},
        {"loginas": 0},
        {"loginas": "false"},
        {"loginas": None},
    ],
)
async def test_loginas_is_refused_audited_and_writes_no_token(pg, over) -> None:  # type: ignore[no-untyped-def]
    async with world(pg) as w:
        before_jti = await _count(pg, "SELECT count(*) FROM assess_token_jti")
        before_sessions = await _count(pg, "SELECT count(*) FROM assess_sessions")
        r = await w.mint(sub=202, cmid=777, **over)
        assert r.status_code == 403, r.text
        assert err(r)["code"] == "ERR-ASSESS-006"
        assert "token" not in r.text
        assert await _count(pg, "SELECT count(*) FROM assess_token_jti") == before_jti
        assert (
            await _count(pg, "SELECT count(*) FROM assess_sessions") == before_sessions
        )
        assert (
            await _count(
                pg,
                "SELECT count(*) FROM assess_audit_events WHERE event = "
                "'assess_loginas_rejected' AND site_id = :s",
                s=w.site,
            )
            == 1
        )


@pytest.mark.parametrize(
    ("version", "site_min", "ok"),
    [
        ("1.0.0", None, True),
        ("0.0.9", None, False),
        ("0.1.9", None, False),  # below VEKTRA_ASSESS_MIN_PLUGIN_VERSION (0.2.0)
        ("0.2.0", None, True),  # the default minimum itself
        ("0.10.0", None, True),  # numeric, not lexical: 0.10.0 > 0.2.0
        ("1.0.0", "1.2.0", False),  # below the site's own minimum
        ("1.2.0-rc1", "1.2.0", True),
        ("banana", None, False),
    ],
)
async def test_plugin_version_minimum(pg, version, site_min, ok) -> None:  # type: ignore[no-untyped-def]
    async with world(pg) as w:
        await pg.run(
            "super",
            "UPDATE assess_sites SET min_plugin_version = :v WHERE id = :s",
            {"v": site_min, "s": w.site},
        )
        r = await w.mint(plugin_version=version)
        assert (r.status_code == 201) is ok, r.text
        if not ok:
            assert err(r)["code"] == "ERR-ASSESS-016"


async def test_bad_body_is_400_and_unknown_fields_are_refused(pg) -> None:  # type: ignore[no-untyped-def]
    async with world(pg) as w:
        r = await w.mint(attemptid=55)
        assert r.status_code == 400 and err(r)["code"] == "ERR-ASSESS-014"
        r = await w.mint(sub="x")
        assert r.status_code == 400


async def test_cleanup_deletes_only_rows_a_day_past_expiry(pg) -> None:  # type: ignore[no-untyped-def]
    from contextlib import asynccontextmanager

    import asyncpg
    from sqlalchemy.engine import make_url

    from vektra_assess.worker.cleanup import cleanup_expired_jtis

    async with world(pg) as w:
        await pg.run("super", "DELETE FROM assess_token_jti")
        for jti, age in (("old-jti-000001", "2 days"), ("new-jti-000002", "1 hour")):
            await pg.run(
                "super",
                "INSERT INTO assess_token_jti (jti, site_id, student_userid, cmid, "
                "mint_key_id, expires_at) VALUES (:j, :s, 1, 1, :k, now() - "
                f"interval '{age}')",
                {"j": jti, "s": w.site, "k": w.keys["mint"].key_id},
            )
        dsn = (
            make_url(pg.url("worker"))
            .set(drivername="postgresql")
            .render_as_string(hide_password=False)
        )

        @asynccontextmanager
        async def acquire():  # type: ignore[no-untyped-def]
            c = await asyncpg.connect(dsn)
            try:
                yield c
            finally:
                await c.close()

        assert await cleanup_expired_jtis(acquire) == 1
        left = await pg.run("super", "SELECT jti FROM assess_token_jti")
        assert [r[0] for r in left] == ["new-jti-000002"]
