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
from support.world import auth, bind, default_settings, err, make_key, world

from integration.test_teacher_authz import COURSE, EDITOR, OTHER, seed_gate
from vektra_assess.api import teacher_config as route
from vektra_assess.crypto.teacher_config import read_teacher_config
from vektra_assess.delivery.recipients import ConfigRecipients, recipient_ref

pytestmark = pytest.mark.integration

SOLUTION = "Soluzione attesa: 42.\r\nPasso 2\x07 fine"
A, B = "Prof.Rossi@uni.example", "m.bianchi@uni.example"


def settings() -> Any:
    return default_settings(VEKTRA_ASSESS_RECIPIENT_DOMAINS="uni.example")


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
        assert got["rubric_source"] == "lms" and got["active_sessions"] == 0
        # an assignment without a REQ-091 date: Start refuses close_unreadable (REQ-091)
        assert got["opening"] == {
            "at": None,
            "source": "unreadable",
            "fallback_required": True,
        }
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
                transcript_recipients=[A, "prof.rossi@uni.example", B],
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
        for secret in ("Soluzione", "uni.example", "Rossi"):
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
                    A.replace("UNI.EXAMPLE", "uni.example"),
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
            recipient_ref(w.site, "Prof.Rossi@uni.example"),
            recipient_ref(w.site, B),
        }
        dumped = json.dumps([r.data for r in rows])
        for secret in ("uni.example", "Rossi", "bianchi", "Soluzione", "Passo 2"):
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
                "allowed_domains": ["uni.example"],
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
            json=body(recipients_extra=["a@uni.example"]),
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
                await bind(conn, w.site)  # as the route's own transaction is bound
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


async def test_non_ascii_digits_are_a_400_not_a_500(pg, pair) -> None:  # type: ignore[no-untyped-def]
    """Review 7: ``"²".isdigit()`` is True but ``int("²")`` raises: ids are ASCII digits only."""
    ctx, w = await ready(pg, pair)
    try:
        r = await w.client.get(url("%C2%B2"), headers=get_headers())  # type: ignore[arg-type]
        assert r.status_code == 400 and err(r)["details"] == {"reason": "cmid"}
        r = await w.client.get(
            "/api/v1/assess/activities/٣/config", headers=get_headers()
        )
        assert r.status_code == 400 and err(r)["details"] == {"reason": "cmid"}
        headers = {
            **get_headers(),
            "X-Assess-Actor": b"\xb2",
        }  # latin-1 superscript two
        r = await w.client.get(url(60), headers=headers)  # type: ignore[arg-type]
        assert r.status_code == 400 and err(r)["details"] == {
            "reason": "x_assess_actor"
        }
        headers = {**get_headers(), "X-Assess-Course": b"\xb2"}
        r = await w.client.get(url(60), headers=headers)  # type: ignore[arg-type]
        assert r.status_code == 200  # a malformed hint is no hint; the facts are fresh
    finally:
        await ctx.__aexit__(None, None, None)


async def test_null_on_a_setting_that_cannot_be_cleared_is_a_field_error(
    pg, pair
) -> None:  # type: ignore[no-untyped-def]
    """Review 8: ``null`` clears only the write-only fields, the cap and the end date. On
    ``enabled``, ``show_solution``, ``rag_enabled`` and ``rubric_source`` it is a field error,
    never a silent ``false`` (a silent disable)."""
    ctx, w = await ready(pg, pair)
    try:
        r = await w.client.put(
            url(60),
            json=body(show_solution=True, cap_minutes=30),
            headers=auth("teacher"),
        )
        assert r.status_code == 200
        before = await counts(pg, w.site)
        r = await w.client.put(
            url(60),
            json=body(
                expected_version=1,
                enabled=None,
                show_solution=None,
                rag_enabled=None,
                rubric_source=None,
            ),
            headers=auth("teacher"),
        )
        assert r.status_code == 400, r.text
        assert err(r)["details"]["fields"] == [
            {"field": f, "code": "null_not_allowed"}
            for f in ("enabled", "rubric_source", "show_solution", "rag_enabled")
        ]
        assert await counts(pg, w.site) == before
        g = await w.client.get(url(60), headers=get_headers())
        assert g.json()["show_solution"] is True
    finally:
        await ctx.__aexit__(None, None, None)


async def test_an_unrepresentable_end_date_is_a_400(pg, pair) -> None:  # type: ignore[no-untyped-def]
    """CodeRabbit 4505511246, through the route: never a 500 when the value is stored."""
    ctx, w = await ready(pg, pair)
    try:
        before = await counts(pg, w.site)
        r = await w.client.put(
            url(60),
            json=body(cap_minutes=30, available_until="9999-12-31T23:30:00-01:00"),
            headers=auth("teacher"),
        )
        assert r.status_code == 400, r.text
        assert err(r)["details"]["fields"] == [
            {"field": "available_until", "code": "out_of_range"}
        ]
        # review 2 nitpick: the valid field of the rejected request was not saved either
        assert await counts(pg, w.site) == before
    finally:
        await ctx.__aexit__(None, None, None)


async def test_a_naive_end_date_is_a_field_error_even_at_the_range_edge(
    pg, pair
) -> None:  # type: ignore[no-untyped-def]
    """CodeRabbit 4505705685: the field rule runs before any conversion; a naive
    0001-01-01T00:00:00 is ``timezone_required`` (400), never a conversion failure (500)."""
    ctx, w = await ready(pg, pair)
    try:
        for value in ("0001-01-01T00:00:00", "9999-12-31T23:59:00"):
            r = await w.client.put(
                url(60), json=body(available_until=value), headers=auth("teacher")
            )
            assert r.status_code == 400, r.text
            assert err(r)["details"]["fields"] == [
                {"field": "available_until", "code": "timezone_required"}
            ]
    finally:
        await ctx.__aexit__(None, None, None)


async def test_a_secret_without_the_sealing_key_is_refused_and_saves_nothing(
    pg, pair
) -> None:  # type: ignore[no-untyped-def]
    """Review 2 nitpick: an API without the worker's public key must refuse a secret field
    (503 teacher_config_key) before saving any part of the request."""
    ctx, w = await ready(pg, pair)
    try:
        del w.app.state.assess_keys[route.PUBLIC_KEY]
        before = await counts(pg, w.site)
        r = await w.client.put(
            url(60), json=body(cap_minutes=30, solution="x"), headers=auth("teacher")
        )
        assert r.status_code == 503, r.text
        assert err(r)["code"] == "ERR-ASSESS-001"
        assert "teacher_config_key" in err(r)["remediation"]
        assert await counts(pg, w.site) == before
        # a save with no secret field needs no key
        r = await w.client.put(
            url(60), json=body(cap_minutes=30), headers=auth("teacher")
        )
        assert r.status_code == 200, r.text
    finally:
        await ctx.__aexit__(None, None, None)


# --- FEAT-028: RAG ON with the teacher's attestation (REQ-077 amendment 2026-11-16) ----------


async def _rag_ready(pg, pair, readable: Any = True, read_age_s: int | None = 0):  # type: ignore[no-untyped-def]
    """``read_age_s``: how old the worker's course-module read is (None: no read time)."""
    import time

    ctx = world(pg, default_settings(
        VEKTRA_ASSESS_RECIPIENT_DOMAINS="uni.example", VEKTRA_ASSESS_RAG_ENABLED=True
    ))  # fmt: skip
    w = await ctx.__aenter__()
    w.app.state.assess_keys[route.PUBLIC_KEY] = pair[0]
    facts: dict[str, Any] = (
        {} if readable is None else {"course_modules_readable": readable}
    )
    if readable is not None and read_age_s is not None:
        facts["course_modules_read_at"] = int(time.time()) - read_age_s
    await seed_gate(pg, w.site, 60, **facts)
    return ctx, w


async def test_rag_on_needs_the_attestation_then_is_saved_and_audited(pg, pair) -> None:  # type: ignore[no-untyped-def]
    ctx, w = await _rag_ready(pg, pair)
    try:
        before = await counts(pg, w.site)
        r = await w.client.put(
            url(60), json=body(rag_enabled=True), headers=auth("teacher")
        )
        assert r.status_code == 400, r.text
        assert err(r)["details"]["fields"] == [
            {"field": "rag_enabled", "code": "rag_attestation_required"}
        ]
        r = await w.client.put(
            url(60),
            json=body(rag_enabled=True, rag_attested=False),
            headers=auth("teacher"),
        )
        assert r.status_code == 400, r.text
        assert await counts(pg, w.site) == before
        r = await w.client.put(
            url(60),
            json=body(rag_enabled=True, rag_attested=True),
            headers=auth("teacher"),
        )
        assert r.status_code == 200, r.text
        view = r.json()
        assert view["rag_enabled"] is True and view["rag_attested_at"] is not None
        rows = await pg.run(
            "super",
            "SELECT rag_enabled, rag_attested_at IS NOT NULL, rag_attested_by "
            "FROM assess_activity_config WHERE site_id = :s AND cmid = 60",
            {"s": w.site},
        )
        assert tuple(rows[0]) == (True, True, str(EDITOR))
        got = {code: data for code, data in await audits(pg, w.site)}
        assert got["rag_enabled"]["old"] is False and got["rag_enabled"]["new"] is True
        assert got["rag_attested"]["old"] is None
        assert got["rag_attested"]["new"] == view["rag_attested_at"]
        assert got["rag_attested"]["attested_by"] == str(EDITOR)
        # staying ON keeps the attestation (a save of another field re-checks nothing of it)
        r = await w.client.put(
            url(60),
            json=body(expected_version=1, cap_minutes=30),
            headers=auth("teacher"),
        )
        assert r.status_code == 200, r.text
        assert r.json()["rag_attested_at"] == view["rag_attested_at"]
        # turning it OFF clears the attestation, audited
        r = await w.client.put(
            url(60),
            json=body(expected_version=2, rag_enabled=False),
            headers=auth("teacher"),
        )
        assert r.status_code == 200, r.text
        assert r.json()["rag_enabled"] is False
        assert r.json()["rag_attested_at"] is None
        last = [d for c, d in await audits(pg, w.site) if c == "rag_attested"][-1]
        assert last["old"] == view["rag_attested_at"] and last["new"] is None
    finally:
        await ctx.__aexit__(None, None, None)


@pytest.mark.parametrize("readable", [False, None])
async def test_rag_on_with_an_unreadable_eligibility_is_refused_and_refreshed(
    pg, pair, readable: Any
) -> None:  # type: ignore[no-untyped-def]
    ctx, w = await _rag_ready(pg, pair, readable)
    try:
        before = await counts(pg, w.site)
        r = await w.client.put(
            url(60),
            json=body(rag_enabled=True, rag_attested=True),
            headers=auth("teacher"),
        )
        assert r.status_code == 400, r.text
        e = err(r)
        assert e["details"]["fields"] == [
            {
                "field": "rag_enabled",
                "code": "rag_unavailable",
                "reason": "eligibility_unreadable",
            }
        ]
        # an LMS fact: the refresh is queued and the retry hinted
        assert e["details"]["retry_after_s"] > 0
        assert await counts(pg, w.site) == before
    finally:
        await ctx.__aexit__(None, None, None)


async def test_a_rag_only_put_follows_the_other_fields_put(pg, pair) -> None:  # type: ignore[no-untyped-def]
    """Plugin contract (FEAT-028): the RAG field travels in its OWN PUT, after the PUT with the
    other fields, on the version that one returned. A refusal changes nothing; a stale version
    is the usual conflict."""
    ctx, w = await _rag_ready(pg, pair)
    try:
        r = await w.client.put(
            url(60),
            json=body(cap_minutes=30, show_solution=False),
            headers=auth("teacher"),
        )
        assert r.status_code == 200, r.text
        v1 = r.json()["version"]
        before = await counts(pg, w.site)
        r = await w.client.put(
            url(60),
            json=body(expected_version=v1, rag_enabled=True),
            headers=auth("teacher"),
        )
        assert r.status_code == 400, r.text
        assert err(r)["details"]["fields"] == [
            {"field": "rag_enabled", "code": "rag_attestation_required"}
        ]
        assert await counts(pg, w.site) == before  # nothing changed
        stale = await w.client.put(
            url(60),
            json=body(expected_version=v1 - 1, rag_enabled=True, rag_attested=True),
            headers=auth("teacher"),
        )
        assert stale.status_code == 409 and err(stale)["code"] == "ERR-ASSESS-023"
        assert await counts(pg, w.site) == before
        r = await w.client.put(
            url(60),
            json=body(expected_version=v1, rag_enabled=True, rag_attested=True),
            headers=auth("teacher"),
        )
        assert r.status_code == 200, r.text
        view = r.json()
        assert view["version"] == v1 + 1 and view["rag_enabled"] is True
        assert view["cap_minutes"] == 30  # the other fields are untouched
        assert view["rag_available"] is True and view["rag_unavailable_reason"] is None
        # rag_enabled=true again while ON, without rag_attested: idempotent, the record kept
        again = await w.client.put(
            url(60),
            json=body(expected_version=v1 + 1, rag_enabled=True),
            headers=auth("teacher"),
        )
        assert again.status_code == 200, again.text
        assert again.json()["rag_attested_at"] == view["rag_attested_at"]
        assert [c for c, _ in await audits(pg, w.site)].count("rag_attested") == 1
    finally:
        await ctx.__aexit__(None, None, None)


async def test_the_view_says_whether_rag_is_available(pg, pair) -> None:  # type: ignore[no-untyped-def]
    """GET config: ``rag_available`` / ``rag_unavailable_reason`` from the operator flag only."""
    ctx, w = await ready(pg, pair)
    try:
        r = await w.client.get(url(60), headers=get_headers())
        assert r.status_code == 200, r.text
        got = r.json()
        assert got["rag_available"] is False
        assert got["rag_unavailable_reason"] == "operator_flag_off"
        assert got["rag_enabled"] is False and got["rag_attested_at"] is None
    finally:
        await ctx.__aexit__(None, None, None)
    ctx, w = await _rag_ready(pg, pair, readable=None)
    try:
        got = (await w.client.get(url(60), headers=get_headers())).json()
        # the per-turn eligibility is not consulted: unreadable facts, still available
        assert got["rag_available"] is True and got["rag_unavailable_reason"] is None
    finally:
        await ctx.__aexit__(None, None, None)


@pytest.mark.parametrize("read_age_s", [None, 100_000])
async def test_a_readability_without_a_fresh_read_time_asks_for_a_rag_refresh(
    pg, pair, read_age_s: int | None
) -> None:  # type: ignore[no-untyped-def]
    """Review 9: the course-module readability counts only when read recently (within the
    teacher-gate freshness bound); otherwise the save is refused and a RAG refresh is queued
    (a teacher_gate job that reads course_modules), with the retry hint."""
    ctx, w = await _rag_ready(pg, pair, True, read_age_s)
    try:
        r = await w.client.put(
            url(60),
            json=body(rag_enabled=True, rag_attested=True),
            headers=auth("teacher"),
        )
        assert r.status_code == 400, r.text
        assert err(r)["details"]["fields"] == [
            {"field": "rag_enabled", "code": "rag_unavailable",
             "reason": "eligibility_unreadable"}
        ]  # fmt: skip
        assert err(r)["details"]["retry_after_s"] > 0
        queued = await pg.run(
            "super",
            "SELECT args FROM assess_jobs WHERE kind = 'teacher_gate' "
            "AND args->>'site_id' = :s AND args->>'rag' = '1'",
            {"s": str(w.site)},
        )
        assert queued, "no RAG refresh was queued"
    finally:
        await ctx.__aexit__(None, None, None)


async def test_the_attestation_audit_reads_no_extra_config_row(pg, pair) -> None:  # type: ignore[no-untyped-def]
    """Review 10: the attestation's audit takes the stored time and actor from the write itself
    (RETURNING), not from a second read of the config row."""
    from sqlalchemy import event

    ctx, w = await _rag_ready(pg, pair)
    reads: list[str] = []

    def count(_c, _cur, statement, *_a):  # type: ignore[no-untyped-def]
        if (
            "FROM assess_activity_config " in statement
            and "rag_attested_by" in statement
        ):
            reads.append(statement)

    engine = w.app.state.assess_engine.sync_engine
    event.listen(engine, "before_cursor_execute", count)
    try:
        r = await w.client.put(
            url(60),
            json=body(rag_enabled=True, rag_attested=True),
            headers=auth("teacher"),
        )
        assert r.status_code == 200, r.text
        # the row lock of the save, and the view: nothing more
        assert len(reads) == 2, reads
        (audit,) = [d for c, d in await audits(pg, w.site) if c == "rag_attested"]
        assert audit["new"] == r.json()["rag_attested_at"]
        assert audit["attested_by"] == str(EDITOR)
    finally:
        event.remove(engine, "before_cursor_execute", count)
        await ctx.__aexit__(None, None, None)
