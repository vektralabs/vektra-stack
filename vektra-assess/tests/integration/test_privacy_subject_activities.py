"""``GET /api/v1/assess/privacy/subjects/{userid}/activities`` (plugin request, CodeRabbit on the
plugin's PR #9): every activity of the key's site where the subject has ANY row the erasure would
delete, with their sessions, resets and extra time, so the LMS's export and its erasure approval
see what ``DELETE /privacy/subjects/{userid}`` erases.

The set is derived from ``privacy.subject.SUBJECT_TABLES``, the one list the erasure deletes and
counts and ``activities/{cmid}/subjects`` reads: the drift test below cross-checks the three.
"""

from __future__ import annotations

import uuid
from typing import Any

import pytest
from sqlalchemy.ext.asyncio import create_async_engine
from support.sessions import seed_session
from support.world import auth, default_settings, err, make_key, world

from integration.test_privacy_retention import _privacy_key, erase, ready
from vektra_assess.privacy.subject import SUBJECT_TABLES

pytestmark = pytest.mark.integration


@pytest.fixture
async def worker(pg):  # type: ignore[no-untyped-def]
    from integration.test_session_preflight import _reset

    await _reset(pg)
    engine = create_async_engine(pg.url("worker"))
    yield engine
    await engine.dispose()


URL = "/api/v1/assess/privacy/subjects/{u}/activities"


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


async def _cmids_with_rows(pg, site: uuid.UUID, user: int) -> set[int]:  # type: ignore[no-untyped-def]
    """Independently of the route: every cmid with a row of the subject in any subject table."""
    out: set[int] = set()
    for table in SUBJECT_TABLES:
        rows = await pg.run(
            "super",
            f"SELECT DISTINCT cmid FROM {table} WHERE site_id = :s AND student_userid = :u",
            {"s": site, "u": user},
        )
        out |= {int(r[0]) for r in rows}
    return out


async def test_listing_export_and_erasure_cover_the_same_activities(pg, worker) -> None:  # type: ignore[no-untyped-def]
    """The drift cross-check: a transcript in A, only a live session in B, only a reset in C,
    only extra time in D. All four are listed, each lists the subject, and erasing the subject
    deletes exactly the listed activities' rows (and nothing of another subject)."""
    async with world(pg, default_settings(VEKTRA_ASSESS_ERASURE_WAIT_S=10)) as w:
        await _privacy_key(w, "assess:privacy-export", "export")
        await _privacy_key(w, "assess:privacy-erase", "erase")
        a, _ = await ready(w, worker, assignment="rubric_vis")
        b = await seed_session(w, worker, "ACTIVE", assignment="simple")
        assert b.sub == a.sub and b.cmid != a.cmid
        user = a.sub
        c, d = 770_001, 770_002
        await _reset_only(pg, w.site, c, user)
        await _extra_only(pg, w.site, d, user, 900)
        # another subject in C: never listed for ours, untouched by the erasure
        await _extra_only(pg, w.site, c, 990_123)

        r = await _get(w, user)
        assert r.status_code == 200, r.text
        assert r.headers["cache-control"] == "no-store"
        body = r.json()
        assert body["next_cursor"] is None
        listed = {i["cmid"]: i for i in body["items"]}
        assert set(listed) == {a.cmid, b.cmid, c, d}
        assert set(listed) == await _cmids_with_rows(pg, w.site, user)
        assert [i["cmid"] for i in body["items"]] == sorted(listed)
        # the shape: ids, states, codes and timestamps, never free text
        assert [s["state"] for s in listed[a.cmid]["sessions"]] == ["CLOSED"]
        assert listed[a.cmid]["sessions"][0]["close_reason"] == "finished"
        assert listed[a.cmid]["sessions"][0]["session_no"] == 1
        assert listed[a.cmid]["sessions"][0]["closed_at"] is not None
        assert listed[a.cmid]["course_id"] == 7 or listed[a.cmid]["course_id"] is None
        assert [s["state"] for s in listed[b.cmid]["sessions"]] == ["ACTIVE"]
        assert listed[b.cmid]["sessions"][0]["closed_at"] is None
        assert listed[c] == {
            "cmid": c,
            "course_id": None,
            "sessions": [],
            "resets": [
                {
                    "after_session_no": 1,
                    "granted_at": listed[c]["resets"][0]["granted_at"],
                }
            ],
            "extra_seconds": 0,
        }
        assert listed[c]["resets"][0]["granted_at"]
        assert listed[d]["sessions"] == [] and listed[d]["resets"] == []
        assert listed[d]["extra_seconds"] == 900
        for item in body["items"]:
            assert set(item) == {
                "cmid",
                "course_id",
                "sessions",
                "resets",
                "extra_seconds",
            }
            for s in item["sessions"]:
                assert set(s) == {
                    "session_no",
                    "state",
                    "close_reason",
                    "created_at",
                    "closed_at",
                }
            for g in item["resets"]:
                assert set(g) == {"after_session_no", "granted_at"}

        # the activity listing includes the subject in every listed activity
        for cmid in listed:
            got = await w.client.get(
                f"/api/v1/assess/privacy/activities/{cmid}/subjects",
                headers=auth("export"),
            )
            assert got.status_code == 200, got.text
            assert user in got.json()["userids"], cmid

        # the erasure deletes exactly the listed activities' rows
        r = await erase(w, worker, f"/api/v1/assess/privacy/subjects/{user}")
        assert r.status_code == 200, r.text
        assert await _cmids_with_rows(pg, w.site, user) == set()
        after = await _get(w, user)
        assert after.json() == {"items": [], "next_cursor": None}
        assert await _cmids_with_rows(pg, w.site, 990_123) == {c}


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
        mine = (await _get(w, user)).json()
        theirs = (await _get(w, user, key="export_b")).json()
        assert [i["cmid"] for i in mine["items"]] == [772_001]
        assert [i["cmid"] for i in theirs["items"]] == [772_002]


async def test_the_listing_is_rate_limited_per_key(pg, worker) -> None:  # type: ignore[no-untyped-def]
    async with world(pg, default_settings(VEKTRA_ASSESS_PRIVACY_RPM=2)) as w:
        await _privacy_key(w, "assess:privacy-export", "export")
        codes = [(await _get(w, 880_001)).status_code for _ in range(3)]
        assert codes == [200, 200, 429]
