"""``GET /api/v1/assess/privacy/subjects/{userid}/activities`` (plugin request, CodeRabbit on the
plugin's PR #9): every activity of the key's site where the subject has ANY row the erasure would
delete or rewrite, with what the export owes the subject, so the LMS's export and its erasure
approval see what ``DELETE /privacy/subjects/{userid}`` erases.

The set and the projection are ``privacy.subject``'s (``SUBJECT_TABLES``, ``SUBJECT_EXPORT``).
The oracle here is independent of both: it reads the schema (``information_schema``) for every
``assess_*`` table with a subject column or a key to a session.
"""

from __future__ import annotations

import uuid
from typing import Any

import pytest
from sqlalchemy.ext.asyncio import create_async_engine
from support.sessions import seed_session
from support.world import auth, default_settings, err, make_key, world

from integration.test_privacy_retention import _privacy_key, erase, ready
from vektra_assess.privacy import purge
from vektra_assess.privacy import subject as subj

pytestmark = pytest.mark.integration


@pytest.fixture
async def worker(pg):  # type: ignore[no-untyped-def]
    from integration.test_session_preflight import _reset

    await _reset(pg)
    engine = create_async_engine(pg.url("worker"))
    yield engine
    await engine.dispose()


URL = "/api/v1/assess/privacy/subjects/{u}/activities"
TECHNICAL = {
    "assess_start_attempts",
    "assess_token_jti",
    "assess_gate_inputs",
    "assess_render_state",
    "assess_render_checks",
}


async def _get(w, user: Any, key: str = "export", cursor: str | None = None):  # type: ignore[no-untyped-def]
    headers = auth(key)
    if cursor is not None:
        headers["X-Assess-Cursor"] = cursor
    return await w.client.get(URL.format(u=user), headers=headers)


async def _reset_only(pg, site: uuid.UUID, cmid: int, user: int) -> None:  # type: ignore[no-untyped-def]
    await pg.run(
        "super",
        "INSERT INTO assess_reset_grants (site_id, cmid, student_userid, granted_by, "
        "session_no, reason_ct, reason_kid) VALUES (:s, :c, :u, 'grader:1', 1, :ct, 'k1')",
        {"s": site, "c": cmid, "u": user, "ct": b"\x00" * 16},
    )


async def _extra_only(pg, site: uuid.UUID, cmid: int, user: int, s: int = 600) -> None:  # type: ignore[no-untyped-def]
    await pg.run(
        "super",
        "INSERT INTO assess_extra_time (site_id, cmid, student_userid, extra_seconds) "
        "VALUES (:s, :c, :u, :x)",
        {"s": site, "c": cmid, "u": user, "x": s},
    )


async def _tombstone_only(pg, site: uuid.UUID, cmid: int, user: int) -> None:  # type: ignore[no-untyped-def]
    """What a retention purge leaves of a session: ids and a subject reference."""
    await pg.run(
        "super",
        "INSERT INTO assess_purge_records (site_id, subject_ref, session_id, session_no, "
        "cmid, reason) VALUES (:s, :ref, :sid, 1, :c, 'retention')",
        {
            "s": site,
            "ref": subj.subject_ref(site, user),
            "sid": uuid.uuid4(),
            "c": cmid,
        },
    )


async def _schema_subject_tables(pg) -> tuple[set[str], set[str]]:  # type: ignore[no-untyped-def]
    """From the schema alone: (tables with a ``student_userid`` column, tables with a
    ``subject_ref`` column or a foreign key to ``assess_sessions`` / ``assess_transcripts``)."""
    by_user = {
        r[0]
        for r in await pg.run(
            "super",
            "SELECT table_name FROM information_schema.columns WHERE table_schema = 'public' "
            "AND table_name LIKE 'assess\\_%' AND column_name = 'student_userid'",
        )
    }
    linked = {
        r[0]
        for r in await pg.run(
            "super",
            "SELECT DISTINCT tc.table_name FROM information_schema.table_constraints tc "
            "JOIN information_schema.constraint_column_usage cu "
            "ON cu.constraint_name = tc.constraint_name "
            "WHERE tc.constraint_type = 'FOREIGN KEY' AND tc.table_schema = 'public' "
            "AND cu.table_name IN ('assess_sessions', 'assess_transcripts') "
            "AND tc.table_name LIKE 'assess\\_%' "
            "UNION SELECT table_name FROM information_schema.columns "
            "WHERE table_schema = 'public' AND table_name LIKE 'assess\\_%' "
            "AND column_name IN ('subject_ref', 'subject_userids', 'subject_userid_hash')",
        )
    }
    return by_user, linked


async def _cmids_with_rows(pg, site: uuid.UUID, user: int) -> set[int]:  # type: ignore[no-untyped-def]
    """Independently of the route and of ``SUBJECT_TABLES``: every cmid where a table the
    schema scan finds holds a row of the subject, plus its retention tombstones."""
    by_user, _ = await _schema_subject_tables(pg)
    out: set[int] = set()
    for table in by_user:
        rows = await pg.run(
            "super",
            f"SELECT DISTINCT cmid FROM {table} WHERE site_id = :s AND student_userid = :u",
            {"s": site, "u": user},
        )
        out |= {int(r[0]) for r in rows}
    rows = await pg.run(
        "super",
        "SELECT DISTINCT cmid FROM assess_purge_records WHERE site_id = :s AND subject_ref = :r",
        {"s": site, "r": subj.subject_ref(site, user)},
    )
    return out | {int(r[0]) for r in rows}


async def test_every_subject_table_of_the_schema_is_covered(pg, worker) -> None:  # type: ignore[no-untyped-def]
    """Findings 2+3: the schema, not the code, says which tables hold a subject. Each is a
    subject table (exported by declaration), session-linked content (reached through the
    subject's sessions), the tombstones, or a documented exclusion; and every table the erasure
    touches is one the listing covers."""
    by_user, linked = await _schema_subject_tables(pg)
    session_linked = {
        *purge.SESSION_TABLES,
        *purge.TRANSCRIPT_TABLES,
        "assess_transcripts",
        "assess_turn_notes",  # keyed to its turn, deleted with the session's content
    }
    covered = (
        set(subj.SUBJECT_TABLES)
        | session_linked
        | {"assess_purge_records"}
        | set(subj.EXPORT_EXCLUDED)
    )
    assert by_user <= set(subj.SUBJECT_TABLES) | set(subj.EXPORT_EXCLUDED), by_user
    assert linked <= covered, sorted(linked - covered)
    assert set(subj.SUBJECT_EXPORT) == set(subj.SUBJECT_TABLES)
    # what the erasure touches (its own constants) is a subset of what the listing covers
    touched = {
        *subj.SUBJECT_TABLES,
        *purge.SESSION_TABLES,
        *purge.TRANSCRIPT_TABLES,
        "assess_transcripts",
        "assess_purge_records",
        "assess_jobs",
    }
    assert touched <= covered, sorted(touched - covered)


async def test_listing_export_and_erasure_cover_the_same_activities(pg, worker) -> None:  # type: ignore[no-untyped-def]
    """The drift cross-check: a transcript in A, only a live session in B, only a reset in C,
    only extra time in D, only a retention tombstone in E. All five are listed, the schema
    oracle finds exactly those, each but E lists the subject (a tombstone keeps a hash, no id),
    and erasing the subject deletes or rewrites exactly the listed activities' rows."""
    async with world(pg, default_settings(VEKTRA_ASSESS_ERASURE_WAIT_S=10)) as w:
        await _privacy_key(w, "assess:privacy-export", "export")
        await _privacy_key(w, "assess:privacy-erase", "erase")
        a, _ = await ready(w, worker, assignment="rubric_vis")
        b = await seed_session(w, worker, "ACTIVE", assignment="simple")
        assert b.sub == a.sub and b.cmid != a.cmid
        user = a.sub
        c, d, e = 770_001, 770_002, 770_003
        await _reset_only(pg, w.site, c, user)
        await _extra_only(pg, w.site, d, user, 900)
        await _tombstone_only(pg, w.site, e, user)
        # another subject in C: never listed for ours, untouched by the erasure
        await _extra_only(pg, w.site, c, 990_123)

        r = await _get(w, user)
        assert r.status_code == 200, r.text
        assert r.headers["cache-control"] == "no-store"
        body = r.json()
        assert body["next_cursor"] is None
        listed = {i["cmid"]: i for i in body["items"]}
        assert set(listed) == {a.cmid, b.cmid, c, d, e}
        assert set(listed) == await _cmids_with_rows(pg, w.site, user)
        assert [i["cmid"] for i in body["items"]] == sorted(listed)

        # A: a closed session with a transcript points to the transcript export
        (sa,) = listed[a.cmid]["sessions"]
        assert sa["state"] == "CLOSED" and sa["close_reason"] == "finished"
        assert sa["transcript_id"] and "turns" not in sa
        assert set(sa) == {
            "session_no",
            "state",
            "close_reason",
            "created_at",
            "deadline_at",
            "closed_at",
            "transcript_id",
        }
        # B: a live session carries its decrypted turns, the same the owner reads
        (sb,) = listed[b.cmid]["sessions"]
        assert sb["state"] == "ACTIVE" and "transcript_id" not in sb
        own = await w.client.get(
            f"/api/v1/assess/sessions/{b.session_id}",
            headers={"Authorization": f"Bearer {b.session_token}"},
        )
        assert own.status_code == 200, own.text
        want = [
            {k: t[k] for k in ("turn_no", "role", "kind", "text")}
            for t in own.json()["turns"]
        ]
        assert want and sb["turns"] == want
        assert sb["deadline_at"] is not None
        assert listed[b.cmid]["technical_records"]["assess_token_jti"] >= 1
        # C, D, E
        assert listed[c]["sessions"] == [] and listed[c]["extra_seconds"] == 0
        (g,) = listed[c]["resets"]
        assert (
            g["after_session_no"] == 1 and g["granted_at"] and g["consumed_at"] is None
        )
        assert listed[d]["extra_seconds"] == 900 and listed[d]["extra_granted_at"]
        assert listed[e]["purged_sessions"] == 1 and listed[e]["sessions"] == []
        for item in body["items"]:
            assert set(item) == {
                "cmid",
                "course_id",
                "sessions",
                "resets",
                "extra_seconds",
                "extra_granted_at",
                "purged_sessions",
                "technical_records",
            }
            assert set(item["technical_records"]) == TECHNICAL
            for g in item["resets"]:
                assert set(g) == {"after_session_no", "granted_at", "consumed_at"}

        # the activity listing includes the subject in every listed activity but E
        for cmid in set(listed) - {e}:
            got = await w.client.get(
                f"/api/v1/assess/privacy/activities/{cmid}/subjects",
                headers=auth("export"),
            )
            assert got.status_code == 200, got.text
            assert user in got.json()["userids"], cmid

        # the erasure deletes (or, for E, rewrites) exactly the listed activities' rows
        r = await erase(w, worker, f"/api/v1/assess/privacy/subjects/{user}")
        assert r.status_code == 200, r.text
        assert await _cmids_with_rows(pg, w.site, user) == set()
        after = await _get(w, user)
        assert after.json() == {"items": [], "next_cursor": None}
        assert await _cmids_with_rows(pg, w.site, 990_123) == {c}


async def test_the_course_is_the_sessions_else_the_latest_mints(pg, worker) -> None:  # type: ignore[no-untyped-def]
    """Finding 5: the session's hint over any mint's; with no session, the latest mint's (by
    ``minted_at``, not the largest id); with neither, null."""
    async with world(pg) as w:
        await _privacy_key(w, "assess:privacy-export", "export")
        user, with_session, mint_only, none = 880_010, 773_001, 773_002, 773_003
        await pg.run(
            "super",
            "INSERT INTO assess_sessions (site_id, cmid, student_userid, session_no, state, "
            "starting_expires_at, courseid_hint) VALUES "
            "(:s, :c, :u, 1, 'STARTING', now() + interval '1 minute', 42)",
            {"s": w.site, "c": with_session, "u": user},
        )
        for cmid, course, age in (
            (with_session, 99, "1 minute"),
            (mint_only, 900, "2 hours"),  # the largest hint, but the older mint
            (mint_only, 51, "1 hour"),
        ):
            await pg.run(
                "super",
                "INSERT INTO assess_token_jti (jti, site_id, student_userid, cmid, "
                "courseid_hint, mint_key_id, minted_at, expires_at) VALUES (:j, :s, :u, :c, "
                f":k, :key, now() - interval '{age}', now())",
                {
                    "j": uuid.uuid4().hex,
                    "s": w.site,
                    "u": user,
                    "c": cmid,
                    "k": course,
                    "key": w.keys["mint"].key_id,
                },
            )
        await _extra_only(pg, w.site, none, user)
        got = {i["cmid"]: i["course_id"] for i in (await _get(w, user)).json()["items"]}
        assert got == {with_session: 42, mint_only: 51, none: None}


async def _closed_without_transcript(w, worker) -> Any:  # type: ignore[no-untyped-def]
    """A CLOSED session whose transcript is not generated: the close queued its
    ``transcript_generate`` job (``assess_close_session``), no worker ran it."""
    s = await seed_session(w, worker, "CLOSED")
    jobs = await w.pg.run(
        "super",
        "SELECT state FROM assess_jobs WHERE kind = 'transcript_generate' "
        "AND args->>'session_id' = :s",
        {"s": str(s.session_id)},
    )
    assert [j[0] for j in jobs] == ["ready"]
    return s


async def _session_of(w, s) -> dict[str, Any]:  # type: ignore[no-untyped-def]
    items = (await _get(w, s.sub)).json()["items"]
    (item,) = [i for i in items if i["cmid"] == s.cmid]
    (session,) = item["sessions"]
    return session


@pytest.mark.parametrize(
    ("job_state", "code"),
    [
        ("ready", "transcript_pending"),
        ("leased", "transcript_pending"),
        ("failed", "transcript_failed"),
        ("dead", "transcript_failed"),
    ],
)
async def test_missing_turns_say_why(pg, worker, job_state: str, code: str) -> None:  # type: ignore[no-untyped-def]
    """The coordinator's decision on finding 1: a closed session without a transcript has
    ``turns: null`` and ``turns_unavailable`` from its transcript job: pending while queued or
    running (retry later), failed when generation ended failed or dead."""
    async with world(pg) as w:
        await _privacy_key(w, "assess:privacy-export", "export")
        s = await _closed_without_transcript(w, worker)
        await pg.run(
            "super",
            "UPDATE assess_jobs SET state = :st WHERE kind = 'transcript_generate' "
            "AND args->>'session_id' = :s",
            {"st": job_state, "s": str(s.session_id)},
        )
        session = await _session_of(w, s)
        assert session["state"] == "CLOSED" and "transcript_id" not in session
        assert session["turns"] is None
        assert session["turns_unavailable"] == code


async def test_a_purged_session_says_purged_and_a_present_transcript_says_nothing(
    pg, worker
) -> None:  # type: ignore[no-untyped-def]
    async with world(pg) as w:
        await _privacy_key(w, "assess:privacy-export", "export")
        s = await _closed_without_transcript(w, worker)
        await pg.run(
            "super",
            "INSERT INTO assess_purge_records (site_id, subject_ref, session_id, session_no, "
            "cmid, reason) VALUES (:s, :ref, :sid, 1, :c, 'retention')",
            {
                "s": w.site,
                "ref": subj.subject_ref(w.site, s.sub),
                "sid": s.session_id,
                "c": s.cmid,
            },
        )
        session = await _session_of(w, s)
        assert session["turns"] is None
        assert session["turns_unavailable"] == "transcript_purged"
    async with world(pg) as w:
        await _privacy_key(w, "assess:privacy-export", "export")
        a, _ = await ready(w, worker)
        session = await _session_of(w, a)
        assert "turns_unavailable" not in session and "turns" not in session


async def test_pages_follow_the_cursor(pg, worker) -> None:  # type: ignore[no-untyped-def]
    from vektra_assess.api.privacy import EXPORT_PAGE

    async with world(pg) as w:
        await _privacy_key(w, "assess:privacy-export", "export")
        user = 880_001
        cmids = [771_000 + i for i in range(EXPORT_PAGE + 2)]
        for cmid in cmids:
            await _extra_only(pg, w.site, cmid, user)
        first = await _get(w, user)
        assert first.status_code == 200, first.text
        page = first.json()
        assert [i["cmid"] for i in page["items"]] == cmids[:EXPORT_PAGE]
        assert page["next_cursor"] == str(cmids[EXPORT_PAGE - 1])
        second = (await _get(w, user, cursor=page["next_cursor"])).json()
        assert [i["cmid"] for i in second["items"]] == cmids[EXPORT_PAGE:]
        assert second["next_cursor"] is None
        audited = await pg.run(
            "super",
            "SELECT count(*) FROM assess_audit_events WHERE event = "
            "'assess_privacy_export' AND site_id = :s AND data->>'kind' = 'activities'",
            {"s": w.site},
        )
        assert audited[0][0] == 2


async def test_the_transcript_export_audit_names_its_kind(pg, worker) -> None:  # type: ignore[no-untyped-def]
    """Finding 9: both exports' audit rows carry ``kind``."""
    async with world(pg) as w:
        await _privacy_key(w, "assess:privacy-export", "export")
        r = await w.client.get(
            "/api/v1/assess/privacy/subjects/880001/transcripts", headers=auth("export")
        )
        assert r.status_code == 200, r.text
        rows = await pg.run(
            "super",
            "SELECT data->>'kind' FROM assess_audit_events WHERE event = "
            "'assess_privacy_export' AND site_id = :s",
            {"s": w.site},
        )
        assert [x[0] for x in rows] == ["transcripts"]


async def test_an_unknown_subject_is_an_empty_page(pg, worker) -> None:  # type: ignore[no-untyped-def]
    async with world(pg) as w:
        await _privacy_key(w, "assess:privacy-export", "export")
        r = await _get(w, 880_999)
        assert r.status_code == 200, r.text
        assert r.json() == {"items": [], "next_cursor": None}


@pytest.mark.parametrize(
    ("user", "cursor"),
    [("abc", None), ("0", None), ("-1", None), ("880001", "x"), ("880001", "0")],
)
async def test_an_invalid_userid_or_cursor_is_a_400(pg, worker, user, cursor) -> None:  # type: ignore[no-untyped-def]
    async with world(pg) as w:
        await _privacy_key(w, "assess:privacy-export", "export")
        r = await _get(w, user, cursor=cursor)
        assert r.status_code == 400, r.text
        assert err(r)["code"] == "ERR-ASSESS-014"


async def test_only_a_site_bound_export_key_lists(pg, worker) -> None:  # type: ignore[no-untyped-def]
    async with world(pg) as w:
        await _privacy_key(w, "assess:privacy-export", "export")
        await _privacy_key(w, "assess:privacy-erase", "erase")
        info = await make_key(pg, ["assess:privacy-export"], None)  # bound to no site
        w.app.state.registry.get("key_store", "default").table["unbound"] = info
        for key in ("admin", "mint", "teacher", "erase"):
            r = await _get(w, 880_001, key=key)
            assert r.status_code == 403, (key, r.text)
        r = await _get(w, 880_001, key="unbound")
        assert r.status_code == 403 and err(r)["code"] == "ERR-ASSESS-007", r.text


async def test_a_site_a_key_never_sees_site_b_activities(pg, worker) -> None:  # type: ignore[no-untyped-def]
    """The cross-site negative: the same user id has rows in both sites; each site's key lists
    its own site's activities only."""
    async with world(pg) as w:
        await _privacy_key(w, "assess:privacy-export", "export")
        await _privacy_key(w, "assess:privacy-export", "export_b", site=w.other_site)
        user = 880_002
        await _extra_only(pg, w.site, 772_001, user)
        await _extra_only(pg, w.other_site, 772_002, user)
        await _tombstone_only(pg, w.other_site, 772_003, user)
        mine = (await _get(w, user)).json()
        theirs = (await _get(w, user, key="export_b")).json()
        assert [i["cmid"] for i in mine["items"]] == [772_001]
        assert [i["cmid"] for i in theirs["items"]] == [772_002, 772_003]


async def test_the_listing_is_rate_limited_per_key(pg, worker) -> None:  # type: ignore[no-untyped-def]
    async with world(pg, default_settings(VEKTRA_ASSESS_PRIVACY_RPM=2)) as w:
        await _privacy_key(w, "assess:privacy-export", "export")
        codes = [(await _get(w, 880_001)).status_code for _ in range(3)]
        assert codes == [200, 200, 429]
