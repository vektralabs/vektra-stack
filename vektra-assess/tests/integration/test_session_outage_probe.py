"""M4.2: the worker's outage probe ends OUTAGE with the credit (REQ-086), on a real database."""

from __future__ import annotations

import json
from collections.abc import AsyncIterator
from typing import Any

import pytest
from sqlalchemy.ext.asyncio import create_async_engine
from support.litellm_errors import litellm_error
from support.sessions import seed_session
from support.world import world

from integration.test_session_preflight import _reset
from vektra_assess.timing import deadline
from vektra_assess.worker.outage_probe import make_llm_probe, outage_tick
from vektra_shared.types import CompletionChunk

pytestmark = pytest.mark.integration

G = 20


@pytest.fixture
async def worker(pg):  # type: ignore[no-untyped-def]
    await _reset(pg)
    engine = create_async_engine(pg.url("worker"))
    yield engine
    await engine.dispose()


class Probe:
    def __init__(self, ok: bool | BaseException = True) -> None:
        self.ok = ok
        self.calls = 0

    async def __call__(self) -> bool:
        self.calls += 1
        if isinstance(self.ok, BaseException):
            raise self.ok
        return self.ok


async def started_ago(pg, sid, seconds: int) -> None:  # type: ignore[no-untyped-def]
    await pg.run(
        "super",
        "UPDATE assess_outage_incidents SET started_at = now() - make_interval(secs => :n) "
        "WHERE session_id = :s AND ended_at IS NULL",
        {"s": sid, "n": seconds},
    )


async def session(pg, sid) -> Any:  # type: ignore[no-untyped-def]
    return (
        await pg.run(
            "super",
            "SELECT state, credited_outage_s, deadline_at, committed_at, duration_s "
            "FROM assess_sessions WHERE id = :s",
            {"s": sid},
        )
    )[0]


async def test_a_successful_probe_ends_the_outage_with_the_credit(pg, worker) -> None:  # type: ignore[no-untyped-def]
    async with world(pg) as w:
        s = await seed_session(w, worker, "OUTAGE", cap_minutes=30)
        await started_ago(pg, s.session_id, 180)
        probe = Probe(True)
        got = await outage_tick(worker.connect, probe, min_credit_s=G)
        assert got.ok and got.ended == 1 and probe.calls == 1
        row = await session(pg, s.session_id)
        assert row.state == "ACTIVE"
        assert 179 <= row.credited_outage_s <= 185  # 3-min outage -> +3 min (+-5 s)
        # the one deadline formula, with the new credit
        assert row.deadline_at == deadline(
            row.committed_at, row.duration_s, row.credited_outage_s
        )
        inc = await pg.run(
            "super",
            "SELECT ended_at IS NOT NULL, credited_s FROM assess_outage_incidents "
            "WHERE session_id = :s",
            {"s": s.session_id},
        )
        assert [tuple(i) for i in inc] == [(True, row.credited_outage_s)]
        audit = await pg.run(
            "super",
            "SELECT actor_kind, data FROM assess_audit_events "
            "WHERE session_id = :s AND event = 'assess_outage_end'",
            {"s": s.session_id},
        )
        assert [(a[0], a[1]) for a in audit] == [
            ("worker", {"credited_s": row.credited_outage_s})
        ]


async def test_a_failed_probe_changes_nothing(pg, worker) -> None:  # type: ignore[no-untyped-def]
    async with world(pg) as w:
        s = await seed_session(w, worker, "OUTAGE")
        for outcome in (False, TimeoutError()):
            got = await outage_tick(worker.connect, Probe(outcome), min_credit_s=G)
            assert got.probed and not got.ok and got.ended == 0
        row = await session(pg, s.session_id)
        assert row.state == "OUTAGE" and row.credited_outage_s == 0


async def test_no_outage_no_probe(pg, worker) -> None:  # type: ignore[no-untyped-def]
    async with world(pg) as w:
        await seed_session(w, worker, "ACTIVE")
        probe = Probe(True)
        got = await outage_tick(worker.connect, probe, min_credit_s=G)
        assert probe.calls == 0 and not got.probed


async def test_short_outage_earns_nothing_and_the_credit_stays_within_c(
    pg, worker
) -> None:  # type: ignore[no-untyped-def]
    async with world(pg) as w:
        s = await seed_session(w, worker, "OUTAGE", cap_minutes=30)  # C = 900 s
        await started_ago(pg, s.session_id, 15)
        await outage_tick(worker.connect, Probe(True), min_credit_s=G)
        assert (await session(pg, s.session_id)).credited_outage_s == 0  # 15 s < G
        # a later outage near the cap: credited only up to C in total
        await pg.run(
            "super",
            "UPDATE assess_sessions SET credited_outage_s = 850, "
            "deadline_at = committed_at + make_interval(secs => duration_s + 850) "
            "WHERE id = :s",
            {"s": s.session_id},
        )
        await pg.run(
            "api",
            "SELECT assess_enter_outage(:s, 1, now(), 'error')",
            {"s": s.session_id},
        )
        await started_ago(pg, s.session_id, 120)
        await outage_tick(worker.connect, Probe(True), min_credit_s=G)
        row = await session(pg, s.session_id)
        assert row.state == "ACTIVE" and row.credited_outage_s == 900


async def test_the_pending_turn_restarts_its_clock_at_the_end(pg, worker) -> None:  # type: ignore[no-untyped-def]
    """The PendingTurnDriver answers it, not reconcile_reply (which waits timeout + grace)."""
    async with world(pg) as w:
        s = await seed_session(w, worker, "OUTAGE")
        await pg.run(
            "super",
            "UPDATE assess_sessions SET pending_reply_since = now() - interval '10 minutes' "
            "WHERE id = :s",
            {"s": s.session_id},
        )
        await outage_tick(worker.connect, Probe(True), min_credit_s=G)
        age = await pg.run(
            "super",
            "SELECT extract(epoch FROM now() - pending_reply_since) FROM assess_sessions "
            "WHERE id = :s",
            {"s": s.session_id},
        )
        assert float(age[0][0]) < 5


def _probe_of(llm: Any) -> Any:
    return make_llm_probe(
        llm, "openai/reasoner", timeout_s=1, max_tokens=600, temperature=0.2, seed=None
    )


async def test_the_llm_probe_is_one_streamed_turn_shaped_call() -> None:
    calls: list[dict[str, Any]] = []

    class Llm:
        async def stream(  # type: ignore[no-untyped-def]
            self, messages, model, temperature=0.7, max_tokens=None, **kw
        ) -> AsyncIterator[CompletionChunk]:
            calls.append(
                {"n": len(messages), "model": model, "max_tokens": max_tokens, "kw": kw}
            )

            async def gen() -> AsyncIterator[CompletionChunk]:
                yield CompletionChunk(content="ok")

            return gen()

    assert await _probe_of(Llm())() is True
    assert calls == [{"n": 1, "model": "openai/reasoner", "max_tokens": 600, "kw": {}}]


def _probe_lines(capture: Any) -> list[dict[str, Any]]:
    lines = [json.loads(ln) for ln in capture.lines()]
    return [ln for ln in lines if ln.get("event") == "assess_outage_probe_failed"]


@pytest.mark.parametrize(
    ("name", "status", "cause", "http_status"),
    [
        ("AuthenticationError", 500, "auth_error", None),  # litellm, no key configured
        ("AuthenticationError", 401, "auth_error", 401),
        ("PermissionDeniedError", 403, "auth_error", 403),
        ("BadRequestError", 400, "http_4xx", 400),
        ("NotFoundError", 404, "http_4xx", 404),
    ],
)
async def test_a_refused_probe_keeps_the_outage_and_says_it_is_configuration(  # type: ignore[no-untyped-def]
    pg, worker, canary_log_capture, name, status, cause, http_status
) -> None:
    """The provider answered, so it is reachable, but the next turn would be refused too:
    the outage stays open (the timer stays frozen, C still bounds it) and the log line says
    ``provider_refused`` with the closed cause, so the operator looks at the configuration."""
    async with world(pg) as w:
        s = await seed_session(w, worker, "OUTAGE")
        got = await outage_tick(
            worker.connect, Probe(litellm_error(name, status)), min_credit_s=G
        )
        assert got.probed and not got.ok and got.ended == 0
        assert (await session(pg, s.session_id)).state == "OUTAGE"
    (line,) = _probe_lines(canary_log_capture)
    assert line["reason_code"] == "provider_refused" and line["level"] == "warning"
    assert (line["outage_cause"], line["http_status"]) == (cause, http_status)
    assert line["error_type"] == name


@pytest.mark.parametrize(
    ("exc", "cause", "http_status"),
    [
        (ConnectionError("down"), "connection_error", None),
        # a 429 is transient (review of #219, finding 3): not provider_refused
        (litellm_error("RateLimitError", 429), "http_4xx", 429),
    ],
)
async def test_an_unreachable_or_rate_limited_probe_says_provider_unavailable(  # type: ignore[no-untyped-def]
    pg, worker, canary_log_capture, exc, cause, http_status
) -> None:
    async with world(pg) as w:
        s = await seed_session(w, worker, "OUTAGE")
        got = await outage_tick(worker.connect, Probe(exc), min_credit_s=G)
        assert got.probed and not got.ok
        assert (await session(pg, s.session_id)).state == "OUTAGE"
    (line,) = _probe_lines(canary_log_capture)
    assert line["reason_code"] == "provider_unavailable" and line["level"] == "info"
    assert (line["outage_cause"], line["http_status"]) == (cause, http_status)


async def test_a_reasoning_model_probe_ends_the_outage(pg, worker) -> None:  # type: ignore[no-untyped-def]
    """The live bug end to end: a provider that refuses a non-streamed call and any
    ``max_tokens`` below the turn's (400), and streams reasoning (empty chunks) then a
    length-cut end. The probe, shaped like a turn, ends the outage."""

    class Reasoner:
        async def complete(self, *a: Any, **kw: Any) -> Any:
            raise litellm_error("BadRequestError", 400)

        async def stream(  # type: ignore[no-untyped-def]
            self, messages, model, temperature=0.7, max_tokens=None, **kw
        ) -> AsyncIterator[CompletionChunk]:
            if max_tokens is None or max_tokens < 600:
                raise litellm_error("BadRequestError", 400)

            async def gen() -> AsyncIterator[CompletionChunk]:
                yield CompletionChunk(content="")
                yield CompletionChunk(content="", done=True, finish_reason="length")

            return gen()

    async with world(pg) as w:
        s = await seed_session(w, worker, "OUTAGE")
        got = await outage_tick(worker.connect, _probe_of(Reasoner()), min_credit_s=G)
        assert got.ok and got.ended == 1
        assert (await session(pg, s.session_id)).state == "ACTIVE"
