"""The render evaluation's short cuts and side reads without a database (plans 10, 11b).

``test_render_moodle_reasons.py`` pins what ``store_render`` writes; these pin what feeds it:
the configuration row's short cuts, the REQ-091 date handed to the reset check, a failing
source read failing closed, the grader and recipient reads, and the job handler end to end.
"""

from __future__ import annotations

import json
import uuid
from contextlib import asynccontextmanager
from types import SimpleNamespace
from typing import Any

import pytest

from vektra_assess.gates import ButtonState, Code
from vektra_assess.worker import render as r

SITE = uuid.uuid4()


class Conn:
    def __init__(self, row: Any, sink: list[Any]) -> None:
        self.row = row
        self.sink = sink

    async def execute(self, stmt: Any, params: Any = None) -> Any:
        sql = str(stmt)
        self.sink.append((sql, params))
        return SimpleNamespace(first=lambda: self.row, scalar=lambda: "T0")

    async def rollback(self) -> None:
        self.sink.append(("ROLLBACK", None))

    def begin(self) -> Any:
        @asynccontextmanager
        async def tx() -> Any:
            yield self

        return tx()


def acquirer(row: Any, sink: list[Any]) -> Any:
    @asynccontextmanager
    async def acquire() -> Any:
        yield Conn(row, sink)

    return acquire


def act(**kw: Any) -> Any:
    base = dict(
        activity_type="quiz", enabled=True, cap_minutes=30, flags={}, end_at=None,
        attested_at=1_800_000_900, db_now=1_800_000_000, closed_sessions=0, open_sessions=0,
        reset_grants=0,
    )  # fmt: skip
    return SimpleNamespace(**{**base, **kw})


class Source:
    def __init__(self, exc: Exception | None = None) -> None:
        self.exc = exc
        self.forgot: list[Any] = []

    async def gate_facts(self, target: Any) -> Any:
        if self.exc is not None:
            raise self.exc
        from vektra_assess.worker.preflight import MoodleGateFacts

        return MoodleGateFacts()

    def forget(self, sid: Any) -> None:
        self.forgot.append(sid)


def deps(source: Any = None, **kw: Any) -> r.RenderDeps:
    settings = SimpleNamespace(
        privacy_allow_unset=False, env="production", is_development=False
    )

    async def privacy(conn: Any, site: Any) -> bool:
        return True

    return r.RenderDeps(
        settings=settings, source=source or Source(), privacy=privacy, **kw
    )  # type: ignore[arg-type]


async def test_no_configuration_row_is_not_enabled_and_has_no_target() -> None:
    decision, target = await r.evaluate_render(deps(), acquirer(None, []), SITE, 5, 9)
    assert (decision.code, decision.state) == (
        Code.NOT_ENABLED,
        ButtonState.NOT_ENABLED,
    )
    assert target is None


@pytest.mark.parametrize(
    ("row", "code"),
    [
        (act(enabled=False), Code.NOT_ENABLED),
        (act(cap_minutes=None), Code.NOT_ENABLED),
        (act(open_sessions=1), Code.SESSION_OPEN),
    ],
)
async def test_the_short_cuts_keep_the_target_and_report_the_date_used(
    row: Any, code: Code
) -> None:
    seen: dict[str, Any] = {}
    decision, target = await r.evaluate_render(
        deps(), acquirer(row, []), SITE, 5, 9, courseid_hint=7, seen=seen
    )
    assert decision.code is code and decision.reasons == ()
    assert target is not None and target.courseid_hint == 7 and target.session_no == 1
    assert seen == {"attested_at": 1_800_000_900}


async def test_a_failing_source_read_fails_closed() -> None:
    decision, target = await r.evaluate_render(
        deps(Source(RuntimeError("ws down"))),
        acquirer(act(closed_sessions=1), []),
        SITE,
        5,
        9,
    )
    assert decision.code is not Code.OK
    assert target is not None and target.session_no == 2


class Graders:
    def __init__(self, result: Any = None, exc: Exception | None = None) -> None:
        self.result = result
        self.exc = exc

    async def graders(self, target: Any) -> Any:
        if self.exc is not None:
            raise self.exc
        return self.result


@pytest.mark.parametrize(
    ("source", "want"),
    [
        (None, None),
        (Graders(exc=RuntimeError("x")), None),
        (Graders(result=None), None),
        (
            Graders(result=[SimpleNamespace(userid=9), SimpleNamespace(userid=3),
                            SimpleNamespace(userid=9)]),
            (3, 9),
        ),
    ],
)  # fmt: skip
async def test_the_grader_read_is_sorted_unique_or_none(source: Any, want: Any) -> None:
    got = await r.read_graders(deps(graders=source), SimpleNamespace())  # type: ignore[arg-type]
    assert (got.graders if got else None) == want


class Recipients:
    def __init__(self, result: Any = None, exc: Exception | None = None) -> None:
        self.result = result
        self.exc = exc

    async def read(self, conn: Any, site: Any, cmid: int) -> Any:
        if self.exc is not None:
            raise self.exc
        return self.result


async def test_recipients_are_masked_or_none_when_unreadable() -> None:
    sink: list[Any] = []
    acquire = acquirer(None, sink)
    assert await r.read_recipients(deps(), acquire, SITE, 9) is None
    assert (
        await r.read_recipients(
            deps(recipients=Recipients(exc=OSError())), acquire, SITE, 9
        )
        is None
    )  # type: ignore[arg-type]
    assert (
        await r.read_recipients(deps(recipients=Recipients(None)), acquire, SITE, 9)
        is None
    )  # type: ignore[arg-type]
    got = await r.read_recipients(
        deps(recipients=Recipients(["mario.rossi@example.org"])),
        acquire,
        SITE,
        9,  # type: ignore[arg-type]
    )
    assert got is not None and len(got) == 1
    assert "mario.rossi" not in json.dumps(got) and got[0]["name"].endswith(
        "@example.org"
    )
    assert ("ROLLBACK", None) in sink  # the read never commits anything


async def test_the_handler_stores_the_evaluation_and_forgets_the_scratch_key() -> None:
    sink: list[Any] = []
    source = Source()
    handle = r.make_render_handler(
        deps(source, graders=Graders(result=[SimpleNamespace(userid=4)]))
    )
    ctx = SimpleNamespace(
        args={"site_id": str(SITE), "student_userid": 5, "cmid": 9, "courseid": 7},
        acquire=acquirer(act(), sink),
    )
    await handle(ctx)  # type: ignore[arg-type]
    (params,) = [p for s, p in sink if "INSERT INTO assess_gate_inputs" in s]
    inputs = json.loads(params["i"])
    assert params["t"] == "T0"  # the clock read before the source reads
    assert inputs["graders"] == [4]
    assert inputs["attested_at"] == 1_800_000_900
    assert len(source.forgot) == 1


async def test_a_malformed_job_is_a_no_op() -> None:
    sink: list[Any] = []
    handle = r.make_render_handler(deps())
    await handle(
        SimpleNamespace(args={"site_id": "nope"}, acquire=acquirer(act(), sink))
    )  # type: ignore[arg-type]
    assert sink == []
