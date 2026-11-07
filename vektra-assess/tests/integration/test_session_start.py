"""M1.4: ``POST /api/v1/assess/sessions`` (Start) on a real database, as the API role.

Every assertion counts rows: sessions, consumed jtis, jobs, attempts and audit events.
"""

from __future__ import annotations

import asyncio
import uuid

import jwt
import pytest
from support.world import beat, configure_activity, err, world

pytestmark = pytest.mark.integration

CMID = 501


async def n(pg, sql: str, **params) -> int:  # type: ignore[no-untyped-def]
    return int((await pg.run("super", sql, params))[0][0])


async def _clean(pg) -> None:  # type: ignore[no-untyped-def]
    for table in ("assess_jobs", "assess_start_attempts", "assess_token_jti"):
        await pg.run("super", f"DELETE FROM {table}")
    await pg.run("super", "DELETE FROM assess_sessions WHERE state = 'STARTING'")


async def start(w, token: str):  # type: ignore[no-untyped-def]
    return await w.client.post(
        "/api/v1/assess/sessions", headers={"Authorization": f"Bearer {token}"}
    )


async def test_start_writes_starting_row_attempt_job_and_audit(pg) -> None:  # type: ignore[no-untyped-def]
    await _clean(pg)
    async with world(pg) as w:
        await configure_activity(pg, w.site, CMID)
        token = await w.presession(sub=111, cmid=CMID)
        r = await start(w, token)
        assert r.status_code == 202, r.text
        body = r.json()
        sid = uuid.UUID(body["session_id"])
        assert body["state"] == "STARTING" and body["session_no"] == 1
        claims = jwt.decode(body["token"], options={"verify_signature": False})
        assert claims["stage"] == "session" and claims["sid"] == str(sid)
        assert (claims["seq"], claims["epoch"]) == (1, 1)
        row = (
            await pg.run(
                "super",
                "SELECT state, student_userid, cmid, courseid_hint, notice_version, "
                "starting_expires_at > now() FROM assess_sessions WHERE id = :s",
                {"s": sid},
            )
        )[0]
        assert tuple(row) == ("STARTING", 111, CMID, 7, "1", True)
        assert (
            await n(
                pg,
                "SELECT count(*) FROM assess_jobs WHERE kind='preflight' "
                "AND args->>'session_id' = :s",
                s=str(sid),
            )
            == 1
        )
        assert (
            await n(
                pg,
                "SELECT count(*) FROM assess_start_attempts WHERE session_id=:s "
                "AND outcome='requested'",
                s=sid,
            )
            == 1
        )
        assert (
            await n(
                pg,
                "SELECT count(*) FROM assess_audit_events WHERE session_id=:s "
                "AND event='assess_start' AND data->>'notice_version' = '1'",
                s=sid,
            )
            == 1
        )
        assert (
            await n(
                pg,
                "SELECT count(*) FROM assess_token_jti WHERE consumed_at IS NOT NULL",
            )
            == 1
        )
        # the response carries no content at all: ids, state and the token only
        assert set(body) == {"session_id", "state", "token", "session_no"}
        # the jti is single use: a second Start with the same token is refused
        again = await start(w, token)
        assert again.status_code == 401 and err(again)["code"] == "ERR-ASSESS-005"


async def test_twenty_parallel_starts_leave_exactly_one_open_row(pg) -> None:  # type: ignore[no-untyped-def]
    await _clean(pg)
    async with world(pg) as w:
        await configure_activity(pg, w.site, CMID)
        tokens = [await w.presession(sub=222, cmid=CMID) for _ in range(20)]
        results = await asyncio.gather(*(start(w, t) for t in tokens))
        codes = sorted(r.status_code for r in results)
        assert codes.count(202) == 1, codes
        assert codes.count(409) == 19, codes
        for r in results:
            if r.status_code == 409:
                assert err(r)["details"]["reason"] == "session_already_open"
        assert (
            await n(
                pg,
                "SELECT count(*) FROM assess_sessions WHERE student_userid=222 "
                "AND state <> 'CLOSED'",
            )
            == 1
        )
        # refused Starts consumed nothing: only the winner's jti is consumed
        assert (
            await n(
                pg,
                "SELECT count(*) FROM assess_token_jti WHERE student_userid=222 "
                "AND consumed_at IS NOT NULL",
            )
            == 1
        )
        assert await n(pg, "SELECT count(*) FROM assess_jobs") == 1


async def test_token_for_another_activity_or_student_does_not_consume(pg) -> None:  # type: ignore[no-untyped-def]
    await _clean(pg)
    async with world(pg) as w:
        await configure_activity(pg, w.site, CMID)
        token = await w.presession(sub=333, cmid=CMID)
        # the jti row says (333, CMID); forge nothing, just delete the row: an unminted jti
        await pg.run("super", "DELETE FROM assess_token_jti")
        r = await start(w, token)
        assert r.status_code == 401
        assert (
            await n(pg, "SELECT count(*) FROM assess_sessions WHERE student_userid=333")
            == 0
        )


async def test_a_rotated_away_mint_key_cannot_start(pg) -> None:  # type: ignore[no-untyped-def]
    await _clean(pg)
    async with world(pg) as w:
        await configure_activity(pg, w.site, CMID)
        token = await w.presession(sub=334, cmid=CMID)
        await pg.run(
            "super",
            "DELETE FROM assess_site_keys WHERE api_key_id = :k",
            {"k": w.keys["mint"].key_id},
        )
        r = await start(w, token)
        assert r.status_code == 401
        assert (
            await n(
                pg,
                "SELECT count(*) FROM assess_token_jti WHERE consumed_at IS NOT NULL",
            )
            == 0
        )


@pytest.mark.parametrize(
    ("setup", "status", "reason"),
    [
        ("unconfigured", 409, "activity_not_configured"),
        ("disabled", 409, "activity_not_configured"),
        ("no_cap", 409, "activity_not_configured"),
        ("stale_worker", 503, None),
        ("capacity", 503, "capacity"),
    ],
)
async def test_advisory_refusals_write_nothing_but_their_audit(  # type: ignore[no-untyped-def]
    pg, setup, status, reason
) -> None:
    await _clean(pg)
    settings_over = {}
    if setup == "capacity":
        settings_over = {"VEKTRA_ASSESS_ADMISSION_K": 1}
    from support.world import default_settings

    async with world(pg, default_settings(**settings_over)) as w:
        cmid = CMID + 7
        if setup == "disabled":
            await configure_activity(pg, w.site, cmid, enabled=False)
        elif setup == "no_cap":
            await configure_activity(pg, w.site, cmid, cap_minutes=None)
        elif setup != "unconfigured":
            await configure_activity(pg, w.site, cmid)
        if setup == "stale_worker":
            await beat(pg, "5 minutes")
        if setup == "capacity":
            await pg.run(
                "super",
                "INSERT INTO assess_sessions (site_id, cmid, student_userid, state) "
                "VALUES (:s, 1, 9999, 'STARTING')",
                {"s": w.site},
            )
        token = await w.presession(sub=444, cmid=cmid)
        before_audit = await n(
            pg,
            "SELECT count(*) FROM assess_audit_events WHERE event='assess_start_refused'",
        )
        r = await start(w, token)
        assert r.status_code == status, r.text
        if reason is not None:
            assert err(r)["details"]["reason"] == reason
        assert (
            await n(pg, "SELECT count(*) FROM assess_sessions WHERE student_userid=444")
            == 0
        )
        assert await n(pg, "SELECT count(*) FROM assess_jobs") == 0
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
                "SELECT count(*) FROM assess_audit_events WHERE event='assess_start_refused'",
            )
            == before_audit + 1
        )
        await beat(pg)


async def test_a_closed_session_blocks_a_new_start_until_a_reset_grant(pg) -> None:  # type: ignore[no-untyped-def]
    from integration.conftest import make_session

    await _clean(pg)
    async with world(pg) as w:
        await configure_activity(pg, w.site, CMID)
        sid = await make_session(pg, "CLOSED")
        await pg.run(
            "super",
            "UPDATE assess_sessions SET site_id = :s, student_userid = 555, cmid = :c "
            "WHERE id = :i",
            {"s": w.site, "c": CMID, "i": sid},
        )
        r = await start(w, await w.presession(sub=555, cmid=CMID))
        assert r.status_code == 409
        assert err(r)["details"]["reason"] == "already_discussed"
        await pg.run(
            "super",
            "INSERT INTO assess_reset_grants (site_id, cmid, student_userid) VALUES (:s, :c, 555)",
            {"s": w.site, "c": CMID},
        )
        r = await start(w, await w.presession(sub=555, cmid=CMID))
        assert r.status_code == 202, r.text
        assert r.json()["session_no"] == 2


async def test_query_string_token_is_not_read(pg) -> None:  # type: ignore[no-untyped-def]
    await _clean(pg)
    async with world(pg) as w:
        await configure_activity(pg, w.site, CMID)
        token = await w.presession(sub=666, cmid=CMID)
        r = await w.client.post(f"/api/v1/assess/sessions?token={token}")
        assert r.status_code == 401
        r = await w.client.post(
            "/api/v1/assess/sessions",
            headers={"Cookie": f"token={token}; access_token={token}"},
        )
        assert r.status_code == 401
        assert (
            await n(pg, "SELECT count(*) FROM assess_sessions WHERE student_userid=666")
            == 0
        )


@pytest.mark.parametrize(
    ("status", "reason", "refused"),
    [
        ("not_graded", "not_released", True),
        ("not_open", "close_not_passed", True),
        ("unavailable", "unreadable", False),
        ("unavailable", None, False),
        ("not_open", "close_unreadable", False),
        ("unavailable", "some_future_code", False),
    ],
)
async def test_only_a_definitive_negative_render_refuses_a_start(  # type: ignore[no-untyped-def]
    pg, status: str, reason: str | None, refused: bool
) -> None:
    """Review fix 8: unreadable, NULL and unknown codes go on to the pre-flight; 'None' never
    reaches the error details."""
    await _clean(pg)
    async with world(pg) as w:
        cmid = CMID + 40
        await configure_activity(pg, w.site, cmid)
        await pg.run(
            "super",
            "INSERT INTO assess_render_state (site_id, student_userid, cmid, status, reason_code) "
            "VALUES (:s, 777, :c, :st, :r)",
            {"s": w.site, "c": cmid, "st": status, "r": reason},
        )
        r = await start(w, await w.presession(sub=777, cmid=cmid))
        if refused:
            assert r.status_code == 409 and err(r)["details"]["reason"] == reason
        else:
            assert r.status_code == 202, r.text
        assert "None" not in r.text
        await pg.run(
            "super", "DELETE FROM assess_render_state WHERE student_userid = 777"
        )


async def test_a_heartbeat_without_a_ready_state_refuses_the_start(pg) -> None:  # type: ignore[no-untyped-def]
    """CodeRabbit 4493109404: a fresh heartbeat row that does not say ``ready`` (missing state)
    refuses before the jti is consumed or a pre-flight queued."""
    await _clean(pg)
    async with world(pg) as w:
        await configure_activity(pg, w.site, CMID)
        await pg.run("super", "DELETE FROM assess_heartbeat")
        await pg.run(
            "worker",
            "INSERT INTO assess_heartbeat (component, beat_at, details) "
            "VALUES ('worker', now(), '{}'::jsonb)",
        )
        r = await start(w, await w.presession(sub=889, cmid=CMID))
        assert r.status_code == 503
        assert await n(pg, "SELECT count(*) FROM assess_jobs") == 0
        assert (
            await n(
                pg,
                "SELECT count(*) FROM assess_token_jti WHERE consumed_at IS NOT NULL",
            )
            == 0
        )
        await beat(pg)


async def test_a_correctly_signed_token_never_minted_is_rejected_and_audited(
    pg,
) -> None:  # type: ignore[no-untyped-def]
    """M5.5: signed by the right key with valid claims, but never minted through /tokens: the
    jti has no mint row from the site's current mint key. Rejected, audited, nothing written."""
    await _clean(pg)
    async with world(pg) as w:
        await configure_activity(pg, w.site, CMID)
        from vektra_assess.api.deps import tokens_of

        class _Req:
            app = w.app

        forged = tokens_of(_Req()).mint_presession(sub=335, site=w.site, cmid=CMID)  # type: ignore[arg-type]
        audited = (
            "SELECT count(*) FROM assess_audit_events WHERE event = 'assess_start_refused' "
            "AND reason_code = 'mint_correlation'"
        )
        before = await n(pg, audited)
        r = await start(w, forged.token)
        assert r.status_code == 401 and err(r)["code"] == "ERR-ASSESS-005"
        assert (
            await n(
                pg, "SELECT count(*) FROM assess_sessions WHERE student_userid = 335"
            )
            == 0
        )
        assert await n(pg, "SELECT count(*) FROM assess_jobs") == 0
        assert await n(pg, audited) == before + 1
