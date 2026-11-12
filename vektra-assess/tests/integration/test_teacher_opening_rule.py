"""One opening decision for every teacher route (code review of PR #208).

The config view, the session list's gate panel, the end-date validation of ``PUT config``, its
warning, and ``PUT opening-fallback`` all decide through ``gates.decide_opening`` on the same
inputs (the worker's teacher facts merged with the capability matrix). Each test pins one place
that used to compute its own answer.
"""

from __future__ import annotations

from datetime import UTC, datetime
from typing import Any

import pytest
from support.teacher_ops import (
    COURSE,
    GRADER,
    list_headers,
    seed_teacher_gate,
    teacher,
    with_transcript_key,
)
from support.world import configure_activity, err, world

from integration.test_teacher_extra_time import caps, fallback_url, fb

pytestmark = pytest.mark.integration

CLOSE = 1_900_000_000
GRACE = 600
REL = "4.5 (Build: 20250101)"


def at(epoch: int) -> str:
    return datetime.fromtimestamp(epoch, UTC).isoformat()


def headers(kind: str) -> dict[str, str]:
    return {**list_headers(), "X-Assess-Activity-Type": kind}


async def views(w: Any, cmid: int, kind: str) -> tuple[dict, dict]:  # type: ignore[type-arg]
    config = await w.client.get(
        f"/api/v1/assess/activities/{cmid}/config", headers=headers(kind)
    )
    panel = await w.client.get(
        f"/api/v1/assess/activities/{cmid}/sessions", headers=headers(kind)
    )
    assert config.status_code == panel.status_code == 200, (config.text, panel.text)
    return config.json(), panel.json()["gate"]


async def put_end(w: Any, cmid: int, kind: str, end: int) -> Any:
    version = (await views(w, cmid, kind))[0]["version"]
    return await w.client.put(
        f"/api/v1/assess/activities/{cmid}/config",
        json={
            "actor_userid": GRADER,
            "courseid": COURSE,
            "activity_type": kind,
            "expected_version": version,
            "available_until": at(end),
        },
        headers=teacher(),
    )


async def attest(pg: Any, site: Any, cmid: int, epoch: int) -> None:
    await pg.run(
        "super",
        "UPDATE assess_activity_config SET attested_opening_at = to_timestamp(:t) "
        "WHERE site_id = :s AND cmid = :c",
        {"s": site, "c": cmid, "t": epoch},
    )


async def test_the_end_date_floor_is_the_opening_the_view_shows(pg) -> None:  # type: ignore[no-untyped-def]
    """Findings 1, 2, 8: a quiz on the REQ-091 path with a teacher date inside the grace
    opens at close + grace (the view says so); an end date before that is refused, and a
    stored one is warned about, against the same date."""
    async with world(pg) as w:
        with_transcript_key(w)
        await configure_activity(
            pg, w.site, 920, kind="quiz", attested=False, enabled=False
        )
        await attest(pg, w.site, 920, CLOSE + 60)
        await seed_teacher_gate(
            pg, w.site, 920, kind="quiz", close_at=CLOSE, automatic_close_at=None,
            grace_s=GRACE, editors=[GRADER],
        )  # fmt: skip
        config, _ = await views(w, 920, "quiz")
        assert config["opening"]["at"] == at(CLOSE + GRACE)
        r = await put_end(
            w, 920, "quiz", CLOSE + 300
        )  # after the date, inside the grace
        assert r.status_code == 400, r.text
        assert err(r)["details"]["fields"] == [
            {
                "field": "available_until",
                "code": "not_after_opening",
                "opening_at": CLOSE + GRACE,
            }
        ]
        # a stored end date inside the grace is warned about
        await pg.run(
            "super",
            "UPDATE assess_activity_config SET end_at = to_timestamp(:t) "
            "WHERE site_id = :s AND cmid = 920",
            {"s": w.site, "t": CLOSE + 300},
        )
        config, _ = await views(w, 920, "quiz")
        assert config["warnings"]["available_until_not_after_opening"] is True
        r = await put_end(w, 920, "quiz", CLOSE + GRACE + 1)
        assert r.status_code == 200, r.text


async def test_while_a_date_is_required_the_floor_is_close_plus_grace(pg) -> None:  # type: ignore[no-untyped-def]
    """The opening is undecidable until the teacher sets a date, but it cannot be earlier
    than the close plus the grace: that is the floor."""
    async with world(pg) as w:
        with_transcript_key(w)
        await configure_activity(
            pg, w.site, 921, kind="quiz", attested=False, enabled=False
        )
        await seed_teacher_gate(
            pg, w.site, 921, kind="quiz", close_at=CLOSE, automatic_close_at=None,
            grace_s=GRACE, editors=[GRADER],
        )  # fmt: skip
        r = await put_end(w, 921, "quiz", CLOSE + GRACE)
        assert r.status_code == 400, r.text
        assert err(r)["details"]["fields"][0]["code"] == "not_after_opening"
        assert (await put_end(w, 921, "quiz", CLOSE + GRACE + 1)).status_code == 200


async def test_no_close_date_has_no_floor_even_with_a_stale_teacher_date(pg) -> None:  # type: ignore[no-untyped-def]
    """Finding 6: BR-007, a teacher date never replaces a missing close: the gate refuses
    no_close_date, so the end date is refused opening_unknown, not checked against the date."""
    async with world(pg) as w:
        with_transcript_key(w)
        await configure_activity(pg, w.site, 922, attested=False, enabled=False)
        await attest(pg, w.site, 922, CLOSE)
        await seed_teacher_gate(pg, w.site, 922, close_at=0, editors=[GRADER])
        r = await put_end(w, 922, "assign", CLOSE + 3600)
        assert r.status_code == 400, r.text
        assert err(r)["details"]["fields"] == [
            {"field": "available_until", "code": "opening_unknown"}
        ]


async def test_an_unreadable_close_reads_unreadable_never_unknown(pg) -> None:  # type: ignore[no-untyped-def]
    """Finding 5: the activity's own close unreadable and no date: /state says
    close_unreadable, the views say ``unreadable`` (a date cannot help)."""
    async with world(pg) as w:
        with_transcript_key(w)
        await configure_activity(pg, w.site, 923, attested=False)
        await seed_teacher_gate(pg, w.site, 923, close_at=None, editors=[GRADER])
        config, panel = await views(w, 923, "assign")
        expected = {"at": None, "source": "unreadable", "fallback_required": False}
        assert config["opening"] == panel["opening"] == expected
        assert panel["fallback_date_missing"] is False


async def test_facts_unreadable_with_a_readable_matrix_accepts_the_date(pg) -> None:  # type: ignore[no-untyped-def]
    """Finding 3: the matrix says a quiz's close inputs are readable, but the last facts read
    of the overrides failed (no automatic close): Start refuses close_unreadable, the view says
    a date is required, and the PUT accepts it (never ``fallback_not_needed``)."""
    async with world(pg) as w:
        with_transcript_key(w)
        await caps(pg, w.site, REL, quiz_overrides="readable", quiz_grace="readable")
        await configure_activity(pg, w.site, 924, kind="quiz", attested=False)
        await seed_teacher_gate(
            pg, w.site, 924, kind="quiz", close_at=CLOSE, automatic_close_at=None,
            grace_s=0, editors=[GRADER],
        )  # fmt: skip
        config, panel = await views(w, 924, "quiz")
        assert config["opening"] == panel["opening"]
        assert config["opening"]["fallback_required"] is True
        assert panel["fallback_date_missing"] is True
        assert panel["fallback_reason"] is not None
        r = await w.client.put(
            fallback_url(924), json=fb(CLOSE, activity_type="quiz"), headers=teacher()
        )
        assert r.status_code == 200, r.text
        config, panel = await views(w, 924, "quiz")
        assert config["opening"] == panel["opening"]
        assert config["opening"]["source"] == "attested"
        assert panel["fallback_date_missing"] is False


async def test_a_matrix_unreadable_input_requires_the_date_in_every_view(pg) -> None:  # type: ignore[no-untyped-def]
    """Finding 4: the matrix says a quiz's grace is partial (the fallback applies, the PUT
    accepts a date) while the last facts read gave an automatic close: the view must not say
    ``activity_close`` / no date required while the panel says the date is missing."""
    async with world(pg) as w:
        with_transcript_key(w)
        await caps(pg, w.site, REL, quiz_overrides="readable", quiz_grace="partial")
        await configure_activity(pg, w.site, 925, kind="quiz", attested=False)
        await seed_teacher_gate(
            pg, w.site, 925, kind="quiz", close_at=CLOSE,
            automatic_close_at=CLOSE + 900, grace_s=0, editors=[GRADER],
        )  # fmt: skip
        config, panel = await views(w, 925, "quiz")
        expected = {"at": None, "source": "unreadable", "fallback_required": True}
        assert config["opening"] == panel["opening"] == expected
        assert panel["fallback_date_missing"] is True
        r = await w.client.put(
            fallback_url(925), json=fb(CLOSE, activity_type="quiz"), headers=teacher()
        )
        assert r.status_code == 200, r.text
        config, panel = await views(w, 925, "quiz")
        assert config["opening"] == panel["opening"]
        assert config["opening"]["fallback_required"] is False
        assert panel["fallback_date_missing"] is False


async def test_every_input_readable_refuses_the_date_and_requires_none(pg) -> None:  # type: ignore[no-untyped-def]
    """The other side: matrix and facts readable, no date needed, the PUT refuses
    ``fallback_not_needed`` and no view asks for one."""
    async with world(pg) as w:
        with_transcript_key(w)
        await caps(pg, w.site, REL, quiz_overrides="readable", quiz_grace="readable")
        await configure_activity(pg, w.site, 926, kind="quiz", attested=False)
        await seed_teacher_gate(
            pg, w.site, 926, kind="quiz", close_at=CLOSE,
            automatic_close_at=CLOSE + 900, grace_s=0, editors=[GRADER],
        )  # fmt: skip
        config, panel = await views(w, 926, "quiz")
        expected = {
            "at": at(CLOSE + 900),
            "source": "activity_close",
            "fallback_required": False,
        }
        assert config["opening"] == panel["opening"] == expected
        assert panel["fallback_date_missing"] is False
        assert panel["fallback_reason"] is None
        r = await w.client.put(
            fallback_url(926), json=fb(CLOSE, activity_type="quiz"), headers=teacher()
        )
        assert r.status_code == 400, r.text
        assert err(r)["details"]["fields"] == [
            {"field": "opening_at", "code": "fallback_not_needed"}
        ]
