"""REQ-076 session list, the IDOR matrix of every teacher route, the reason rotation (11b).

Real Postgres and the restricted API role. The IDOR matrix runs every teacher route against
every refused caller and counts the executed cases against the declared matrix size (false-pass
guard), the 403s, the audit rows they left and the state they did not change.
"""

from __future__ import annotations

import os
import uuid
from typing import Any

import pytest
from sqlalchemy.ext.asyncio import create_async_engine
from support.teacher_ops import (
    COURSE,
    GRADER,
    NOT_GRADER,
    body,
    close,
    count,
    list_headers,
    live_sessions,
    seed_render,
    seed_teacher_gate,
    teacher,
    transcript_ring,
    with_transcript_key,
)
from support.world import auth, configure_activity, err, make_key, world

from integration.test_session_preflight import _reset
from integration.test_teacher_extra_time import caps
from vektra_assess.api import teacher_sessions
from vektra_assess.crypto import rotation
from vektra_assess.crypto.envelope import InvalidTag
from vektra_assess.crypto.teacher_reason import decrypt_reason
from vektra_assess.keys import AssessKeyring
from vektra_assess.transcript import store

pytestmark = pytest.mark.integration


@pytest.fixture
async def worker(pg):  # type: ignore[no-untyped-def]
    await _reset(pg)
    engine = create_async_engine(pg.url("worker"))
    yield engine
    await engine.dispose()


def list_url(cmid: int) -> str:
    return f"/api/v1/assess/activities/{cmid}/sessions"


def cursor(value: str, actor: int = GRADER) -> dict[str, Any]:
    # raw bytes: a header may carry any latin-1 octet ("²" is 0xB2)
    return {**list_headers(actor), "X-Assess-Cursor": value.encode("latin-1")}


async def test_the_list_shows_sessions_resets_extra_time_and_the_gate_panel(
    pg, worker, monkeypatch
) -> None:  # type: ignore[no-untyped-def]
    monkeypatch.setattr(teacher_sessions, "PAGE_SIZE", 2)
    async with world(pg) as w:
        with_transcript_key(w)
        cmid, seeded = await live_sessions(w, worker, [9001, 9002, 9003])
        await seed_teacher_gate(
            pg, w.site, cmid, override_holder=9002, override_viewers=[GRADER]
        )
        sid1 = seeded[9001][0]
        await close(worker, sid1, "service_failure")
        await seed_render(pg, w.site, 9001, cmid, lms_reasons=[])
        r = await w.client.post(
            f"/api/v1/assess/activities/{cmid}/sessions/{sid1}/reset",
            json=body(reason="interruzione di rete"),
            headers=teacher(),
        )
        assert r.status_code == 201, r.text
        await w.client.put(
            f"/api/v1/assess/activities/{cmid}/students/9004/extra-time",
            json=body(minutes=15),
            headers=teacher(),
        )

        r = await w.client.get(list_url(cmid), headers=list_headers())
        assert r.status_code == 200, r.text
        assert r.headers["cache-control"] == "no-store"
        page = r.json()
        assert [s["userid"] for s in page["students"]] == [9001, 9002]
        assert page["next_cursor"] == 9002
        first = page["students"][0]
        (session,) = first["sessions"]
        assert (session["session_no"], session["state"], session["close_reason"]) == (
            1,
            "CLOSED",
            "service_failure",
        )
        assert first["reset_count"] == 1
        (reset,) = first["resets"]
        assert reset["reason"] == "interruzione di rete"
        assert reset["include_previous_turns"] is True and reset["granted_by"] == GRADER
        assert reset["student_notice"] == "studente non avvisato via email"
        gate = page["gate"]
        assert gate["active_sessions"] == 2
        # an assignment: Vektra cannot list every extension, the C7 reason is shown
        assert gate["fallback_reason"] == (
            "Vektra non può verificare proroghe/override per questa attività"
        )
        assert gate["opening"]["source"] == "attested"
        assert gate["override_holder"] == 9002  # the actor may see it
        # no names, no addresses anywhere
        assert "@" not in r.text and "fullname" not in r.text

        r = await w.client.get(list_url(cmid), headers=cursor("9002"))
        page2 = r.json()
        assert [s["userid"] for s in page2["students"]] == [9003, 9004]
        assert page2["students"][1]["extra_seconds"] == 900
        assert page2["students"][1]["sessions"] == []
        r = await w.client.get(list_url(cmid), headers=cursor("9004"))
        assert r.json()["students"] == [] and r.json()["next_cursor"] is None
        for bad in ("x", "-1", "²", "0"):
            r = await w.client.get(list_url(cmid), headers=cursor(bad))
            assert r.status_code == 400, bad

        # a grader not allowed to see override holders does not see one
        await seed_teacher_gate(
            pg, w.site, cmid, graders=[GRADER, NOT_GRADER], override_holder=9002,
            override_viewers=[GRADER],
        )  # fmt: skip
        r = await w.client.get(list_url(cmid), headers=list_headers(NOT_GRADER))
        assert r.status_code == 200 and "override_holder" not in r.json()["gate"]
        await seed_teacher_gate(
            pg, w.site, cmid, override_holder=9002, override_viewers=None
        )
        r = await w.client.get(list_url(cmid), headers=list_headers())
        assert (
            "override_holder" not in r.json()["gate"]
        )  # unreadable viewers: fail closed


ROUTES = ("list", "config_get", "config_put", "reset", "terminate", "extra", "fallback")
CALLERS = ("other_course", "non_grader", "other_cmid", "cross_site", "admin")


async def _call(w, route: str, cmid: int, sid: Any, actor: int, key: str):  # type: ignore[no-untyped-def]
    b = body(actor_userid=actor)
    base = f"/api/v1/assess/activities/{cmid}"
    if route == "list":
        return await w.client.get(f"{base}/sessions", headers=list_headers(actor, key))
    if route == "config_get":
        return await w.client.get(f"{base}/config", headers=list_headers(actor, key))
    if route == "config_put":
        return await w.client.put(
            f"{base}/config", json={**b, "expected_version": 0}, headers=auth(key)
        )
    if route == "reset":
        return await w.client.post(
            f"{base}/sessions/{sid}/reset", json={**b, "reason": "r"}, headers=auth(key)
        )
    if route == "terminate":
        return await w.client.post(
            f"{base}/terminate",
            json={**b, "reason": "r", "expected_active_count": 1},
            headers=auth(key),
        )
    if route == "extra":
        return await w.client.put(
            f"{base}/students/9101/extra-time",
            json={**b, "minutes": 5},
            headers=auth(key),
        )
    return await w.client.put(
        f"{base}/opening-fallback",
        json={
            **b,
            "opening_at": "2031-01-01T00:00:00+00:00",
            "attestation": True,
            "attestation_text_version": "v1",
        },
        headers=auth(key),
    )


async def test_idor_matrix_every_teacher_route_refuses_every_wrong_caller(
    pg, worker
) -> None:  # type: ignore[no-untyped-def]
    """Other-course teacher (the cmid is not in the course they teach), non-grader of this
    course, grader of another cmid of the same course, a key bound to another site, an admin
    key: 403 on every route, an audit row for each refusal Vektra decides (the admin key is
    refused by the scope dependency itself), and no state change."""
    async with world(pg) as w:
        with_transcript_key(w)
        w.keys["teacher_other"] = await make_key(
            pg, ["assess:teacher"], w.other_site, "teacher"
        )
        w.app.state.registry.get("key_store", "default").table["teacher_other"] = (
            w.keys["teacher_other"]
        )
        cmid, seeded = await live_sessions(w, worker, [9101])
        sid = seeded[9101][0]
        other_cmid = cmid + 500
        other_teacher, other_course_teacher = 77, 78
        # this activity: only GRADER grades and edits; another cmid of the same course is
        # graded and edited by other_teacher; the other-course teacher's hint resolves nowhere
        await seed_teacher_gate(pg, w.site, cmid, editors=[GRADER])
        await seed_teacher_gate(
            pg, w.site, other_cmid, graders=[other_teacher], editors=[other_teacher]
        )
        await configure_activity(pg, w.site, other_cmid)
        actors = {
            "other_course": (other_course_teacher, "teacher"),
            "non_grader": (NOT_GRADER, "teacher"),
            "other_cmid": (other_teacher, "teacher"),
            "cross_site": (GRADER, "teacher_other"),
            "admin": (GRADER, "admin"),
        }

        async def snapshot() -> tuple[int, ...]:
            return tuple(
                [
                    await count(pg, sql, s=w.site)
                    for sql in (
                        "SELECT count(*) FROM assess_reset_grants WHERE site_id = :s",
                        "SELECT count(*) FROM assess_terminations WHERE site_id = :s",
                        "SELECT count(*) FROM assess_extra_time WHERE site_id = :s",
                        "SELECT count(*) FROM assess_sessions WHERE site_id = :s "
                        "AND state = 'ACTIVE'",
                        "SELECT coalesce(sum(version), 0) FROM assess_activity_config "
                        "WHERE site_id = :s",
                        "SELECT count(*) FROM assess_activity_config "
                        "WHERE site_id = :s AND attested_opening_at IS NOT NULL "
                        "AND attested_opening_at > now() + interval '1 year'",
                    )
                ]
            )

        before = await snapshot()
        audited_before = await count(
            pg,
            "SELECT count(*) FROM assess_audit_events WHERE event = 'assess_teacher_unverified'",
        )
        executed = 0
        expected_audits = 0
        for caller in CALLERS:
            actor, key = actors[caller]
            for route in ROUTES:
                if caller == "other_course":
                    # the other-course teacher's plugin hints their own course: resolved there
                    # the cmid is not found, a definitive "not a teacher here"
                    await seed_teacher_gate(
                        pg, w.site, cmid, resolved=False, reason="activity_not_in_course",
                        hint=[COURSE, "assign"],
                    )  # fmt: skip
                r = await _call(w, route, cmid, sid, actor, key)
                executed += 1
                assert r.status_code == 403, (caller, route, r.text)
                if caller == "admin":
                    # refused by the exact-scope dependency before any route code runs
                    assert "ERR-ASSESS-021" not in r.text
                elif caller == "cross_site":
                    # the other site has no facts for this cmid: refused, refresh queued
                    assert err(r)["code"] == "ERR-ASSESS-021"
                    expected_audits += 1
                else:
                    assert err(r)["code"] == "ERR-ASSESS-021", (caller, route)
                    assert err(r)["details"]["reason"] in (
                        "not_a_grader",
                        "not_an_editor",
                    )
                    expected_audits += 1
                if caller == "other_course":
                    await seed_teacher_gate(pg, w.site, cmid, editors=[GRADER])
        assert executed == len(CALLERS) * len(ROUTES) == 35  # the declared matrix
        audited = (
            await count(
                pg,
                "SELECT count(*) FROM assess_audit_events "
                "WHERE event = 'assess_teacher_unverified'",
            )
            - audited_before
        )
        assert audited == expected_audits == 28
        assert await snapshot() == before

        # the right grader, for contrast, is served (the matrix is not refusing everyone)
        r = await w.client.get(
            f"/api/v1/assess/activities/{cmid}/sessions", headers=list_headers()
        )
        assert r.status_code == 200
        # the right grader naming this session under the OTHER cmid (which they do not grade)
        r = await _call(w, "reset", other_cmid, sid, GRADER, "teacher")
        assert r.status_code == 403


async def test_reasons_follow_the_transcript_kek_rotation(pg, worker) -> None:  # type: ignore[no-untyped-def]
    async with world(pg) as w:
        # kids unique to this test: other tests of the module left rows under their own "t1"
        k1, k2 = f"r1-{uuid.uuid4().hex[:8]}", f"r2-{uuid.uuid4().hex[:8]}"
        old = with_transcript_key(w, transcript_ring(k1))
        cmid, seeded = await live_sessions(w, worker, [9201, 9202])
        await seed_teacher_gate(pg, w.site, cmid)
        sid = seeded[9201][0]
        await close(worker, sid)
        await seed_render(pg, w.site, 9201, cmid, lms_reasons=[])
        assert (
            await w.client.post(
                f"/api/v1/assess/activities/{cmid}/sessions/{sid}/reset",
                json=body(reason="motivo reset"),
                headers=teacher(),
            )
        ).status_code == 201
        assert (
            await w.client.post(
                f"/api/v1/assess/activities/{cmid}/terminate",
                json=body(reason="motivo termina", expected_active_count=1),
                headers=teacher(),
            )
        ).status_code == 200
        both = AssessKeyring(store.KEYRING, k2, {k1: old.keys[k1], k2: os.urandom(32)})
        async with worker.begin() as conn:
            assert await rotation.rewrap_teacher_reasons(conn, both, k1) == 0
        # the old kid can go: the reasons open with the new key alone
        new_only = AssessKeyring(store.KEYRING, k2, {k2: both.keys[k2]})
        rows = await pg.run(
            "super",
            "SELECT 'assess_reset_grants', id, reason_ct, reason_kid FROM assess_reset_grants "
            "WHERE site_id = :s UNION ALL SELECT 'assess_terminations', id, reason_ct, "
            "reason_kid FROM assess_terminations WHERE site_id = :s",
            {"s": w.site},
        )
        assert {r[3] for r in rows} == {k2}
        assert sorted(
            decrypt_reason(new_only, r[0], r[1], bytes(r[2])) for r in rows
        ) == ["motivo reset", "motivo termina"]
        # a ciphertext moved to another row does not open
        with pytest.raises(InvalidTag):
            decrypt_reason(new_only, rows[0][0], rows[1][1], bytes(rows[0][2]))
        # and the table is bound on its own: same row id, same ciphertext, other table
        other_table = (
            "assess_terminations"
            if rows[0][0] == "assess_reset_grants"
            else "assess_reset_grants"
        )
        with pytest.raises(InvalidTag):
            decrypt_reason(new_only, other_table, rows[0][1], bytes(rows[0][2]))


async def test_stale_facts_refuse_every_operation_queue_a_refresh_and_change_nothing(
    pg, worker
) -> None:  # type: ignore[no-untyped-def]
    """INT-001 on each 11b route: the grader list older than the bound is not trusted."""
    async with world(pg) as w:
        with_transcript_key(w)
        cmid, seeded = await live_sessions(w, worker, [9301])
        sid = seeded[9301][0]
        await seed_teacher_gate(pg, w.site, cmid, age="1 hour")
        before = await count(
            pg,
            "SELECT count(*) FROM assess_audit_events WHERE site_id = :s",
            s=w.site,
        )
        routes = ("list", "reset", "terminate", "extra", "fallback")
        hints = []
        for route in routes:
            r = await _call(w, route, cmid, sid, GRADER, "teacher")
            assert r.status_code == 403, (route, r.text)
            assert err(r)["details"]["reason"] == "grader_check_stale", route
            hints.append(err(r)["details"].get("retry_after_s"))
        # a refresh and one follow-up for the hint, deduplicated while pending; a retry is
        # promised only by the two calls that queued one (review of #221)
        assert (
            await count(
                pg,
                "SELECT count(*) FROM assess_jobs WHERE kind = 'teacher_gate' "
                "AND args->>'site_id' = :s AND state = 'ready'",
                s=str(w.site),
            )
            == 2
        )
        assert hints == [3, 3, None, None, None]
        rows = await pg.run(
            "super",
            "SELECT event, reason_code FROM assess_audit_events WHERE site_id = :s "
            "ORDER BY id OFFSET :n",
            {"s": w.site, "n": before},
        )
        assert [tuple(x) for x in rows] == [
            ("assess_teacher_unverified", "stale")
        ] * len(routes)
        assert (
            await count(
                pg,
                "SELECT count(*) FROM assess_sessions WHERE id = :i AND state = 'ACTIVE'",
                i=sid,
            )
            == 1
        )
        for table in (
            "assess_reset_grants",
            "assess_terminations",
            "assess_extra_time",
        ):
            assert (
                await count(
                    pg, f"SELECT count(*) FROM {table} WHERE site_id = :s", s=w.site
                )
                == 0
            )


async def test_the_config_view_and_the_gate_panel_show_the_same_opening(pg) -> None:  # type: ignore[no-untyped-def]
    """Review 11b-8: one rule, two pages; an attested date, then one before the close."""
    async with world(pg) as w:
        with_transcript_key(w)
        for attested_sql, source in (
            ("to_timestamp(1900000100)", "attested"),
            ("to_timestamp(1899999000)", "activity_close"),
        ):
            await configure_activity(pg, w.site, 906, attested=False)
            await pg.run(
                "super",
                f"UPDATE assess_activity_config SET attested_opening_at = {attested_sql} "
                "WHERE site_id = :s AND cmid = 906",
                {"s": w.site},
            )
            await seed_teacher_gate(
                pg, w.site, 906, close_at=1_900_000_000, editors=[GRADER]
            )
            config = await w.client.get(
                "/api/v1/assess/activities/906/config", headers=list_headers()
            )
            panel = await w.client.get(list_url(906), headers=list_headers())
            assert config.status_code == panel.status_code == 200
            assert config.json()["opening"] == panel.json()["gate"]["opening"]
            assert panel.json()["gate"]["opening"]["source"] == source


async def test_an_unreadable_reason_does_not_take_the_list_down(pg, worker) -> None:  # type: ignore[no-untyped-def]
    """Review 11b-4a: a reason under a retired kid (or tampered) shows as unreadable, the
    others and the rest of the list still render."""
    async with world(pg) as w:
        with_transcript_key(w)
        cmid, seeded = await live_sessions(w, worker, [9401, 9402])
        await seed_teacher_gate(pg, w.site, cmid)
        for student in (9401, 9402):
            sid = seeded[student][0]
            await close(worker, sid)
            await seed_render(pg, w.site, student, cmid, lms_reasons=[])
            r = await w.client.post(
                f"/api/v1/assess/activities/{cmid}/sessions/{sid}/reset",
                json=body(reason=f"motivo {student}"),
                headers=teacher(),
            )
            assert r.status_code == 201, r.text
        # 9401's reason moved onto 9402's row: authenticated against another row id, unreadable
        await pg.run(
            "super",
            "UPDATE assess_reset_grants SET reason_ct = (SELECT reason_ct FROM "
            "assess_reset_grants WHERE site_id = :s AND student_userid = 9401) "
            "WHERE site_id = :s AND student_userid = 9402",
            {"s": w.site},
        )
        r = await w.client.get(list_url(cmid), headers=list_headers())
        assert r.status_code == 200, r.text
        got = {s["userid"]: s["resets"][0] for s in r.json()["students"]}
        assert (got[9401]["reason"], got[9401]["reason_state"]) == (
            "motivo 9401",
            "readable",
        )
        assert (got[9402]["reason"], got[9402]["reason_state"]) == (None, "unreadable")


async def test_the_panel_shows_the_opening_start_waits_for(pg) -> None:  # type: ignore[no-untyped-def]
    """CodeRabbit 4506916932: with a readable automatic close (an override past the quiz
    close, plus grace) the panel and the config view show it, not the quiz's own close; the
    holder's id stays hidden from a non-viewer. The capability matrix holds the quiz's close
    inputs readable (otherwise the REQ-091 path applies, whatever the last facts read got)."""
    async with world(pg) as w:
        with_transcript_key(w)
        await caps(
            pg, w.site, "4.5 (Build: 20250101)", quiz_overrides="readable",
            quiz_grace="readable",
        )  # fmt: skip
        await configure_activity(pg, w.site, 908, kind="quiz", attested=False)
        await seed_teacher_gate(
            pg, w.site, 908, kind="quiz", close_at=1_900_000_000,
            automatic_close_at=1_900_007_260, override_holder=9501,
            override_viewers=[], editors=[GRADER],
        )  # fmt: skip
        headers = {**list_headers(), "X-Assess-Activity-Type": "quiz"}
        panel = (await w.client.get(list_url(908), headers=headers)).json()["gate"]
        config = (
            await w.client.get("/api/v1/assess/activities/908/config", headers=headers)
        ).json()
        expected = {
            "at": "2030-03-17T19:47:40+00:00",
            "source": "activity_close",
            "fallback_required": False,
        }
        assert panel["opening"] == config["opening"] == expected
        assert "override_holder" not in panel


async def expect_opening(
    w: Any, cmid: int, kind: str, at: str | None, source: str, required: bool
) -> None:
    """The config view's ``opening`` and the gate panel's are both exactly this."""
    headers = {**list_headers(), "X-Assess-Activity-Type": kind}
    config = await w.client.get(
        f"/api/v1/assess/activities/{cmid}/config", headers=headers
    )
    panel = await w.client.get(list_url(cmid), headers=headers)
    assert config.status_code == panel.status_code == 200, (config.text, panel.text)
    expected = {"at": at, "source": source, "fallback_required": required}
    assert config.json()["opening"] == expected
    assert panel.json()["gate"]["opening"] == expected


async def test_both_views_ask_for_the_fallback_start_needs(pg) -> None:  # type: ignore[no-untyped-def]
    """Plugin-team report (2026-11-12): GET config said ``activity_close`` while ``/state``
    said ``not_open/close_unreadable``. An assignment without a REQ-091 date (its extensions
    are never listable) and a quiz whose overrides were unreadable now read ``unreadable`` with
    ``fallback_required`` in both views; the PUT of the date turns them ``attested``."""
    close_at = 1_900_000_000
    async with world(pg) as w:
        with_transcript_key(w)
        # an assignment: the Start gate always takes the REQ-091 path
        await configure_activity(pg, w.site, 910, attested=False)
        await seed_teacher_gate(pg, w.site, 910, close_at=close_at, editors=[GRADER])
        await expect_opening(w, 910, "assign", None, "unreadable", True)
        r = await w.client.put(
            "/api/v1/assess/activities/910/opening-fallback",
            json=body(
                opening_at="2030-03-17T18:00:00+00:00",
                attestation=True,
                attestation_text_version="it-2026-1",
            ),
            headers=teacher(),
        )
        assert r.status_code == 200, r.text
        await expect_opening(
            w, 910, "assign", "2030-03-17T18:00:00+00:00", "attested", False
        )

        # a quiz whose overrides were unreadable (no automatic close), a 10-minute grace
        await configure_activity(pg, w.site, 911, kind="quiz", attested=False)
        await seed_teacher_gate(
            pg, w.site, 911, kind="quiz", close_at=close_at, automatic_close_at=None,
            grace_s=600, editors=[GRADER],
        )  # fmt: skip
        await expect_opening(w, 911, "quiz", None, "unreadable", True)
        # a teacher date inside the grace: the gate opens at close + grace, as Start does
        await pg.run(
            "super",
            "UPDATE assess_activity_config SET attested_opening_at = "
            "to_timestamp(:t) WHERE site_id = :s AND cmid = 911",
            {"s": w.site, "t": close_at + 60},
        )
        # at: close_at + 600
        await expect_opening(
            w, 911, "quiz", "2030-03-17T17:56:40+00:00", "activity_close", False
        )

        # the activity's own close unreadable: a teacher date cannot open it
        await seed_teacher_gate(
            pg, w.site, 911, kind="quiz", close_at=None, editors=[GRADER]
        )
        await expect_opening(w, 911, "quiz", None, "unreadable", False)


async def test_the_opening_fallback_body_requires_the_common_fields(pg) -> None:  # type: ignore[no-untyped-def]
    """docs/reference/api.md: ``courseid`` and ``activity_type`` are required (with
    ``actor_userid``); a missing one is ``400 ERR-ASSESS-014`` ``missing:<field>``."""
    async with world(pg) as w:
        await configure_activity(pg, w.site, 912, attested=False)
        await seed_teacher_gate(pg, w.site, 912)
        full = body(
            opening_at="2030-03-17T18:00:00+00:00",
            attestation=True,
            attestation_text_version="it-2026-1",
        )
        for name in ("courseid", "activity_type", "actor_userid"):
            sent = {k: val for k, val in full.items() if k != name}
            r = await w.client.put(
                "/api/v1/assess/activities/912/opening-fallback",
                json=sent,
                headers=teacher(),
            )
            assert r.status_code == 400, r.text
            e = err(r)
            assert (e["code"], e["details"]["reason"]) == (
                "ERR-ASSESS-014",
                f"missing:{name}",
            )


async def test_an_out_of_range_close_still_renders_the_list(pg, worker) -> None:  # type: ignore[no-untyped-def]
    """CodeRabbit nitpick (PR #179 round 2): a Moodle close beyond datetime's range that
    decides the opening renders as a null date; the route answers 200 with its students."""
    async with world(pg) as w:
        with_transcript_key(w)
        cmid, _seeded = await live_sessions(w, worker, [9601])
        await seed_teacher_gate(pg, w.site, cmid, close_at=2**62)
        r = await w.client.get(list_url(cmid), headers=list_headers())
        assert r.status_code == 200, r.text
        assert r.json()["gate"]["opening"]["at"] is None
        assert [s["userid"] for s in r.json()["students"]] == [9601]
