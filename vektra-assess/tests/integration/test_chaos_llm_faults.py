"""Chaos: provider faults from the fault-injecting LLM stub, through the real runner (plan 13a).

The turn runner is the production composition (``vektra_app.assess_llm``: ``LitellmProvider`` for
``VEKTRA_ASSESS_LLM_MODEL`` with no fallback, ``DiscussionTurnRunner``), pointed at
``tests/chaos/assess/llm_stub.py`` on 127.0.0.1. No fake runner: the outage the session sees is the
one litellm raised on the stub's real HTTP answer, or the runner's own first-token timeout on a
connection the stub keeps open and silent.

- **service_failure** (REQ-086, M4.5): the stub answers 503 to every call. Failed call 1 and 2 move
  the session to OUTAGE (the student turn is kept); between them the worker's outage tick ends the
  outage (its probe is a stub that says "up", which is what lets the pending turn be retried) and
  the API's ``PendingTurnDriver`` retries. The third consecutive failure closes ``service_failure``:
  epoch bumped, hand-off gone, one transcript job and one teacher notification job, 2 outage
  intervals, no bot reply persisted. Negative control: the same sequence with the stub back to
  ``ok`` before the third call answers the turn and the session stays ACTIVE.
- **stalled reply**: the stub reads the request and never answers. The runner gives up at
  ``VEKTRA_ASSESS_OUTAGE_TIMEOUT_S`` (2 s here), records ``first_token_timeout`` and the session
  enters OUTAGE with its turn kept; nothing hangs. With the stub back to ``ok`` the outage ends and
  the same pending turn is answered exactly once with the stub's reply.

Every scenario proves the stub was actually hit (``requests`` by mode) and ends with the invariant
checker.
"""

from __future__ import annotations

import asyncio
import sys
import time
from pathlib import Path
from typing import Any

import pytest
from support.sessions import seed_session
from support.world import default_settings, world

from integration.test_session_service_failure import (
    _Req,
    bearer,
    closed_row,
    ok_probe,
    wait_idle,
    worker,  # noqa: F401  (the fixture)
)
from vektra_assess.api.events import broker_of
from vektra_assess.api.pending import PendingTurnDriver
from vektra_assess.worker.outage_probe import outage_tick
from vektra_shared.safeguards import PassthroughSafeguard

sys.path.insert(
    0, str(Path(__file__).resolve().parents[3] / "tests" / "chaos" / "assess")
)
from invariants import assert_invariants
from llm_stub import REPLY, LlmStub

pytestmark = pytest.mark.integration


class _NoTrace:
    async def write(self, trace: Any, namespace: str) -> None:
        return None


async def _no_audit(event: str, data: Any) -> None:
    return None


def _runner(settings: Any) -> Any:
    from vektra_app.assess_llm import build_assess_llm, build_assess_runner

    return build_assess_runner(
        settings,
        safeguard=PassthroughSafeguard(),
        trace_writer=_NoTrace(),
        audit=_no_audit,
        llm=build_assess_llm(settings),
    )


def _settings(stub: LlmStub, **over: Any) -> Any:
    return default_settings(
        VEKTRA_ASSESS_LLM_MODEL="openai/stub",
        VEKTRA_ASSESS_LLM_API_BASE=stub.base_url,
        VEKTRA_LLM_API_KEY="not-a-key",
        **over,
    )


async def _counts(pg: Any, sid: Any) -> tuple[Any, ...]:
    """state, epoch, hand-off, student turns, bot normal turns, open / total outage intervals,
    pending reply."""
    return tuple(
        (
            await pg.run(
                "super",
                "SELECT s.state, s.epoch, "
                "(SELECT count(*) FROM assess_session_handoff h WHERE h.session_id = s.id), "
                "(SELECT count(*) FROM assess_turns t WHERE t.session_id = s.id "
                " AND t.role = 'student'), "
                "(SELECT count(*) FROM assess_turns t WHERE t.session_id = s.id "
                " AND t.role = 'bot' AND t.kind = 'normal'), "
                "(SELECT count(*) FROM assess_outage_incidents i WHERE i.session_id = s.id "
                " AND i.ended_at IS NULL), "
                "(SELECT count(*) FROM assess_outage_incidents i WHERE i.session_id = s.id), "
                "s.pending_reply_since IS NOT NULL "
                "FROM assess_sessions s WHERE s.id = :s",
                {"s": sid},
            )
        )[0]
    )


async def _retry(w: Any, worker: Any, sid: Any) -> None:  # noqa: F811
    """The worker's outage tick ends the outage, the API's pending driver retries the turn."""
    await outage_tick(worker.connect, ok_probe, min_credit_s=20)
    assert await PendingTurnDriver(w.app, min_age_s=0).run_once() == [sid]
    await wait_idle(w)


async def _fail_twice(w: Any, worker: Any, stub: LlmStub, s: Any) -> None:  # noqa: F811
    sid = s.session_id
    broker_of(_Req(w.app)).observe(sid, "ACTIVE", {})  # type: ignore[arg-type]
    before = await _counts(w.pg, sid)
    assert before == ("ACTIVE", 1, 1, 0, 0, 0, 0, False), before
    r = await w.client.post(
        f"/api/v1/assess/sessions/{sid}/turns",
        json={"text": "Ho scelto quel criterio perche' era il piu' chiaro."},
        headers=bearer(s.session_token),
    )
    assert r.status_code == 202, r.text
    await wait_idle(w)  # failed call 1
    assert await _counts(w.pg, sid) == ("OUTAGE", 1, 1, 1, 0, 1, 1, True)
    await _retry(w, worker, sid)  # failed call 2
    assert await _counts(w.pg, sid) == ("OUTAGE", 1, 1, 1, 0, 1, 2, True)
    assert stub.requests["5xx"] >= 2


async def test_three_consecutive_5xx_from_the_provider_close_service_failure(
    pg,
    worker,  # noqa: F811
) -> None:  # type: ignore[no-untyped-def]
    stub = await LlmStub(mode="5xx").start()
    try:
        settings = _settings(stub)
        async with world(pg, settings, assess_runner=_runner(settings)) as w:
            s = await seed_session(w, worker, "ACTIVE")
            await _fail_twice(w, worker, stub, s)
            hits = stub.requests["5xx"]
            await _retry(w, worker, s.session_id)  # failed call 3: the close
            assert stub.requests["5xx"] > hits and stub.requests["ok"] == 0
            assert await closed_row(pg, s.session_id) == (
                "CLOSED",
                "service_failure",
                2,
                1,
                1,
                0,
            )
            # measured: the close leaves the unanswered student turn marked pending
            # (pending_reply_since stays set on the CLOSED row; nothing reads it there)
            assert await _counts(pg, s.session_id) == (
                "CLOSED",
                2,
                0,
                1,
                0,
                0,
                2,
                True,
            )
            audit = await pg.run(
                "super",
                "SELECT data->>'failed_calls' FROM assess_audit_events "
                "WHERE session_id = :s AND event = 'assess_outage_start' ORDER BY id",
                {"s": s.session_id},
            )
            assert [a[0] for a in audit] == ["1", "2"]
            await assert_invariants(
                pg, "llm-stub:5xx-service-failure", expect_sessions=1, sites=[w.site]
            )
    finally:
        await stub.stop()


async def test_control_the_provider_back_before_the_third_call_answers_the_turn(
    pg,
    worker,  # noqa: F811
) -> None:  # type: ignore[no-untyped-def]
    stub = await LlmStub(mode="5xx").start()
    try:
        settings = _settings(stub)
        async with world(pg, settings, assess_runner=_runner(settings)) as w:
            s = await seed_session(w, worker, "ACTIVE")
            await _fail_twice(w, worker, stub, s)
            stub.mode = "ok"
            await _retry(w, worker, s.session_id)
            assert stub.requests["ok"] >= 1
            assert await _counts(pg, s.session_id) == (
                "ACTIVE",
                1,
                1,
                1,
                1,
                0,
                2,
                False,
            )
            await assert_invariants(
                pg, "llm-stub:5xx-recovered", expect_sessions=1, sites=[w.site]
            )
    finally:
        await stub.stop()


async def test_a_stalled_provider_enters_outage_at_the_timeout_then_answers_once(
    pg,
    worker,  # noqa: F811
) -> None:  # type: ignore[no-untyped-def]
    stub = await LlmStub(mode="stall").start()
    try:
        settings = _settings(stub, VEKTRA_ASSESS_OUTAGE_TIMEOUT_S=2)
        async with world(pg, settings, assess_runner=_runner(settings)) as w:
            s = await seed_session(w, worker, "ACTIVE")
            sid = s.session_id
            broker_of(_Req(w.app)).observe(sid, "ACTIVE", {})  # type: ignore[arg-type]
            t0 = time.monotonic()
            r = await w.client.post(
                f"/api/v1/assess/sessions/{sid}/turns",
                json={"text": "Perche' ho usato quella formula?"},
                headers=bearer(s.session_token),
            )
            assert r.status_code == 202, r.text
            await wait_idle(w)
            waited = time.monotonic() - t0
            assert 2.0 <= waited < 15.0, waited  # ended by the outage timeout, not hung
            assert stub.requests["stall"] == 1
            assert await _counts(pg, sid) == ("OUTAGE", 1, 1, 1, 0, 1, 1, True)
            reason = await pg.run(
                "super",
                "SELECT reason_code FROM assess_outage_incidents WHERE session_id = :s",
                {"s": sid},
            )
            assert [x[0] for x in reason] == ["first_token_timeout"]

            stub.mode = "ok"
            await _retry(w, worker, sid)
            assert stub.requests["ok"] >= 1
            assert await _counts(pg, sid) == ("ACTIVE", 1, 1, 1, 1, 0, 1, False)
            got = await w.client.get(
                f"/api/v1/assess/sessions/{sid}", headers=bearer(s.session_token)
            )
            assert got.status_code == 200, got.text
            bot = [t for t in got.json()["turns"] if t["role"] == "bot"]
            assert bot[-1]["kind"] == "normal" and REPLY.split()[0] in bot[-1]["text"]
            await asyncio.sleep(0)
            await assert_invariants(
                pg, "llm-stub:stalled-reply", expect_sessions=1, sites=[w.site]
            )
    finally:
        await stub.stop()
