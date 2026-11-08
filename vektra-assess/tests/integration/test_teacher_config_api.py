"""PUT / GET the teacher configuration through the real routes (plan assess-teacher-ops 11a).

Real Postgres, the restricted API role, worker-written ``assess_teacher_gate`` rows. Every
success is checked on the rows (versions, sealed fields the worker opens, audit rows), every
refusal on what it left behind (0 state change, the refusal audited, a refresh queued).
"""

from __future__ import annotations

import json
import uuid
from typing import Any

import pytest
from sqlalchemy.exc import DBAPIError
from sqlalchemy.ext.asyncio import create_async_engine
from support.recipients import config_pair
from support.world import auth, default_settings, err, make_key, world

from integration.test_teacher_authz import COURSE, EDITOR, OTHER, seed_gate
from vektra_assess.api import teacher_config as route
from vektra_assess.crypto.teacher_config import read_teacher_config
from vektra_assess.delivery.recipients import ConfigRecipients, recipient_ref

pytestmark = pytest.mark.integration

SOLUTION = "Soluzione attesa: 42.\r\nPasso 2\x07 fine"
A, B = "Prof.Rossi@unical.it", "m.bianchi@unical.it"


def settings() -> Any:
    return default_settings(VEKTRA_ASSESS_RECIPIENT_DOMAINS="unical.it")


def url(cmid: int) -> str:
    return f"/api/v1/assess/activities/{cmid}/config"


def body(**over: Any) -> dict[str, Any]:
    return {
        "actor_userid": EDITOR,
        "courseid": COURSE,
        "activity_type": "assign",
        "expected_version": 0,
        **over,
    }


def get_headers(actor: int = EDITOR, key: str = "teacher") -> dict[str, str]:
    return {
        **auth(key),
        "X-Assess-Actor": str(actor),
        "X-Assess-Course": str(COURSE),
        "X-Assess-Activity-Type": "assign",
    }


async def counts(pg, site: uuid.UUID) -> tuple[int, int, int]:  # type: ignore[no-untyped-def]
    rows = await pg.run(
        "super",
        "SELECT (SELECT count(*) FROM assess_activity_config WHERE site_id = :s), "
        "(SELECT count(*) FROM assess_activity_config_sealed WHERE site_id = :s), "
        "(SELECT coalesce(max(version), 0) FROM assess_activity_config WHERE site_id = :s)",
        {"s": site},
    )
    return tuple(rows[0])  # type: ignore[return-value]


async def audits(
    pg, site: uuid.UUID, event: str = "assess_teacher_config_changed"
) -> list:  # type: ignore[no-untyped-def]
    return await pg.run(
        "super",
        "SELECT reason_code, data FROM assess_audit_events WHERE site_id = :s AND event = :e "
        "ORDER BY id",
        {"s": site, "e": event},
    )


@pytest.fixture
def pair():  # type: ignore[no-untyped-def]
    return config_pair()


async def ready(pg, pair, cmid: int = 60, **facts: Any):  # type: ignore[no-untyped-def]
    ctx = world(pg, settings())
    w = await ctx.__aenter__()
    w.app.state.assess_keys[route.PUBLIC_KEY] = pair[0]
    await seed_gate(pg, w.site, cmid, **facts)
    return ctx, w


async def test_a_new_activity_reads_safe_defaults(pg, pair) -> None:  # type: ignore[no-untyped-def]
    ctx, w = await ready(pg, pair)
    try:
        r = await w.client.get(url(60), headers=get_headers())
        assert r.status_code == 200, r.text
        got = r.json()
        assert got["configured"] is False and got["version"] == 0
        assert (got["enabled"], got["rag_enabled"], got["show_solution"]) == (
            False,
        ) * 3
        assert got["available_until"] is None
        assert {got[k] for k in ("solution_state", "pasted_rubric_state")} == {
            "non impostata"
        }
        assert got["transcript_recipients_state"] == "non impostata"
        assert got["rubric_source"] == "moodle" and got["active_sessions"] == 0
        assert got["opening"]["source"] == "activity_close"
        assert r.headers["cache-control"] == "no-store"
    finally:
        await ctx.__aexit__(None, None, None)


async def test_save_seals_each_field_audits_old_new_and_never_returns_a_secret(
    pg, pair
) -> None:  # type: ignore[no-untyped-def]
    ctx, w = await ready(pg, pair)
    try:
        r = await w.client.put(
            url(60),
            json=body(
                cap_minutes=30,
                show_solution=True,
                solution=SOLUTION,
                transcript_recipients=[A, "prof.rossi@unical.it", B],
            ),
            headers=auth("teacher"),
        )
        assert r.status_code == 200, r.text
        got = r.json()
        assert got["version"] == 1 and got["cap_minutes"] == 30
        assert got["solution_state"] == "impostata"
        assert got["transcript_recipients_state"] == "impostata"
        assert (
            got["transcript_recipients_count"] == 2
        )  # case-insensitive duplicate collapsed
        assert got["pasted_rubric_state"] == "non impostata"
        for secret in ("Soluzione", "unical", "Rossi"):
            assert secret not in r.text

        # ARCH-071: the API role cannot read the sealed solution back
        for sql in (
            "SELECT sealed_blob FROM assess_activity_config_sealed",
            "SELECT * FROM assess_activity_config_sealed",
        ):
            with pytest.raises(DBAPIError) as exc:
                await pg.run("api", sql)
            assert exc.value.orig.sqlstate == "42501"
        # the worker opens it: normalised (CRLF -> LF, control removed), and the recipients
        worker = create_async_engine(pg.url("worker"))
        try:
            async with worker.connect() as conn:
                opened = await read_teacher_config(conn, pair[1], w.site, 60)
                assert opened.solution == "Soluzione attesa: 42.\nPasso 2 fine"
                assert await ConfigRecipients(pair[1]).read(conn, w.site, 60) == (
                    A.replace("UNICAL", "unical"),
                    B,
                )
        finally:
            await worker.dispose()

        rows = await audits(pg, w.site)
        fields = [(r.reason_code, r.data.get("action")) for r in rows]
        assert ("cap_minutes", None) in fields and ("show_solution", None) in fields
        assert ("solution", "set") in fields and (
            "transcript_recipients",
            "set",
        ) in fields
        cap = next(r.data for r in rows if r.reason_code == "cap_minutes")
        assert (cap["old"], cap["new"], cap["actor"], cap["version"]) == (
            None,
            30,
            EDITOR,
            1,
        )
        rec = next(r.data for r in rows if r.reason_code == "transcript_recipients")
        assert {rec["new_1"], rec["new_2"]} == {
            recipient_ref(w.site, "Prof.Rossi@unical.it"),
            recipient_ref(w.site, B),
        }
        dumped = json.dumps([r.data for r in rows])
        for secret in ("unical", "Rossi", "bianchi", "Soluzione", "Passo 2"):
            assert secret not in dumped  # 0 secret content in the audit

        # write-only semantics: absent = unchanged, null = clear
        r = await w.client.put(
            url(60),
            json=body(expected_version=1, cap_minutes=45),
            headers=auth("teacher"),
        )
        assert r.status_code == 200 and r.json()["solution_state"] == "impostata"
        r = await w.client.put(
            url(60),
            json=body(expected_version=2, solution=None),
            headers=auth("teacher"),
        )
        assert r.status_code == 200 and r.json()["solution_state"] == "non impostata"
        assert ("solution", "cleared") in [
            (x.reason_code, x.data.get("action")) for x in await audits(pg, w.site)
        ]
        g = await w.client.get(url(60), headers=get_headers())
        assert g.json()["version"] == 3 and g.json()["cap_minutes"] == 45
    finally:
        await ctx.__aexit__(None, None, None)


async def test_a_stale_version_is_409_and_changes_nothing(pg, pair) -> None:  # type: ignore[no-untyped-def]
    ctx, w = await ready(pg, pair)
    try:
        assert (
            await w.client.put(
                url(60), json=body(cap_minutes=30), headers=auth("teacher")
            )
        ).status_code == 200
        before = await counts(pg, w.site)
        r = await w.client.put(
            url(60), json=body(cap_minutes=50, solution="x"), headers=auth("teacher")
        )
        assert r.status_code == 409
        assert err(r)["code"] == "ERR-ASSESS-023"
        assert err(r)["details"] == {"current_version": 1}
        assert await counts(pg, w.site) == before
    finally:
        await ctx.__aexit__(None, None, None)


async def test_field_errors_name_their_range_and_change_nothing(pg, pair) -> None:  # type: ignore[no-untyped-def]
    ctx, w = await ready(pg, pair)
    try:
        before = await counts(pg, w.site)
        r = await w.client.put(
            url(60),
            json=body(
                cap_minutes=121,
                rag_enabled=True,
                solution="a" * 20_001,
                transcript_recipients=["x@gmail.com"],
            ),
            headers=auth("teacher"),
        )
        assert r.status_code == 400, r.text
        e = err(r)
        assert e["code"] == "ERR-ASSESS-022"
        assert e["details"]["fields"] == [
            {"field": "cap_minutes", "code": "out_of_range", "range": "5-120"},
            {
                "field": "rag_enabled",
                "code": "rag_unavailable",
                "reason": "operator_flag_off",
            },
            {"field": "solution", "code": "too_long", "range": "0-20000"},
            {
                "field": "transcript_recipients",
                "code": "domain_not_allowed",
                "index": 0,
                "allowed_domains": ["unical.it"],
            },
        ]
        assert "gmail" not in r.text
        assert await counts(pg, w.site) == before
        # the boundary values themselves are accepted
        r = await w.client.put(
            url(60),
            json=body(cap_minutes=120, solution="a" * 20_000, rag_enabled=False),
            headers=auth("teacher"),
        )
        assert r.status_code == 200, r.text
        r = await w.client.put(
            url(60),
            json=body(expected_version=1, cap_minutes=4),
            headers=auth("teacher"),
        )
        assert err(r)["details"]["fields"][0]["range"] == "5-120"
        r = await w.client.put(
            url(60),
            json=body(expected_version=1, cap_minutes=5),
            headers=auth("teacher"),
        )
        assert r.status_code == 200
        # the source "pasted" with an empty pasted rubric
        r = await w.client.put(
            url(60),
            json=body(expected_version=2, rubric_source="pasted", pasted_rubric="  \n"),
            headers=auth("teacher"),
        )
        assert err(r)["details"]["fields"] == [
            {"field": "pasted_rubric", "code": "empty", "range": "1-20000"}
        ]
        # the end date not after the effective opening (the activity's close here)
        r = await w.client.put(
            url(60),
            json=body(expected_version=2, available_until="2030-03-17T17:46:40+00:00"),
            headers=auth("teacher"),
        )  # 1_900_000_000 == 2030-03-17T17:46:40Z
        assert err(r)["details"]["fields"][0]["code"] == "not_after_opening"
        # malformed (unknown field, wrong type) stays the generic 400, never 422
        r = await w.client.put(
            url(60),
            json=body(recipients_extra=["a@unical.it"]),
            headers=auth("teacher"),
        )
        assert r.status_code == 400 and err(r)["code"] == "ERR-ASSESS-014"
    finally:
        await ctx.__aexit__(None, None, None)


async def test_enable_is_refused_with_field_errors(pg, pair, monkeypatch) -> None:  # type: ignore[no-untyped-def]
    ctx, w = await ready(
        pg, pair, team_submission=True, grading_method="guide", close_at=0
    )
    try:
        r = await w.client.put(
            url(60),
            json=body(enabled=True, cap_minutes=30, transcript_recipients=[A]),
            headers=auth("teacher"),
        )
        assert r.status_code == 400
        got = {(f["field"], f["code"]) for f in err(r)["details"]["fields"]}
        assert {
            ("enabled", "assignment_without_cutoff"),
            ("enabled", "team_submission"),
            ("enabled", "privacy_unset"),  # fail closed until plan 12
            ("enabled", "setup_check_red"),
        } <= got
        assert (await counts(pg, w.site))[0] == 0

        # enable needs a recipient; with every precondition met it is accepted
        await seed_gate(pg, w.site, 60)

        async def configured(_conn: Any, _site: Any) -> bool:
            return True

        monkeypatch.setattr(route.privacy, "is_configured", configured)
        await pg.run(
            "super",
            "INSERT INTO assess_setup_check_reports (site_id, status, items, finished_at) "
            "VALUES (:s, 'done', CAST(:i AS jsonb), now())",
            {
                "s": w.site,
                "i": json.dumps([{"name": "x", "result": "OK", "reason_code": None}]),
            },
        )
        r = await w.client.put(
            url(60), json=body(enabled=True, cap_minutes=30), headers=auth("teacher")
        )
        assert ("transcript_recipients", "empty") in {
            (f["field"], f["code"]) for f in err(r)["details"]["fields"]
        }
        r = await w.client.put(
            url(60),
            json=body(enabled=True, cap_minutes=30, transcript_recipients=[A]),
            headers=auth("teacher"),
        )
        assert r.status_code == 200, r.text
        assert r.json()["enabled"] is True
    finally:
        await ctx.__aexit__(None, None, None)


async def test_unverified_teachers_are_refused_audited_and_change_nothing(
    pg, pair
) -> None:  # type: ignore[no-untyped-def]
    ctx, w = await ready(pg, pair)
    try:
        # stale facts: 403, a refresh queued, the refusal audited, no config written
        await seed_gate(pg, w.site, 61, age="10 minutes")
        r = await w.client.put(
            url(61), json=body(cap_minutes=30), headers=auth("teacher")
        )
        assert r.status_code == 403
        assert err(r)["code"] == "ERR-ASSESS-021"
        assert err(r)["details"]["reason"] == "editor_check_stale"
        jobs = await pg.run(
            "super",
            "SELECT count(*) FROM assess_jobs WHERE kind = 'teacher_gate' "
            "AND args->>'site_id' = :s AND (args->>'cmid')::int = 61",
            {"s": str(w.site)},
        )
        assert jobs[0][0] == 1
        # missing facts (GET), a grader who is not an editor (PUT)
        r = await w.client.get(url(62), headers=get_headers())
        assert err(r)["details"]["reason"] == "editor_check_stale"
        r = await w.client.put(
            url(60),
            json=body(actor_userid=OTHER, cap_minutes=30),
            headers=auth("teacher"),
        )
        assert r.status_code == 403 and err(r)["details"]["reason"] == "not_an_editor"
        rows = await audits(pg, w.site, "assess_teacher_unverified")
        assert [x.reason_code for x in rows] == ["stale", "stale", "not_an_editor"]
        assert await counts(pg, w.site) == (0, 0, 0)
        # an admin key or a mint key never reaches the route (exact scope)
        for name in ("admin", "mint"):
            r = await w.client.put(
                url(60), json=body(cap_minutes=30), headers=auth(name)
            )
            assert r.status_code == 403 and err(r)["code"] == "ERR-AUTH-003"
        # a teacher key of another site sees nothing of this one (its own site has no facts)
        other = await make_key(pg, ["assess:teacher"], w.other_site, "teacher")
        w.app.state.registry.get("key_store", "default").table["other_teacher"] = other
        r = await w.client.get(url(60), headers=get_headers(key="other_teacher"))
        assert (
            r.status_code == 403 and err(r)["details"]["reason"] == "editor_check_stale"
        )
        assert await counts(pg, w.site) == (0, 0, 0)
    finally:
        await ctx.__aexit__(None, None, None)


@pytest.fixture
async def worker(pg):  # type: ignore[no-untyped-def]
    from integration.test_session_preflight import _reset

    await _reset(pg)
    engine = create_async_engine(pg.url("worker"))
    yield engine
    await engine.dispose()


async def test_a_cap_change_never_moves_a_started_sessions_deadline(
    pg, pair, worker
) -> None:  # type: ignore[no-untyped-def]
    """REQ-077: changes apply only to sessions not yet started (the session copied its
    duration at Start)."""
    from support.sessions import seed_session

    async with world(pg, settings()) as w:
        w.app.state.assess_keys[route.PUBLIC_KEY] = pair[0]
        s = await seed_session(w, worker, "ACTIVE", cap_minutes=30)
        await seed_gate(pg, w.site, s.cmid)
        q = (
            "SELECT deadline_at, duration_s, cap_s, committed_at FROM assess_sessions "
            "WHERE id = :s"
        )
        before = (await pg.run("super", q, {"s": s.session_id}))[0]
        r = await w.client.put(
            url(s.cmid),
            json=body(cap_minutes=90, transcript_recipients=[A]),
            headers=auth("teacher"),
        )
        assert r.status_code == 200, r.text
        assert r.json()["cap_minutes"] == 90 and r.json()["active_sessions"] == 1
        after = (await pg.run("super", q, {"s": s.session_id}))[0]
        assert tuple(after) == tuple(before)
        assert before.deadline_at is not None
        assert (before.deadline_at - before.committed_at).total_seconds() == 30 * 60
        cfg = await pg.run(
            "super",
            "SELECT cap_minutes, version FROM assess_activity_config "
            "WHERE site_id = :s AND cmid = :c",
            {"s": w.site, "c": s.cmid},
        )
        assert tuple(cfg[0]) == (90, 1)


async def test_two_concurrent_first_saves_yield_one_row_and_a_409(pg, pair) -> None:  # type: ignore[no-untyped-def]
    """Review 2: a first save has no row to lock. While another first save holds its INSERT
    uncommitted, this one waits, then is told the version moved (409), never a 500."""
    import asyncio

    from sqlalchemy import text as sql

    ctx, w = await ready(pg, pair, cmid=63)
    try:
        other = create_async_engine(pg.url("api"))
        try:
            async with other.connect() as conn:
                await conn.execute(
                    sql(
                        "INSERT INTO assess_activity_config (site_id, cmid, activity_type, "
                        "version, updated_by) VALUES (:s, 63, 'assign', 1, :u)"
                    ),
                    {"s": w.site, "u": OTHER},
                )
                task = asyncio.create_task(
                    w.client.put(
                        url(63), json=body(cap_minutes=30), headers=auth("teacher")
                    )
                )
                await asyncio.sleep(
                    1.0
                )  # the PUT is now blocked on the uncommitted row
                assert not task.done()
                await conn.commit()
            r = await task
        finally:
            await other.dispose()
        assert r.status_code == 409, r.text
        assert err(r)["code"] == "ERR-ASSESS-023"
        assert err(r)["details"] == {"current_version": 1}
        assert (await counts(pg, w.site))[0] == 1
    finally:
        await ctx.__aexit__(None, None, None)


async def test_a_get_without_usable_hints_is_400_not_a_promised_refresh(
    pg, pair
) -> None:  # type: ignore[no-untyped-def]
    """Review 5: absent facts and no course / type hint: nothing can be queued, so the answer is
    a 400 naming the missing hint, never "a fresh check has been queued"."""
    ctx, w = await ready(pg, pair)
    try:
        for headers in (
            {**auth("teacher"), "X-Assess-Actor": str(EDITOR)},
            {**get_headers(), "X-Assess-Activity-Type": "forum"},
        ):
            r = await w.client.get(url(64), headers=headers)
            assert r.status_code == 400, r.text
            assert err(r)["code"] == "ERR-ASSESS-014"
            assert err(r)["details"] == {"reason": "teacher_gate_hint"}
        jobs = await pg.run(
            "super",
            "SELECT count(*) FROM assess_jobs WHERE kind = 'teacher_gate' AND args->>'site_id' = :s",
            {"s": str(w.site)},
        )
        assert jobs[0][0] == 0
        # fresh facts need no hint at all
        r = await w.client.get(
            url(60), headers={**auth("teacher"), "X-Assess-Actor": str(EDITOR)}
        )
        assert r.status_code == 200, r.text
    finally:
        await ctx.__aexit__(None, None, None)


async def test_texts_at_the_limit_fit_the_body_even_fully_escaped(pg, pair) -> None:  # type: ignore[no-untyped-def]
    """Review 6: PHP's json_encode escapes every non-ASCII code point (6 bytes for BMP, 12 for an
    astral one as a surrogate pair). Two texts at 20,000 code points must still reach the field
    rules: accepted at the limit, ``too_long`` one past it, never ``body_too_large``."""
    ctx, w = await ready(pg, pair)
    try:
        emoji, cjk = "\U0001f600", "漢"

        async def put(**fields: Any) -> Any:
            raw = json.dumps(body(**fields), ensure_ascii=True)
            return await w.client.put(
                url(60),
                content=raw,
                headers={**auth("teacher"), "Content-Type": "application/json"},
            )

        r = await put(solution=emoji * 20_000, pasted_rubric=cjk * 20_000)
        assert r.status_code == 200, r.text
        assert r.json()["solution_state"] == "impostata"
        r = await put(expected_version=1, solution=emoji * 20_001)
        assert r.status_code == 400, r.text
        assert err(r)["details"]["fields"] == [
            {"field": "solution", "code": "too_long", "range": "0-20000"}
        ]
    finally:
        await ctx.__aexit__(None, None, None)
