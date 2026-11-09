"""The grader session list without a database (plan assess-teacher-ops 11b, REQ-076).

``tests/integration/test_teacher_sessions_api.py`` runs the route on real Postgres; these pin
the view it builds from the rows: per-student grouping and paging, deliveries without an address,
reasons decrypted per row (one unreadable row never takes the list down), the gate panel and the
header-only inputs.
"""

from __future__ import annotations

import asyncio
import json
import os
import uuid
from contextlib import asynccontextmanager
from datetime import UTC, datetime
from types import SimpleNamespace
from typing import Any

import pytest
from fastapi import HTTPException
from starlette.datastructures import Headers

from vektra_assess.api import teacher_sessions as ts
from vektra_assess.api.teachers import TeacherCheck, TeacherGate
from vektra_assess.crypto.teacher_reason import encrypt_reason
from vektra_assess.keys import AssessKeyring

SITE = uuid.uuid4()
KEK = AssessKeyring("assess_transcript_kek", "t1", {"t1": os.urandom(32)})
OLD = AssessKeyring("assess_transcript_kek", "t0", {"t0": os.urandom(32)})
T0 = datetime(2027, 1, 15, 10, 0, tzinfo=UTC)
HEADERS = {
    "X-Assess-Actor": "5",
    "X-Assess-Course": "7",
    "X-Assess-Activity-Type": "quiz",
}


class Result:
    def __init__(self, rows: list[Any] | None = None, scalar: Any = None) -> None:
        self.rows = rows or []
        self._scalar = scalar

    def all(self) -> list[Any]:
        return list(self.rows)

    def scalar(self) -> Any:
        return self._scalar


class Conn:
    def __init__(self, script: dict[str, Any]) -> None:
        self.script = script
        self.sql: list[tuple[str, Any]] = []

    async def execute(self, stmt: Any, params: Any = None) -> Result:
        sql = str(stmt)
        self.sql.append((sql, params))
        for needle, answer in self.script.items():
            if needle in sql:
                return answer
        return Result()


@pytest.fixture
def world(monkeypatch: pytest.MonkeyPatch) -> Any:
    holder: dict[str, Any] = {"check": TeacherCheck.OK, "facts": {}, "readable": {}}

    @asynccontextmanager
    async def begin() -> Any:
        yield holder["conn"]

    async def verify_teacher(conn: Any, **kw: Any) -> tuple[TeacherGate, Any]:
        holder["verify"] = kw
        if holder["check"] is not TeacherCheck.OK:
            return TeacherGate(holder["check"]), HTTPException(403, detail="unverified")
        return TeacherGate(TeacherCheck.OK, holder["facts"]), None

    async def site_of_key(conn: Any, key: Any, purpose: str) -> Any:
        return SITE

    async def readable_close_flags(conn: Any, site: Any) -> dict[str, bool]:
        return holder["readable"]

    monkeypatch.setattr(ts, "engine_of", lambda r: SimpleNamespace(begin=begin))
    monkeypatch.setattr(
        ts, "settings_of", lambda r: SimpleNamespace(teacher_gate_max_age_s=120)
    )
    monkeypatch.setattr(ts, "transcript_key_of", lambda r: KEK)
    monkeypatch.setattr(ts, "site_of_key", site_of_key)
    monkeypatch.setattr(ts, "verify_teacher", verify_teacher)
    monkeypatch.setattr(ts, "readable_close_flags", readable_close_flags)

    def make(script: dict[str, Any], **kw: Any) -> dict[str, Any]:
        holder["conn"] = Conn(script)
        holder.update(kw)
        return holder

    return make


def call(**headers: str) -> Any:
    request = SimpleNamespace(headers=Headers({**HEADERS, **headers}))
    return ts.list_sessions("3", request, SimpleNamespace())  # type: ignore[arg-type]


def view(**headers: str) -> dict[str, Any]:
    resp = asyncio.run(call(**headers))
    assert resp.headers["cache-control"] == "no-store"
    return json.loads(resp.body)


def refused(**headers: str) -> tuple[int, Any]:
    with pytest.raises(HTTPException) as exc:
        asyncio.run(call(**headers))
    return exc.value.status_code, exc.value.detail


def session(
    user: int, no: int, *, transcript: Any = None, state: str = "CLOSED"
) -> Any:
    return SimpleNamespace(
        id=uuid.uuid4(), student_userid=user, session_no=no, state=state,
        close_reason="timeout" if state == "CLOSED" else None, committed_at=T0,
        closed_at=T0 if state == "CLOSED" else None, transcript_id=transcript,
    )  # fmt: skip


def reset_row(
    user: int, no: int, kek: AssessKeyring = KEK, reason: str = "rete"
) -> Any:
    rid = uuid.uuid4()
    ct, _ = encrypt_reason(kek, "assess_reset_grants", rid, reason)
    return SimpleNamespace(
        id=rid, student_userid=user, session_no=no, reason_ct=memoryview(ct),
        include_previous_turns=False, granted_by="5", granted_at=T0,
    )  # fmt: skip


def students(*ids: int) -> Result:
    return Result([SimpleNamespace(u=u) for u in ids])


def test_the_list_groups_sessions_deliveries_resets_and_extra_time_per_student(
    world: Any,
) -> None:
    tid = uuid.uuid4()
    delivery = SimpleNamespace(
        transcript_id=tid, round=1, recipient_kind="teacher", status="SENT", attempts=1,
        smtp_code=250, last_error_class=None, sent_at=T0,
    )  # fmt: skip
    term_id = uuid.uuid4()
    ct, _ = encrypt_reason(KEK, "assess_terminations", term_id, "esame annullato")
    term = SimpleNamespace(
        id=term_id, reason_ct=ct, closed_count=2, rolled_back_count=1, also_disable=True,
        created_by="5", created_at=T0,
    )  # fmt: skip
    world(
        {
            "AS t WHERE u > :after": students(41, 42),
            "LEFT JOIN assess_transcripts": Result(
                [session(41, 1, transcript=tid), session(41, 2, state="ACTIVE")]
            ),
            "FROM assess_deliveries": Result([delivery]),
            "FROM assess_reset_grants g": Result([reset_row(41, 1)]),
            "extra_seconds FROM assess_extra_time": Result(
                [SimpleNamespace(student_userid=42, extra_seconds=600)]
            ),
            "FROM assess_terminations": Result([term]),
            "attested_opening_at": Result(scalar=None),
            "count(*) FROM assess_sessions": Result(scalar=1),
        },
        facts={"activity_type": "quiz", "close_at": 1_800_000_000},
        readable={"quiz_overrides": True, "quiz_grace": True},
    )
    got = view()
    assert got["cmid"] == 3 and got["next_cursor"] is None
    first, second = got["students"]
    assert [s["session_no"] for s in first["sessions"]] == [1, 2]
    (d,) = first["sessions"][0]["deliveries"]
    assert d == {
        "round": 1, "recipient_kind": "teacher", "status": "SENT", "attempts": 1,
        "smtp_code": 250, "error_class": None, "sent_at": "2027-01-15T10:00:00+00:00",
    }  # fmt: skip
    assert first["sessions"][1]["deliveries"] == []
    (r,) = first["resets"]
    assert (r["reason"], r["reason_state"], r["after_session_no"]) == (
        "rete",
        "readable",
        1,
    )
    assert r["student_notice"] == ts.NOT_NOTIFIED and first["reset_count"] == 1
    assert second == {
        "userid": 42,
        "sessions": [],
        "resets": [],
        "extra_seconds": 600,
        "reset_count": 0,
    }
    (t,) = got["terminations"]
    assert (t["reason"], t["closed"], t["rolled_back"], t["also_disable"]) == (
        "esame annullato", 2, 1, True,
    )  # fmt: skip
    assert got["gate"]["active_sessions"] == 1
    assert got["gate"]["fallback_reason"] is None
    assert "override_holder" not in got["gate"]


def test_one_unreadable_reason_does_not_take_the_list_down(world: Any) -> None:
    world(
        {
            "AS t WHERE u > :after": students(41),
            "FROM assess_reset_grants g": Result(
                [reset_row(41, 1, kek=OLD), reset_row(41, 2, reason="ok")]
            ),
        }
    )
    (student,) = view()["students"]
    bad, good = student["resets"]
    assert (bad["reason"], bad["reason_state"]) == (None, "unreadable")
    assert (good["reason"], good["reason_state"]) == ("ok", "readable")


def test_a_full_page_returns_a_cursor_and_a_later_page_omits_terminations(
    world: Any,
) -> None:
    ids = list(range(100, 100 + ts.PAGE_SIZE + 1))
    w = world({"AS t WHERE u > :after": students(*ids)})
    got = view(**{"X-Assess-Cursor": "99"})
    assert len(got["students"]) == ts.PAGE_SIZE
    assert got["next_cursor"] == ids[ts.PAGE_SIZE - 1]
    assert got["terminations"] == []
    page = [p for sql, p in w["conn"].sql if "AS t WHERE u > :after" in sql]
    assert page[0]["after"] == 99 and page[0]["n"] == ts.PAGE_SIZE + 1
    assert not any("FROM assess_terminations" in sql for sql, _ in w["conn"].sql)


def test_an_empty_activity_lists_no_students(world: Any) -> None:
    w = world({"attested_opening_at": Result(scalar=1_800_000_900)})
    got = view()
    assert got["students"] == [] and got["next_cursor"] is None
    assert not any("LEFT JOIN assess_transcripts" in sql for sql, _ in w["conn"].sql)


def test_the_gate_panel_shows_the_fallback_and_the_holder_only_to_a_viewer(
    world: Any,
) -> None:
    facts = {
        "activity_type": "quiz", "close_at": 1_800_000_000, "override_holder": 77,
        "override_viewers": [5],
    }  # fmt: skip
    world({}, facts=facts, readable={"quiz_overrides": False, "quiz_grace": True})
    gate = view()["gate"]
    assert gate["fallback_reason"] == ts.FALLBACK_REASON
    assert gate["fallback_date_missing"] is True
    assert gate["override_holder"] == 77
    world({}, facts={**facts, "override_viewers": [6]}, readable={})
    assert "override_holder" not in view()["gate"]


@pytest.mark.parametrize(
    ("headers", "field"),
    [
        ({"X-Assess-Actor": "abc"}, "x_assess_actor"),
        ({"X-Assess-Cursor": "-1"}, "cursor"),
    ],
)
def test_bad_headers_are_a_bad_request(
    world: Any, headers: dict[str, str], field: str
) -> None:
    w = world({})
    status, detail = refused(**headers)
    assert status == 400 and detail["error"]["code"] == "ERR-ASSESS-014"
    assert field in json.dumps(detail)
    assert w["conn"].sql == []


def test_an_unverified_grader_gets_no_rows(world: Any) -> None:
    w = world({"AS t WHERE u > :after": students(41)}, check=TeacherCheck.NOT_A_GRADER)
    status, _ = refused()
    assert status == 403
    assert w["conn"].sql == []
    assert w["verify"]["role"] == "grader" and w["verify"]["route"] == "list_sessions"
