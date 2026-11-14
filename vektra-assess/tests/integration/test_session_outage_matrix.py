"""M4.6: the REQ-086 outage matrix, end to end, with a fault-injecting fake provider.

The real ``DiscussionTurnRunner`` runs against ``FaultLLM`` (down, hanging, slow, refusing with a
named provider error, or healthy), inside the real API (``TurnDriver``, ``PendingTurnDriver``) and
the real worker bodies (``outage_tick``, ``sweep_timed_out``) on a real database. Outage LENGTHS
are simulated on the database clock (the open interval's ``started_at`` is moved back): the
3-minute and 15-minute cases cannot wait in real time, and the credit is computed by the database
from those timestamps, the same as in production. The slow-success case runs in real time,
scaled (first token inside the outage timeout, the whole reply longer than it).
"""

from __future__ import annotations

import asyncio
from collections.abc import AsyncIterator
from contextlib import asynccontextmanager
from typing import Any

import pytest
from fakes import RecordingAuditSink, RecordingSafeguard, RecordingTraceWriter
from sqlalchemy.ext.asyncio import create_async_engine
from support.litellm_errors import litellm_error
from support.sessions import seed_session
from support.world import world

from integration.test_session_preflight import _reset
from vektra_assess.api.pending import PendingTurnDriver
from vektra_assess.config import VektraAssessSettings
from vektra_assess.llm import DiscussionTurnRunner
from vektra_assess.timing import deadline
from vektra_assess.worker.outage_probe import make_llm_probe, outage_tick
from vektra_assess.worker.slots import _tick_table
from vektra_assess.worker.sweeper import sweep_timed_out
from vektra_shared.types import CompletionChunk
from vektra_testkit.recorders import RecordingLLMProvider

pytestmark = pytest.mark.integration

G = 20
TIMEOUT_S = 0.3  # the outage timeout (no first token within it), scaled down


#: the HTTP status a provider error of that name carries in its ``response`` (litellm's classes)
_STATUS = {
    "ContextWindowExceededError": 400,
    "ContentPolicyViolationError": 400,
    "RateLimitError": 429,
    "AuthenticationError": 401,
    "InternalServerError": 500,
    "ServiceUnavailableError": 503,
}


class _Response:
    def __init__(self, status_code: int) -> None:
        self.status_code = status_code


_real_litellm = litellm_error


class FaultLLM(RecordingLLMProvider):
    """``mode``: ok | down | hang | slow | reasoning_only | <ProviderErrorClassName>.

    A named provider error carries a ``response`` with its HTTP status, as litellm's do: the
    runner classifies it by type name and received status. ``reasoning_only`` is a reasoning
    model that spends max_tokens before any text (empty chunks, then finish_reason "length").
    """

    def __init__(self) -> None:
        super().__init__([])
        self.mode = "ok"
        self.n_calls = 0
        #: the probe's one-word call is answered whatever the mode: the refusal belongs to the
        #: turn's request only (a context-length or content-policy 400)
        self.probe_ok = False
        self.probe_calls = 0
        #: awaited before a turn's call fails (a test moves the clock or the row meanwhile)
        self.before_fault: Any = None

    @staticmethod
    def _is_probe(messages: Any) -> bool:
        return len(messages) == 1 and getattr(messages[0], "content", None) == "ping"

    def _fault(self) -> None:
        if self.mode == "down":
            raise ConnectionError("provider down")
        if self.mode.startswith(
            "litellm:"
        ):  # litellm:<ClassName>:<status>, the real class
            _, name, status = self.mode.split(":")
            raise _real_litellm(name, int(status))
        if self.mode not in ("ok", "hang", "slow", "reasoning_only"):
            exc = type(self.mode, (Exception,), {})("refused")
            if self.mode in _STATUS:
                exc.response = _Response(_STATUS[self.mode])  # type: ignore[attr-defined]
            raise exc

    async def complete(self, messages: Any, model: str, **kw: Any) -> Any:  # type: ignore[override]
        self.n_calls += 1
        self._fault()
        if self.mode == "hang":
            await asyncio.sleep(3600)
        return None

    async def stream(  # type: ignore[override]
        self, messages: Any, model: str, **kw: Any
    ) -> AsyncIterator[CompletionChunk]:
        self.n_calls += 1
        if self._is_probe(messages):
            self.probe_calls += 1
            if self.probe_ok:
                return _ok_stream()
        elif self.before_fault is not None:
            await self.before_fault()
        self._fault()
        mode = self.mode

        async def gen() -> AsyncIterator[CompletionChunk]:
            if mode == "hang":
                await asyncio.sleep(3600)
            if mode == "reasoning_only":
                yield CompletionChunk(content="")
                yield CompletionChunk(content=" ")  # whitespace is no text either
                yield CompletionChunk(content="", done=True, finish_reason="length")
                return
            parts = ["Bene.", " Quale", " scelta", " vuoi spiegare?"]
            for part in parts:
                if mode == "slow":
                    await asyncio.sleep(
                        0.12
                    )  # each gap < timeout, the whole reply > timeout
                yield CompletionChunk(content=part)

        return gen()


def _ok_stream() -> AsyncIterator[CompletionChunk]:
    async def gen() -> AsyncIterator[CompletionChunk]:
        yield CompletionChunk(content="pong")

    return gen()


def make_runner(llm: FaultLLM) -> DiscussionTurnRunner:
    return DiscussionTurnRunner(
        llm=llm,
        safeguard=RecordingSafeguard(),
        trace_writer=RecordingTraceWriter(),
        settings=VektraAssessSettings.model_validate(
            {
                "VEKTRA_ASSESS_LLM_MODEL": "fault/model",
                "VEKTRA_ASSESS_OUTAGE_TIMEOUT_S": TIMEOUT_S,
            }
        ),
        audit=RecordingAuditSink(),
    )


@pytest.fixture
async def worker(pg):  # type: ignore[no-untyped-def]
    await _reset(pg)
    engine = create_async_engine(pg.url("worker"))
    yield engine
    await engine.dispose()


def bearer(token: str) -> dict[str, str]:
    return {"Authorization": f"Bearer {token}"}


async def wait_idle(w) -> None:  # type: ignore[no-untyped-def]
    driver = getattr(w.app.state, "assess_turn_driver", None)
    if driver is not None:
        for task in list(driver.tasks.values()):
            await asyncio.wait_for(task, 15)


class Rig:
    """One seeded session, the fault LLM and the server-side pieces, no client after the post."""

    def __init__(self, pg, w, worker, llm: FaultLLM, sid, token: str) -> None:  # type: ignore[no-untyped-def]
        self.pg, self.w, self.worker, self.llm, self.sid, self.token = (
            pg,
            w,
            worker,
            llm,
            sid,
            token,
        )
        self.probe = make_llm_probe(
            llm,
            "fault/model",
            timeout_s=TIMEOUT_S,
            max_tokens=600,
            temperature=0.2,
            seed=None,
        )
        self.pending = PendingTurnDriver(w.app, min_age_s=0)

    async def say(self, text: str = "Ho scelto quel metodo") -> None:
        r = await self.w.client.post(
            f"/api/v1/assess/sessions/{self.sid}/turns",
            json={"text": text},
            headers=bearer(self.token),
        )
        assert r.status_code == 202, r.text
        await wait_idle(self.w)  # the client is gone from here on

    async def outage_lasted(self, seconds: int) -> None:
        """Simulated outage length: the open interval started ``seconds`` ago."""
        await self.pg.run(
            "super",
            "UPDATE assess_outage_incidents SET started_at = now() - make_interval(secs => :n) "
            "WHERE session_id = :s AND ended_at IS NULL",
            {"s": self.sid, "n": seconds},
        )

    async def tick(self) -> None:
        await outage_tick(self.worker.connect, self.probe, min_credit_s=G)
        await self.pending.run_once()
        await wait_idle(self.w)

    async def row(self) -> Any:
        return (
            await self.pg.run(
                "super",
                "SELECT state, close_reason, credited_outage_s, deadline_at, committed_at, "
                "duration_s, (SELECT count(*) FROM assess_outage_incidents i "
                "WHERE i.session_id = s.id) AS intervals, (SELECT role || '/' || kind FROM "
                "assess_turns t WHERE t.session_id = s.id ORDER BY turn_no DESC LIMIT 1) AS last "
                "FROM assess_sessions s WHERE id = :s",
                {"s": self.sid},
            )
        )[0]


@asynccontextmanager
async def rigged(pg, worker) -> AsyncIterator[Rig]:  # type: ignore[no-untyped-def]
    llm = FaultLLM()
    async with world(pg, assess_runner=make_runner(llm)) as w:
        s = await seed_session(w, worker, "ACTIVE", cap_minutes=30)  # C = 900 s
        # committed 20 minutes ago, so a simulated interval is never clamped to the commit
        await pg.run(
            "super",
            "UPDATE assess_sessions SET committed_at = committed_at - interval '20 minutes', "
            "deadline_at = deadline_at - interval '20 minutes' WHERE id = :s",
            {"s": s.session_id},
        )
        yield Rig(pg, w, worker, llm, s.session_id, str(s.session_token))


@pytest.mark.parametrize(
    ("lasted_s", "low", "high"), [(180, 175, 185), (15, 0, 0)], ids=["3min", "15s"]
)
async def test_an_outage_is_credited_from_g_on(pg, worker, lasted_s, low, high) -> None:  # type: ignore[no-untyped-def]
    async with rigged(pg, worker) as rig:
        rig.llm.mode = "down"
        await rig.say()
        assert (await rig.row()).state == "OUTAGE"
        await rig.outage_lasted(lasted_s)
        await rig.tick()  # still down: nothing changes
        assert (await rig.row()).state == "OUTAGE"
        rig.llm.mode = "ok"
        await (
            rig.tick()
        )  # the probe succeeds; the pending turn is answered with no client
        row = await rig.row()
        assert row.state == "ACTIVE" and row.last == "bot/normal"
        assert low <= row.credited_outage_s <= high
        assert row.deadline_at == deadline(
            row.committed_at, row.duration_s, row.credited_outage_s
        )


async def test_a_hanging_provider_is_an_outage_too(pg, worker) -> None:  # type: ignore[no-untyped-def]
    async with rigged(pg, worker) as rig:
        rig.llm.mode = "hang"
        await rig.say()
        assert (await rig.row()).state == "OUTAGE"


async def test_a_slow_success_is_not_an_outage(pg, worker) -> None:  # type: ignore[no-untyped-def]
    async with rigged(pg, worker) as rig:
        rig.llm.mode = "slow"
        await rig.say()
        row = await rig.row()
        assert (row.state, row.credited_outage_s, row.intervals, row.last) == (
            "ACTIVE",
            0,
            0,
            "bot/normal",
        )


#: refusals of the CONFIGURATION (``is_config_refusal``: auth_error, a non-429 4xx): the turn is
#: marked interrupted, then the session pauses in OUTAGE ``service_misconfigured`` (live E2E
#: follow-up). Every other refusal leaves the session ACTIVE, as before.
CONFIG_REFUSALS = frozenset(
    {"ContextWindowExceededError", "ContentPolicyViolationError", "AuthenticationError"}
)


@pytest.mark.parametrize(
    "refusal",
    [
        "ContextWindowExceededError",
        "RateLimitError",  # a 429 stays a refusal, as before the closed causes
        "ContentPolicyViolationError",
        "AuthenticationError",  # 401: a wrong key is configuration, not an outage
        "BudgetExceededError",  # litellm's own spend limit, no HTTP answer
        "reasoning_only",  # max_tokens spent on reasoning: max_tokens_exhausted
    ],
)
async def test_provider_refusals_mark_the_turn_and_never_replay_it(
    pg, worker, refusal, canary_log_capture
) -> None:  # type: ignore[no-untyped-def]
    import json

    async with rigged(pg, worker) as rig:
        rig.llm.mode = refusal
        await rig.say()
        row = await rig.row()
        config = refusal in CONFIG_REFUSALS
        assert (row.state, row.credited_outage_s, row.intervals, row.last) == (
            "OUTAGE" if config else "ACTIVE",
            0,
            1 if config else 0,
            "bot/interrupted",
        )
    lines = [json.loads(ln) for ln in canary_log_capture.lines()]
    (line,) = [ln for ln in lines if ln.get("event") == "assess_turn_provider_failure"]
    assert line["reason_code"] == "provider_refused" and line["level"] == "warning"
    if refusal == "reasoning_only":
        assert line["outage_cause"] == "max_tokens_exhausted"
        assert (
            line["remediation_code"] == "raise_llm_max_tokens_or_lower_reasoning_effort"
        )
        assert line["max_tokens"] == 600 and line["http_status"] is None
    elif refusal == "AuthenticationError":
        assert line["outage_cause"] == "auth_error"
        assert line["http_status"] == _STATUS[refusal]
    elif refusal != "BudgetExceededError":
        assert line["outage_cause"] == "http_4xx"
        assert line["http_status"] == _STATUS[refusal]


@pytest.mark.parametrize(
    ("mode", "cause", "status"),
    [
        # the first live run: no API key, litellm attaches a 500, the session went OUTAGE
        ("litellm:AuthenticationError:500", "auth_error", None),
        ("litellm:AuthenticationError:401", "auth_error", 401),
        ("litellm:PermissionDeniedError:403", "auth_error", 403),
        ("litellm:NotFoundError:404", "http_4xx", 404),
        ("litellm:BadRequestError:400", "http_4xx", 400),
        ("litellm:UnprocessableEntityError:422", "http_4xx", 422),
        ("litellm:ContentPolicyViolationError:400", "http_4xx", 400),
        ("litellm:RateLimitError:429", "http_4xx", 429),
    ],
)
async def test_real_litellm_refusals_mark_the_turn(
    pg, worker, mode, cause, status, canary_log_capture
) -> None:  # type: ignore[no-untyped-def]
    import json

    async with rigged(pg, worker) as rig:
        rig.llm.mode = mode
        await rig.say()
        row = await rig.row()
        # a configuration refusal pauses the session; a 429 is transient and does not
        expected = ("ACTIVE", 0) if status == 429 else ("OUTAGE", 1)
        assert (row.state, row.intervals, row.last) == (*expected, "bot/interrupted")
    lines = [json.loads(ln) for ln in canary_log_capture.lines()]
    (line,) = [ln for ln in lines if ln.get("event") == "assess_turn_provider_failure"]
    assert line["reason_code"] == "provider_refused"
    assert (line["outage_cause"], line["http_status"]) == (cause, status)
    assert line["error_type"] == mode.split(":")[1]
    # a configuration refusal alerts the operator once (review of #219, finding 2a); a 429 is
    # transient and does not
    alerts = await pg.run(
        "super",
        "SELECT args FROM assess_jobs WHERE kind = 'operator_notify' "
        "AND args->>'kind' = 'provider_config_refused'",
    )
    if status == 429:
        assert alerts == []
    else:
        ((args,),) = alerts
        args = args if isinstance(args, dict) else json.loads(args)
        assert args["reason"] == cause


async def test_repeated_config_refusals_queue_one_alert(pg, worker) -> None:  # type: ignore[no-untyped-def]
    """No mail storm: the paused session takes no turn, the refused probes alert nobody, and a
    second refusal after a recovery falls in the same alert window."""
    async with rigged(pg, worker) as rig:
        rig.llm.mode = "litellm:AuthenticationError:500"
        await rig.say()
        for _ in range(3):
            await rig.tick()  # the probe is refused too: still paused
        assert (await rig.row()).state == "OUTAGE"
        rig.llm.mode = "ok"
        await rig.tick()
        assert (await rig.row()).state == "ACTIVE"
        rig.llm.mode = "litellm:AuthenticationError:500"
        await rig.say("di nuovo")
        assert (await rig.row()).state == "OUTAGE"
    alerts = await pg.run(
        "super",
        "SELECT args->>'reason' FROM assess_jobs WHERE kind = 'operator_notify' "
        "AND args->>'kind' = 'provider_config_refused'",
    )
    assert [a[0] for a in alerts] == ["auth_error"]


async def test_a_config_refusal_pauses_the_time_and_is_credited_on_recovery(
    pg, worker
) -> None:  # type: ignore[no-untyped-def]
    """The student's time does not run while the configuration is refused: the interval is
    credited like an outage once the probe gets an answer, and the refused turn stays answered
    by its marker (it is not replayed)."""
    from vektra_assess.api.events import broker_of

    class _Req:
        def __init__(self, app: Any) -> None:
            self.app = app

    async with rigged(pg, worker) as rig:
        rig.llm.mode = "litellm:NotFoundError:404"  # a wrong model name
        # an open stream has seen the session ACTIVE (the broker publishes on a change)
        broker_of(_Req(rig.w.app)).observe(rig.sid, "ACTIVE", {})  # type: ignore[arg-type]
        await rig.say()
        row = await rig.row()
        assert (row.state, row.last) == ("OUTAGE", "bot/interrupted")
        (code,) = await rig.pg.run(
            "super",
            "SELECT reason_code FROM assess_outage_incidents WHERE session_id = :s",
            {"s": rig.sid},
        )
        assert code[0] == "service_misconfigured"
        events = broker_of(_Req(rig.w.app)).after(rig.sid, 0)  # type: ignore[arg-type]
        interrupted = [e for e in events if e.type == "reply_interrupted"]
        started = [e for e in events if e.type == "outage_start"]
        assert [e.data["reason"] for e in interrupted] == ["service_misconfigured"]
        assert [e.data["reason"] for e in started] == ["service_misconfigured"]
        assert events.index(interrupted[0]) < events.index(started[0])

        # the session snapshot says why, for a reload during the pause
        r = await rig.w.client.get(
            f"/api/v1/assess/sessions/{rig.sid}", headers=bearer(rig.token)
        )
        assert r.status_code == 200, r.text
        assert r.json()["state"] == "OUTAGE"
        assert r.json()["outage_reason"] == "service_misconfigured"

        await rig.outage_lasted(180)
        await rig.tick()  # still refused: nothing moves
        assert (await rig.row()).state == "OUTAGE"
        calls = rig.llm.n_calls
        rig.llm.mode = "ok"  # the operator fixed the model
        await rig.tick()
        row = await rig.row()
        assert row.state == "ACTIVE" and 175 <= row.credited_outage_s <= 185
        assert row.deadline_at == deadline(
            row.committed_at, row.duration_s, row.credited_outage_s
        )
        # only the probe called the model: the refused turn was not answered again
        assert row.last == "bot/interrupted" and rig.llm.n_calls == calls + 1


async def test_a_provider_outage_snapshot_keeps_its_reason(pg, worker) -> None:  # type: ignore[no-untyped-def]
    async with rigged(pg, worker) as rig:
        rig.llm.mode = "down"
        await rig.say()
        r = await rig.w.client.get(
            f"/api/v1/assess/sessions/{rig.sid}", headers=bearer(rig.token)
        )
        assert r.json()["outage_reason"] == "provider_unavailable"
        rig.llm.mode = "ok"
        await rig.tick()
        r = await rig.w.client.get(
            f"/api/v1/assess/sessions/{rig.sid}", headers=bearer(rig.token)
        )
        assert r.json()["state"] == "ACTIVE" and "outage_reason" not in r.json()


@pytest.mark.parametrize(
    ("mode", "cause", "status"),
    [
        ("ServiceUnavailableError", "http_5xx", 503),
        ("down", "connection_error", None),
        ("hang", "first_token_timeout", None),
    ],
)
async def test_an_unavailable_provider_is_an_outage_with_its_cause_logged(
    pg, worker, mode, cause, status, canary_log_capture
) -> None:  # type: ignore[no-untyped-def]
    import json

    async with rigged(pg, worker) as rig:
        rig.llm.mode = mode
        await rig.say()
        assert (await rig.row()).state == "OUTAGE"
    lines = [json.loads(ln) for ln in canary_log_capture.lines()]
    (line,) = [ln for ln in lines if ln.get("event") == "assess_turn_provider_failure"]
    assert line["reason_code"] == "provider_unavailable"
    assert (line["outage_cause"], line["http_status"]) == (cause, status)
    assert line["remediation_code"] is None


async def test_a_refused_query_never_sets_outage(pg, worker) -> None:  # type: ignore[no-untyped-def]
    llm = FaultLLM()
    llm.mode = "down"  # would be an outage, if the model were called at all
    runner = make_runner(llm)
    runner._safeguard = RecordingSafeguard(refuse_query=True)
    async with world(pg, assess_runner=runner) as w:
        s = await seed_session(w, worker, "ACTIVE")
        r = await w.client.post(
            f"/api/v1/assess/sessions/{s.session_id}/turns",
            json={"text": "x"},
            headers=bearer(s.session_token),
        )
        assert r.status_code == 202
        await wait_idle(w)
        st = await pg.run(
            "super",
            "SELECT state, (SELECT count(*) FROM assess_outage_incidents WHERE session_id = :s) "
            "FROM assess_sessions WHERE id = :s",
            {"s": s.session_id},
        )
        assert tuple(st[0]) == ("ACTIVE", 0) and llm.n_calls == 0


async def test_beyond_c_closes_service_failure_with_the_intervals(pg, worker) -> None:  # type: ignore[no-untyped-def]
    async with rigged(pg, worker) as rig:
        rig.llm.mode = "down"
        await rig.say()
        await rig.outage_lasted(901)  # C = 900 s for a 30-minute session
        await rig.tick()
        row = await rig.row()
        assert (row.state, row.close_reason, row.intervals) == (
            "CLOSED",
            "service_failure",
            1,
        )
        ended = await rig.pg.run(
            "super",
            "SELECT ended_at IS NOT NULL, credited_s FROM assess_outage_incidents "
            "WHERE session_id = :s",
            {"s": rig.sid},
        )
        assert tuple(ended[0]) == (
            True,
            0,
        )  # recorded for the transcript, nothing credited


async def test_browser_killed_at_outage_start_reaches_active_or_closed_within_c(
    pg, worker
) -> None:  # type: ignore[no-untyped-def]
    """No client after the post. Provider back -> answered (ACTIVE); never back -> C closes."""
    async with rigged(pg, worker) as rig:
        rig.llm.mode = "down"
        await rig.say()
        await rig.outage_lasted(600)
        rig.llm.mode = "ok"
        await rig.tick()
        row = await rig.row()
        assert row.state == "ACTIVE" and row.last == "bot/normal"
        # a second outage that never recovers: closed at C, not left in OUTAGE
        rig.llm.mode = "down"
        await rig.say("e ancora")
        await rig.outage_lasted(301)  # 600 + 301 > 900
        await rig.tick()
        row = await rig.row()
        assert (row.state, row.close_reason) == ("CLOSED", "service_failure")


async def test_recovery_with_the_client_absent_then_closes_timed_out(
    pg, worker
) -> None:  # type: ignore[no-untyped-def]
    async with rigged(pg, worker) as rig:
        rig.llm.mode = "down"
        await rig.say()
        await rig.outage_lasted(60)
        # the sweeper never closes OUTAGE, even past the deadline
        await rig.pg.run(
            "super",
            "UPDATE assess_sessions SET deadline_at = now() - interval '2 hours' WHERE id = :s",
            {"s": rig.sid},
        )
        assert await sweep_timed_out(rig.worker.connect) == 0
        assert (await rig.row()).state == "OUTAGE"
        rig.llm.mode = "ok"
        await outage_tick(rig.worker.connect, rig.probe, min_credit_s=G)
        assert (await rig.row()).state == "ACTIVE"  # within one probe interval
        # its (credited) deadline is still long past: the sweeper closes it timed_out
        await rig.pg.run(
            "super",
            "UPDATE assess_sessions SET deadline_at = now() - interval '2 hours', "
            "pending_reply_since = NULL WHERE id = :s",
            {"s": rig.sid},
        )
        assert await sweep_timed_out(rig.worker.connect) == 1
        row = await rig.row()
        assert (row.state, row.close_reason) == ("CLOSED", "timed_out")


def test_the_probe_interval_is_5_to_10_seconds() -> None:
    spec = next(
        t for t in _tick_table(VektraAssessSettings()) if t.name == "outage_probe"
    )
    assert 5 <= spec.interval_s and spec.interval_s + spec.jitter_s <= 10


# --- review of #221 ------------------------------------------------------------------------------


@pytest.mark.parametrize(
    "mode",
    [
        "ContextWindowExceededError",
        "ContentPolicyViolationError",
        "litellm:BadRequestError:400",
    ],
)
async def test_a_request_specific_400_keeps_the_plain_refusal(pg, worker, mode) -> None:  # type: ignore[no-untyped-def]
    """Finding 1: the provider refused that request only (the probe is answered). The old path
    stands: the marked turn, ``provider_refused``, the session ACTIVE, no interval; three of
    them in a row never close the session."""
    from vektra_assess.api.events import broker_of

    class _Req:
        def __init__(self, app: Any) -> None:
            self.app = app

    async with rigged(pg, worker) as rig:
        rig.llm.mode = mode
        rig.llm.probe_ok = True
        for i in range(3):
            await rig.say(f"domanda {i}")
            row = await rig.row()
            assert (row.state, row.intervals, row.last) == (
                "ACTIVE",
                0,
                "bot/interrupted",
            )
        assert rig.llm.probe_calls == 3  # one probe per ambiguous refusal
        events = broker_of(_Req(rig.w.app)).after(rig.sid, 0)  # type: ignore[arg-type]
        reasons = [e.data["reason"] for e in events if e.type == "reply_interrupted"]
        assert reasons == ["provider_refused"] * 3


async def test_an_auth_error_pauses_without_a_probe(pg, worker) -> None:  # type: ignore[no-untyped-def]
    async with rigged(pg, worker) as rig:
        rig.llm.mode = "litellm:AuthenticationError:401"
        rig.llm.probe_ok = (
            True  # never asked: a refused key is always the configuration
        )
        await rig.say()
        assert (await rig.row()).state == "OUTAGE"
        assert rig.llm.probe_calls == 0


async def test_the_marker_and_the_pause_are_one_transaction(pg, worker) -> None:  # type: ignore[no-untyped-def]
    """Finding 6: the student turn is answered by the marker and the session paused in the SAME
    transaction, so there is no moment with the session ACTIVE and no reply owed in which the
    student could send a turn the pause then strands. Proven on the rows: the marker turn and
    the session's OUTAGE update carry the same transaction id."""
    async with rigged(pg, worker) as rig:
        rig.llm.mode = "litellm:AuthenticationError:401"
        await rig.say()
        ((same, state),) = await rig.pg.run(
            "super",
            "SELECT s.xmin::text = t.xmin::text, s.state FROM assess_sessions s "
            "JOIN assess_turns t ON t.session_id = s.id WHERE s.id = :s "
            "ORDER BY t.turn_no DESC LIMIT 1",
            {"s": rig.sid},
        )
        assert state == "OUTAGE" and same is True


async def test_no_pause_when_the_marker_is_refused(pg, worker) -> None:  # type: ignore[no-untyped-def]
    """Finding 7: when the database refuses the marked turn (here: no reply is owed any more,
    as when the worker's reconcile answered the turn meanwhile) the session is not paused
    either: nothing of this reply attempt was recorded, so nothing may stop the clock."""
    async with rigged(pg, worker) as rig:

        async def answered_elsewhere() -> None:
            await rig.pg.run(
                "super",
                "UPDATE assess_sessions SET pending_reply_since = NULL WHERE id = :s",
                {"s": rig.sid},
            )

        rig.llm.mode = "litellm:AuthenticationError:401"
        rig.llm.before_fault = answered_elsewhere
        await rig.say()
        row = await rig.row()
        assert (row.state, row.intervals) == ("ACTIVE", 0)
        assert row.last == "student/normal"  # the marker was refused


# --- second review of #221 -----------------------------------------------------------------------


@pytest.mark.parametrize(
    ("mode", "state"),
    [
        (
            "litellm:NotFoundError:404",
            "ACTIVE",
        ),  # needs the probe: unbuildable, not confirmed
        ("litellm:AuthenticationError:401", "OUTAGE"),  # never builds it
        ("litellm:RateLimitError:429", "ACTIVE"),  # never builds it
    ],
)
async def test_a_probe_that_cannot_be_built_never_loses_the_marker(
    pg, worker, mode, state
) -> None:  # type: ignore[no-untyped-def]
    """Finding E: building or running the confirming probe can fail; the turn still gets its
    marker (never a crashed reply task with the turn left pending), and the probe is built only
    for a non-auth, non-429 4xx."""
    async with rigged(pg, worker) as rig:
        built: list[int] = []

        def broken() -> Any:
            built.append(1)
            raise RuntimeError("no provider")

        runner = rig.w.app.state.assess_runner
        runner.config_probe = broken
        rig.llm.mode = mode
        await rig.say()
        row = await rig.row()
        assert (row.state, row.last) == (state, "bot/interrupted")
        assert len(built) == (1 if mode.endswith(":404") else 0)


async def test_an_enrolment_check_dated_far_in_the_future_is_not_fresh(
    pg, worker
) -> None:  # type: ignore[no-untyped-def]
    """Sixth review, B: the enrolment freshness uses the one rule (``is_fresh``): a re-check
    dated beyond the clock-skew tolerance (a backward clock step) is not trusted."""
    async with rigged(pg, worker) as rig:
        url = f"/api/v1/assess/sessions/{rig.sid}"
        r = await rig.w.client.get(url, headers=bearer(rig.token))
        assert r.status_code == 200, r.text
        await rig.pg.run(
            "super",
            "UPDATE assess_enrolment_checks SET checked_at = now() + interval '1 hour' "
            "WHERE session_id = :s",
            {"s": rig.sid},
        )
        r = await rig.w.client.get(url, headers=bearer(rig.token))
        assert r.status_code == 503, r.text
