"""The teacher operations without a database (plan assess-teacher-ops 11b).

The integration tests (``tests/integration/test_teacher_reset.py``, ``..._terminate.py``,
``..._extra_time.py``) run the routes against real Postgres with the restricted roles and the
real locks. These pin each route's decision order with a scripted connection: which refusal wins,
that a refusal writes nothing, what a success writes and returns.
"""

from __future__ import annotations

import asyncio
import json
import os
import uuid
from contextlib import asynccontextmanager
from datetime import UTC, datetime, timedelta
from types import SimpleNamespace
from typing import Any

import pytest
from fastapi import HTTPException

from vektra_assess.api import state as state_api
from vektra_assess.api import teacher_ops as ops
from vektra_assess.api.teachers import TeacherCheck, TeacherGate
from vektra_assess.audit import AssessAuditEvent
from vektra_assess.crypto.teacher_reason import decrypt_reason
from vektra_assess.keys import AssessKeyring
from vektra_assess.readable import Readable
from vektra_assess.teacher.schemas import (
    ExtraTimePut,
    OpeningFallbackPut,
    ResetPost,
    TerminatePost,
)

SITE = uuid.uuid4()
NOW = 1_800_000_000
KEK = AssessKeyring("assess_transcript_kek", "t1", {"t1": os.urandom(32)})
BASE = {"actor_userid": 5, "courseid": 7, "activity_type": "quiz"}


class Result:
    def __init__(self, rows: list[Any] | None = None, scalar: Any = None) -> None:
        self.rows = rows or []
        self._scalar = scalar

    def first(self) -> Any:
        return self.rows[0] if self.rows else None

    def all(self) -> list[Any]:
        return list(self.rows)

    def scalar(self) -> Any:
        return self._scalar

    def scalar_one(self) -> Any:
        return self._scalar


class Conn:
    """Answers each statement from ``script`` (first matching substring wins) and records it."""

    def __init__(self, script: dict[str, Any]) -> None:
        self.script = script
        self.sql: list[tuple[str, Any]] = []

    async def execute(self, stmt: Any, params: Any = None) -> Result:
        sql = str(stmt)
        self.sql.append((sql, params))
        for needle, answer in self.script.items():
            if needle in sql:
                return answer(params) if callable(answer) else answer
        return Result()

    def ran(self, needle: str) -> list[Any]:
        return [p for sql, p in self.sql if needle in sql]


class World:
    def __init__(self, conn: Conn, *, check: TeacherCheck = TeacherCheck.OK) -> None:
        self.conn = conn
        self.check = check
        self.events: list[tuple[Any, dict[str, Any]]] = []
        self.renders: list[tuple[Any, ...]] = []
        self.privacy = True
        self.readable: dict[str, bool] = {}
        self.facts: dict[str, Any] = {"activity_type": "quiz", "close_at": NOW - 3600}


@pytest.fixture
def world(monkeypatch: pytest.MonkeyPatch) -> Any:
    holder: dict[str, World] = {}

    def make(script: dict[str, Any], **kw: Any) -> World:
        w = World(Conn(script), **kw)
        holder["w"] = w
        return w

    @asynccontextmanager
    async def begin() -> Any:
        yield holder["w"].conn

    async def verify_teacher(conn: Any, **kw: Any) -> tuple[TeacherGate, Any]:
        w = holder["w"]
        if w.check is not TeacherCheck.OK:
            return TeacherGate(w.check), HTTPException(403, detail="unverified")
        return TeacherGate(TeacherCheck.OK, dict(w.facts)), None

    async def site_of_key(conn: Any, key: Any, purpose: str) -> Any:
        assert purpose == "teacher"
        return SITE

    async def write_event(conn: Any, event: Any, **kw: Any) -> None:
        holder["w"].events.append((event, kw))

    async def queue_render_check(conn: Any, *args: Any) -> None:
        holder["w"].renders.append(args)

    async def is_configured(conn: Any, site: Any) -> bool:
        return holder["w"].privacy

    async def capability(conn: Any, site: Any, flag: str) -> Any:
        return Readable(True) if holder["w"].readable.get(flag) else None

    settings = SimpleNamespace(
        teacher_gate_max_age_s=120,
        privacy_allow_unset=False,
        env="production",
        is_development=False,
    )
    monkeypatch.setattr(ops, "engine_of", lambda r: SimpleNamespace(begin=begin))
    monkeypatch.setattr(ops, "settings_of", lambda r: settings)
    monkeypatch.setattr(ops, "transcript_key_of", lambda r: KEK)
    monkeypatch.setattr(ops, "site_of_key", site_of_key)
    monkeypatch.setattr(ops, "verify_teacher", verify_teacher)
    monkeypatch.setattr(ops, "write_event", write_event)
    monkeypatch.setattr(ops.privacy, "is_configured", is_configured)
    monkeypatch.setattr(ops, "capability", capability)
    monkeypatch.setattr(state_api, "queue_render_check", queue_render_check)
    return make


def body(monkeypatch: pytest.MonkeyPatch, model: Any, **fields: Any) -> None:
    parsed = model.model_validate({**BASE, **fields})

    async def parse_body(request: Any, m: Any) -> Any:
        assert m is model
        return parsed

    monkeypatch.setattr(ops, "parse_body", parse_body)


def run(coro: Any) -> tuple[int, dict[str, Any]]:
    resp = asyncio.run(coro)
    assert resp.headers["cache-control"] == "no-store"
    return resp.status_code, json.loads(resp.body)


def refused(coro: Any) -> tuple[int, Any]:
    with pytest.raises(HTTPException) as exc:
        asyncio.run(coro)
    return exc.value.status_code, exc.value.detail


def details(detail: Any) -> dict[str, Any]:
    return detail["error"].get("details", {})


def fields(detail: Any) -> list[tuple[str, str]]:
    return [(f["field"], f["code"]) for f in details(detail)["fields"]]


# --- reset ---------------------------------------------------------------------------------------

SID = uuid.uuid4()


def sess(
    sid: Any = SID, no: int = 1, state: str = "CLOSED", reason: str = "timeout"
) -> Any:
    return SimpleNamespace(
        id=sid, session_no=no, state=state, close_reason=reason, student_userid=42
    )


def cfg(**kw: Any) -> Any:
    base = dict(
        enabled=True,
        cap_minutes=30,
        version=3,
        end_at=None,
        attested_at=None,
    )
    return SimpleNamespace(**{**base, **kw})


def render(age: float = 1.0, **inputs: Any) -> Any:
    return SimpleNamespace(
        inputs={"moodle_reasons": [], "attested_at": None, **inputs}, age_s=age
    )


def reset_script(
    sessions: list[Any] | None = None,
    *,
    config: Any = "default",
    grants: int = 0,
    gate_row: Any = "default",
    inserted: bool = True,
    clock: int | None = None,
) -> dict[str, Any]:
    sessions = [sess()] if sessions is None else sessions
    return {
        "WHERE id = :sid": Result([sessions[-1]] if sessions else []),
        "FROM assess_activity_config": Result([] if config is None else [
            cfg() if config == "default" else config
        ]),
        "ORDER BY session_no FOR UPDATE": Result(sessions),
        "count(*) FROM assess_reset_grants": Result(scalar=grants),
        "FROM assess_gate_inputs": Result(
            [] if gate_row is None else [render() if gate_row == "default" else gate_row]
        ),
        "INSERT INTO assess_reset_grants": Result([SimpleNamespace(id=1)] if inserted else []),
        "clock_timestamp())::bigint AS db_now": Result(scalar=NOW if clock is None else clock),
    }  # fmt: skip


def reset(
    monkeypatch: pytest.MonkeyPatch, sid: str | None = None, **fields: Any
) -> Any:
    body(monkeypatch, ResetPost, reason="  Connessione persa  ", **fields)
    return ops.reset_session("3", sid or str(SID), SimpleNamespace(), SimpleNamespace())


def test_a_reset_grants_one_session_with_the_reason_encrypted_to_its_row(
    world: Any, monkeypatch: pytest.MonkeyPatch
) -> None:
    w = world(reset_script(sessions=[sess(reason="service_failure")]))
    status, view = run(reset(monkeypatch))
    assert status == 201
    assert view["student_userid"] == 42 and view["session_no_next"] == 2
    assert view["include_previous_turns"] is True  # default ON after a service failure
    (row,) = w.conn.ran("INSERT INTO assess_reset_grants")
    assert row["n"] == 1 and row["by"] == "5" and row["kid"] == "t1"
    # bound to its own row id, trimmed, and never stored in clear
    assert (
        decrypt_reason(KEK, "assess_reset_grants", row["id"], row["ct"])
        == "Connessione persa"
    )
    assert b"Connessione" not in row["ct"]
    ((event, kw),) = w.events
    assert event is AssessAuditEvent.TEACHER_RESET
    assert kw["data"]["reset_id"] == view["reset_id"]
    assert "reason" not in json.dumps(kw["data"], default=str)
    assert len(w.renders) == 1  # results page refreshed without waiting for the tick


def test_the_continuity_default_follows_the_locked_session_not_the_first_read(
    world: Any, monkeypatch: pytest.MonkeyPatch
) -> None:
    """CodeRabbit 4512009201 (PR #179): the worker closed the session for a service failure
    between the unlocked read and the lock; the default must follow the locked row."""
    script = reset_script(sessions=[sess(reason="service_failure")])
    script["WHERE id = :sid"] = Result([sess(state="ACTIVE", reason=None)])  # type: ignore[arg-type]
    w = world(script)
    status, view = run(reset(monkeypatch))
    assert status == 201
    assert view["include_previous_turns"] is True
    (row,) = w.conn.ran("INSERT INTO assess_reset_grants")
    assert row["prev"] is True


def test_the_end_date_is_checked_on_the_clock_after_the_locks(
    world: Any, monkeypatch: pytest.MonkeyPatch
) -> None:
    """CodeRabbit 4512009218 (PR #179): ``now()`` is the transaction start; a reset that waited
    past the end date for the activity lock must still be refused."""
    w = world(reset_script(config=cfg(end_at=NOW + 10), clock=NOW + 20))
    status, detail = refused(reset(monkeypatch))
    assert status == 409
    assert detail["error"]["details"]["gate"] == "end_date_passed"
    assert w.conn.ran("INSERT INTO assess_reset_grants") == []


def test_an_explicit_continuity_choice_is_kept(
    world: Any, monkeypatch: pytest.MonkeyPatch
) -> None:
    world(reset_script())
    _, view = run(reset(monkeypatch, include_previous_turns=True))
    assert view["include_previous_turns"] is True


def test_a_blank_reason_is_a_field_error_before_any_query(
    world: Any, monkeypatch: pytest.MonkeyPatch
) -> None:
    w = world(reset_script())
    body(monkeypatch, ResetPost, reason="   ")
    status, detail = refused(
        ops.reset_session("3", str(SID), SimpleNamespace(), SimpleNamespace())
    )
    assert status == 400 and fields(detail) == [("reason", "required")]
    assert w.conn.sql == []


def test_a_malformed_session_id_is_a_bad_request(
    world: Any, monkeypatch: pytest.MonkeyPatch
) -> None:
    world(reset_script())
    status, detail = refused(reset(monkeypatch, sid="not-a-uuid"))
    assert status == 400 and detail["error"]["code"] == "ERR-ASSESS-014"


def test_an_unverified_teacher_is_refused_before_the_action(
    world: Any, monkeypatch: pytest.MonkeyPatch
) -> None:
    w = world(reset_script(), check=TeacherCheck.STALE)
    status, _ = refused(reset(monkeypatch))
    assert status == 403
    assert w.conn.ran("INSERT INTO assess_reset_grants") == []


@pytest.mark.parametrize(
    ("script", "status", "reason"),
    [
        (reset_script(sessions=[]), 404, None),
        (reset_script(sessions=[sess(state="ACTIVE")]), 409, "not_closed"),
        (
            reset_script(sessions=[sess(), sess(sid=uuid.uuid4(), no=2)]),
            409,
            "not_latest",
        ),
        (reset_script(grants=1), 409, "already_reset"),
        (reset_script(inserted=False), 409, "already_reset"),
        # plan 12: the retention purge emptied the session (CodeRabbit 4516399820)
        (
            {**reset_script(), "FROM assess_purge_records": Result(scalar=1)},
            409,
            "purged",
        ),
    ],
)
def test_the_session_preconditions_refuse_and_write_nothing(
    world: Any,
    monkeypatch: pytest.MonkeyPatch,
    script: Any,
    status: int,
    reason: str | None,
) -> None:
    if reason == "not_latest":
        # the requested session is the first, the latest is another one
        script["WHERE id = :sid"] = Result([sess()])
    w = world(script)
    got, detail = refused(reset(monkeypatch))
    assert got == status
    if reason is not None:
        assert details(detail)["reason"] == reason
    assert w.events == []


def test_a_session_rolled_back_while_waiting_is_not_found(
    world: Any, monkeypatch: pytest.MonkeyPatch
) -> None:
    script = reset_script()
    script["ORDER BY session_no FOR UPDATE"] = Result([sess(sid=uuid.uuid4())])
    world(script)
    status, _ = refused(reset(monkeypatch))
    assert status == 404


@pytest.mark.parametrize(
    ("config", "privacy", "gate", "extra"),
    [
        (None, True, "not_enabled", {}),
        (cfg(enabled=False), True, "not_enabled", {}),
        (cfg(cap_minutes=None), True, "not_enabled", {}),
        (cfg(end_at=NOW - 1), True, "end_date_passed", {"field": "available_until"}),
        (cfg(), False, "privacy_not_configured", {}),
    ],
)
def test_the_database_gates_refuse_without_waiting_for_a_render(
    world: Any,
    monkeypatch: pytest.MonkeyPatch,
    config: Any,
    privacy: bool,
    gate: str,
    extra: dict[str, Any],
) -> None:
    w = world(reset_script(config=config))
    w.privacy = privacy
    status, detail = refused(reset(monkeypatch))
    assert status == 409
    assert details(detail) == {"reason": "gate", "gate": gate, **extra}
    assert w.renders == [] and w.conn.ran("FROM assess_gate_inputs") == []


@pytest.mark.parametrize(
    "gate_row",
    [
        None,
        render(age=121),
        render(age=None),
        SimpleNamespace(inputs="garbage", age_s=1.0),
        SimpleNamespace(inputs={"attested_at": None}, age_s=1.0),
        render(attested_at=NOW - 10),  # evaluated before the opening date moved
    ],
)
def test_a_stale_render_queues_a_refresh_and_asks_to_retry(
    world: Any, monkeypatch: pytest.MonkeyPatch, gate_row: Any
) -> None:
    w = world(reset_script(gate_row=gate_row))
    status, detail = refused(reset(monkeypatch))
    assert status == 409 and details(detail) == {"reason": "gate_inputs_stale"}
    assert len(w.renders) == 1
    assert w.conn.ran("INSERT INTO assess_reset_grants") == []


@pytest.mark.parametrize(
    ("reasons", "gate"), [(["not_enrolled"], "not_enrolled"), (["bogus"], "unreadable")]
)
def test_the_lms_half_of_the_render_refuses(
    world: Any, monkeypatch: pytest.MonkeyPatch, reasons: list[str], gate: str
) -> None:
    w = world(reset_script(gate_row=render(moodle_reasons=reasons)))
    status, detail = refused(reset(monkeypatch))
    assert status == 409 and details(detail)["gate"] == gate
    assert w.renders == []


# --- terminate -----------------------------------------------------------------------------------


def live(state: str, epoch: int = 1) -> Any:
    return SimpleNamespace(id=uuid.uuid4(), state=state, epoch=epoch)


def terminate_script(
    rows: list[Any], *, disabled: Any = 4, close_ok: Any = True
) -> dict[str, Any]:
    return {
        "state IN ('STARTING', 'ACTIVE', 'OUTAGE')": Result(rows),
        "assess_close_session": Result(scalar=close_ok),
        "assess_rollback_starting": Result(scalar=True),
        "SET enabled = false": Result(scalar=disabled),
    }


def terminate(monkeypatch: pytest.MonkeyPatch, **fields: Any) -> Any:
    body(monkeypatch, TerminatePost, reason="Prova annullata", **fields)
    return ops.terminate_sessions("3", SimpleNamespace(), SimpleNamespace())


def test_terminate_closes_live_rolls_back_starting_and_disables(
    world: Any, monkeypatch: pytest.MonkeyPatch
) -> None:
    rows = [live("ACTIVE"), live("OUTAGE", 2), live("STARTING")]
    w = world(terminate_script(rows))
    status, view = run(
        terminate(monkeypatch, expected_active_count=2, also_disable=True)
    )
    assert status == 200
    assert (view["closed"], view["rolled_back"], view["disabled"]) == (2, 1, True)
    assert [p["epoch"] for p in w.conn.ran("assess_close_session")] == [1, 2]
    (row,) = w.conn.ran("INSERT INTO assess_terminations")
    assert row["dis"] is True and (row["closed"], row["rolled"]) == (2, 1)
    assert (
        decrypt_reason(KEK, "assess_terminations", row["id"], row["ct"])
        == "Prova annullata"
    )
    kinds = [kw["reason_code"] for _, kw in w.events]
    assert kinds == ["enabled", "terminate"]


def test_terminate_records_only_the_disable_that_happened(
    world: Any, monkeypatch: pytest.MonkeyPatch
) -> None:
    w = world(terminate_script([], disabled=None))  # already disabled: no row switched
    _, view = run(terminate(monkeypatch, expected_active_count=0, also_disable=True))
    assert view == {**view, "closed": 0, "rolled_back": 0, "disabled": False}
    (row,) = w.conn.ran("INSERT INTO assess_terminations")
    assert row["dis"] is False
    assert [kw["reason_code"] for _, kw in w.events] == ["terminate"]


def test_a_count_mismatch_changes_nothing(
    world: Any, monkeypatch: pytest.MonkeyPatch
) -> None:
    w = world(terminate_script([live("ACTIVE"), live("STARTING")]))
    status, detail = refused(terminate(monkeypatch, expected_active_count=2))
    assert status == 409 and details(detail) == {"current": 1}
    assert w.conn.ran("assess_close_session") == []
    assert w.conn.ran("INSERT INTO assess_terminations") == [] and w.events == []


def test_a_locked_session_refusing_the_close_is_never_reported_closed(
    world: Any, monkeypatch: pytest.MonkeyPatch
) -> None:
    world(terminate_script([live("ACTIVE")], close_ok=False))
    with pytest.raises(RuntimeError):
        asyncio.run(terminate(monkeypatch, expected_active_count=1))


def test_a_too_long_reason_is_refused_before_any_query(
    world: Any, monkeypatch: pytest.MonkeyPatch
) -> None:
    w = world(terminate_script([]))
    body(monkeypatch, TerminatePost, reason="x" * 1001, expected_active_count=0)
    status, detail = refused(
        ops.terminate_sessions("3", SimpleNamespace(), SimpleNamespace())
    )
    assert status == 400 and fields(detail) == [("reason", "too_long")]
    assert w.conn.sql == []


# --- extra time ----------------------------------------------------------------------------------


def extra_script(
    sessions: list[Any], *, config: Any = "default", grants: int = 0, old: Any = None
) -> dict[str, Any]:
    return {
        "FROM assess_activity_config": Result(
            [] if config is None else [cfg() if config == "default" else config]
        ),
        "ORDER BY session_no FOR UPDATE": Result(sessions),
        "count(*) FROM assess_reset_grants": Result(scalar=grants),
        "SELECT extra_seconds": Result(scalar=old),
    }


def extra(monkeypatch: pytest.MonkeyPatch, **fields: Any) -> Any:
    body(monkeypatch, ExtraTimePut, **fields)
    return ops.put_extra_time("3", "42", SimpleNamespace(), SimpleNamespace())


@pytest.mark.parametrize(
    ("sessions", "grants"), [([], 0), ([sess(), sess(sid=uuid.uuid4(), no=2)], 2)]
)
def test_extra_time_is_stored_for_a_student_who_has_not_started(
    world: Any, monkeypatch: pytest.MonkeyPatch, sessions: list[Any], grants: int
) -> None:
    w = world(extra_script(sessions, grants=grants, old=600))
    status, view = run(extra(monkeypatch, percent=50))
    assert status == 200
    assert view == {"student_userid": 42, "extra_seconds": 900, "duration_s": 2700}
    (row,) = w.conn.ran("INSERT INTO assess_extra_time")
    assert (row["u"], row["x"], row["by"]) == (42, 900, "5")
    ((_, kw),) = w.events
    assert (kw["data"]["old_s"], kw["data"]["new_s"]) == (600, 900)


@pytest.mark.parametrize(
    ("sessions", "grants", "state"),
    [
        ([sess(state="STARTING")], 0, "STARTING"),
        ([sess(state="ACTIVE")], 0, "ACTIVE"),
        ([sess()], 0, "CLOSED"),  # used up and not reset
    ],
)
def test_extra_time_is_locked_once_the_session_started(
    world: Any,
    monkeypatch: pytest.MonkeyPatch,
    sessions: list[Any],
    grants: int,
    state: str,
) -> None:
    w = world(extra_script(sessions, grants=grants))
    status, detail = refused(extra(monkeypatch, minutes=10))
    assert status == 400
    (item,) = details(detail)["fields"]
    assert (item["field"], item["code"], item["state"]) == (
        "extra_time",
        "session_locked",
        state,
    )
    assert w.conn.ran("INSERT INTO assess_extra_time") == []


@pytest.mark.parametrize("config", [None, cfg(cap_minutes=None)])
def test_extra_time_needs_a_configured_cap(
    world: Any, monkeypatch: pytest.MonkeyPatch, config: Any
) -> None:
    world(extra_script([], config=config))
    status, detail = refused(extra(monkeypatch, minutes=10))
    assert status == 400 and fields(detail) == [("extra_time", "cap_not_configured")]


def test_extra_time_out_of_its_rules_is_a_field_error(
    world: Any, monkeypatch: pytest.MonkeyPatch
) -> None:
    world(extra_script([]))
    status, detail = refused(extra(monkeypatch, minutes=10, percent=10))
    assert status == 400 and fields(detail) == [("extra_time", "minutes_or_percent")]


# --- opening fallback ----------------------------------------------------------------------------


def fallback_script(
    *, config: Any = "default", started: bool = False
) -> dict[str, Any]:
    return {
        "SET attested_opening_at": Result(scalar=4),
        "FROM assess_activity_config": Result(
            [] if config is None else [cfg() if config == "default" else config]
        ),
        "committed_at IS NOT NULL": Result(scalar=started),
    }


def fallback(
    monkeypatch: pytest.MonkeyPatch, at: datetime, attestation: bool = True
) -> Any:
    body(
        monkeypatch,
        OpeningFallbackPut,
        opening_at=at.isoformat(),
        attestation=attestation,
        attestation_text_version="v1",
    )
    return ops.put_opening_fallback("3", SimpleNamespace(), SimpleNamespace())


def at(epoch: int) -> datetime:
    return datetime.fromtimestamp(epoch, UTC)


def test_the_fallback_date_is_attested_and_audited(
    world: Any, monkeypatch: pytest.MonkeyPatch
) -> None:
    w = world(fallback_script())
    w.readable = {"quiz_overrides": True}  # quiz_grace unreadable: the fallback applies
    status, view = run(fallback(monkeypatch, at(NOW + 600)))
    assert status == 200 and view["version"] == 4 and view["previous"] is None
    (row,) = w.conn.ran("SET attested_opening_at")
    assert row["at"] == at(NOW + 600) and row["by"] == "5"
    ((_, kw),) = w.events
    assert kw["data"]["new"] == NOW + 600
    assert kw["data"]["attestation_text_version"] == "v1"


def test_the_fallback_is_refused_when_every_close_input_is_readable(
    world: Any, monkeypatch: pytest.MonkeyPatch
) -> None:
    w = world(fallback_script())
    w.readable = {"quiz_overrides": True, "quiz_grace": True}
    status, detail = refused(fallback(monkeypatch, at(NOW + 600), attestation=False))
    assert status == 400
    assert fields(detail) == [
        ("attestation", "required"),
        ("opening_at", "fallback_not_needed"),
    ]
    assert w.conn.ran("SET attested_opening_at") == []


def test_the_fallback_needs_a_configured_activity(
    world: Any, monkeypatch: pytest.MonkeyPatch
) -> None:
    world(fallback_script(config=None))
    status, detail = refused(fallback(monkeypatch, at(NOW + 600)))
    assert status == 400 and fields(detail) == [
        ("opening_at", "activity_not_configured")
    ]


def test_after_a_start_the_fallback_date_only_moves_later(
    world: Any, monkeypatch: pytest.MonkeyPatch
) -> None:
    w = world(fallback_script(config=cfg(attested_at=NOW + 900), started=True))
    w.facts["close_at"] = True  # a bool is not a readable close date
    status, detail = refused(fallback(monkeypatch, at(NOW + 600)))
    assert status == 400
    assert fields(detail) == [
        ("opening_at", "close_unreadable"),
        ("opening_at", "only_later"),
    ]


def test_readable_close_flags_reads_the_matrix_per_flag(
    world: Any, monkeypatch: pytest.MonkeyPatch
) -> None:
    w = world({})
    w.readable = {"quiz_grace": True}
    got = asyncio.run(ops.readable_close_flags(w.conn, SITE))
    assert got == {"quiz_overrides": False, "quiz_grace": True}


def test_iso_previous_date_is_returned_when_one_was_set(
    world: Any, monkeypatch: pytest.MonkeyPatch
) -> None:
    w = world(fallback_script(config=cfg(attested_at=NOW + 60)))
    _, view = run(fallback(monkeypatch, at(NOW) + timedelta(hours=1)))
    assert view["previous"].startswith("2027-")
    assert w.events[0][1]["data"]["old"] == NOW + 60
