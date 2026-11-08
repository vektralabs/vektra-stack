"""M5.6 / REQ-085: every lifecycle event writes exactly one audit row, and fails closed.

One action, one row: each step below counts its own event before and after. The events driven in
a dedicated test elsewhere are mapped in ``ELSEWHERE`` (the map itself is checked against the
enum, so a new event cannot be left untested silently). ``attempt_mismatch_rejected`` cannot be
driven: no route accepts a Moodle or attempt id (M2.6), the attempt is selected server-side at
Start; it stays in the enum for plan 9's client-side reports.
"""

from __future__ import annotations

import asyncio
from datetime import UTC, datetime
from pathlib import Path

import pytest
from sqlalchemy.exc import DBAPIError
from sqlalchemy.ext.asyncio import create_async_engine
from support.preflight import FakeStreamRunner
from support.sessions import fixture_target, seed_session
from support.world import default_settings, world

from integration.test_session_preflight import _reset
from vektra_assess.audit import AssessAuditEvent as E
from vektra_assess.llm.types import ProviderCallRecord, ProviderOutageError
from vektra_assess.worker.outage_probe import outage_tick
from vektra_assess.worker.preflight import rollback_start
from vektra_assess.worker.start_guard import StartLimits

pytestmark = pytest.mark.integration

HERE = Path(__file__).parent

#: event -> the test module that drives it and asserts exactly one row
ELSEWHERE = {
    E.LOGINAS_REJECTED: "test_session_loginas.py",
    E.TEAM_SUBMISSION_REFUSED: "test_session_preflight_gates.py",
    E.REPLY_RECONCILED: "test_session_reconcile.py",
    E.START_REFUSED: "test_session_start.py",
    E.START_BREAKER_OPEN: "test_session_start_guard.py",
    E.START_BREAKER_CLOSE: "test_session_start_guard.py",
    E.FLAG_ADDED: "test_session_flag.py",  # REQ-103, M5.9
    # plan assess-transcript-delivery
    E.TRANSCRIPT_GENERATED: "test_transcript_generation.py",
    E.TRANSCRIPT_INTEGRITY_FAILED: "test_transcript_generation.py",
    E.TRANSCRIPT_RECIPIENTS_UNRESOLVED: "test_transcript_generation.py",
    E.DOWNLOAD_TOKEN_MINT: "test_transcript_api.py",
    E.STUDENT_DOWNLOAD: "test_transcript_api.py",
    E.TEACHER_DOWNLOAD: "test_transcript_api.py",
    E.TRANSCRIPT_RESEND: "test_transcript_schema.py",
    # plan assess-transcript-delivery 10b
    E.DELIVERY_ATTEMPT: "test_delivery_handler.py",
    E.DELIVERY_SENT: "test_delivery_handler.py",
    E.DELIVERY_FAILED: "test_delivery_handler.py",
    E.DELIVERY_POSSIBLE_DUPLICATE: "test_delivery_handler.py",
    E.DELIVERY_RECIPIENT_REPLACED: "test_delivery_handler.py",
    E.DELIVERY_RECIPIENT_ADDED: "test_delivery_handler.py",
}
NOT_DRIVABLE = {E.ATTEMPT_MISMATCH_REJECTED}


@pytest.fixture
async def worker(pg):  # type: ignore[no-untyped-def]
    await _reset(pg)
    engine = create_async_engine(pg.url("worker"))
    yield engine
    await engine.dispose()


def bearer(token: str) -> dict[str, str]:
    return {"Authorization": f"Bearer {token}"}


class Counter:
    def __init__(self, pg) -> None:  # type: ignore[no-untyped-def]
        self.pg = pg
        self.seen: set[E] = set()

    async def count(self, event: E) -> int:
        return int(
            (
                await self.pg.run(
                    "super",
                    "SELECT count(*) FROM assess_audit_events WHERE event = :e",
                    {"e": event.value},
                )
            )[0][0]
        )

    async def exactly_one(self, event: E, action) -> None:  # type: ignore[no-untyped-def]
        before = await self.count(event)
        await action()
        assert await self.count(event) == before + 1, event
        self.seen.add(event)


async def wait_idle(w) -> None:  # type: ignore[no-untyped-def]
    driver = getattr(w.app.state, "assess_turn_driver", None)
    if driver is not None:
        for task in list(driver.tasks.values()):
            await asyncio.wait_for(task, 15)


async def test_each_lifecycle_event_writes_exactly_one_row(pg, worker) -> None:  # type: ignore[no-untyped-def]
    c = Counter(pg)
    now = datetime.now(UTC)
    runner = FakeStreamRunner(
        fail=ProviderOutageError(ProviderCallRecord(now, None, now, "error"))
    )
    settings = default_settings(VEKTRA_ASSESS_SESSION_TOKEN_BUDGET=10_000)
    async with world(pg, settings, assess_runner=runner) as w:
        sub, cmid = fixture_target(name="simple")

        async def mint() -> None:
            assert (await w.mint(sub, cmid)).status_code == 201

        await c.exactly_one(E.TOKEN_MINT, mint)

        box: dict = {}

        async def seed() -> None:
            box["s"] = await seed_session(w, worker, "ACTIVE")

        # Start and commit: one each for one Start (the seeding goes through both routes)
        before = {e: await c.count(e) for e in (E.START, E.COMMIT)}
        await seed()
        for event, n in before.items():
            assert await c.count(event) == n + 1, event
            c.seen.add(event)
        s = box["s"]
        notice = await pg.run(
            "super",
            "SELECT data->>'notice_version' FROM assess_audit_events "
            "WHERE event = 'assess_start' AND session_id = :s",
            {"s": s.session_id},
        )
        assert notice[0][0] is not None

        async def turn_into_outage() -> None:
            r = await w.client.post(
                f"/api/v1/assess/sessions/{s.session_id}/turns",
                json={"text": "x"},
                headers=bearer(s.session_token),
            )
            assert r.status_code == 202
            await wait_idle(w)

        await c.exactly_one(E.OUTAGE_START, turn_into_outage)

        async def end_outage() -> None:
            async def ok() -> bool:
                return True

            await outage_tick(worker.connect, ok, min_credit_s=20)

        await c.exactly_one(E.OUTAGE_END, end_outage)
        credited = await pg.run(
            "super",
            "SELECT data ? 'credited_s' FROM assess_audit_events "
            "WHERE event = 'assess_outage_end' AND session_id = :s",
            {"s": s.session_id},
        )
        assert credited[0][0] is True

        other = await seed_session(w, worker, "ACTIVE", assignment="scale")

        async def ownership() -> None:
            r = await w.client.get(
                f"/api/v1/assess/sessions/{s.session_id}",
                headers=bearer(other.session_token),
            )
            assert r.status_code == 404

        await c.exactly_one(E.OWNERSHIP_MISMATCH_REJECTED, ownership)

        async def budget() -> None:
            runner.fail = None
            await pg.run(
                "super",
                "UPDATE assess_sessions SET tokens_used = 7900, pending_reply_since = NULL "
                "WHERE id = :s",
                {"s": other.session_id},
            )
            r = await w.client.post(
                f"/api/v1/assess/sessions/{other.session_id}/turns",
                json={"text": "z"},
                headers=bearer(other.session_token),
            )
            assert r.status_code == 202
            await wait_idle(w)

        await c.exactly_one(E.BUDGET_WARNING, budget)

        async def close() -> None:
            r = await w.client.post(
                f"/api/v1/assess/sessions/{other.session_id}/finish",
                headers=bearer(other.session_token),
            )
            assert r.status_code == 200

        await c.exactly_one(E.CLOSE, close)
        reason = await pg.run(
            "super",
            "SELECT reason_code FROM assess_audit_events WHERE event = 'assess_close' "
            "AND session_id = :s",
            {"s": other.session_id},
        )
        assert [r[0] for r in reason] == ["finished"]

        async def gate_rejected() -> None:
            # the closed session's token on the turn route: one in-session gate rejection
            r = await w.client.post(
                f"/api/v1/assess/sessions/{other.session_id}/turns",
                json={"text": "w"},
                headers=bearer(other.session_token),
            )
            assert r.status_code == 409

        await c.exactly_one(E.IN_SESSION_GATE_REJECTED, gate_rejected)

        limits = StartLimits(rollback_notify_after=1, breaker_rollbacks=1000)

        async def rollback() -> None:
            st = await seed_session(
                w, worker, "STARTING", assignment="rubric_vis", user="stu2"
            )
            async with worker.begin() as conn:
                assert await rollback_start(
                    conn, st.session_id, "internal_error", limits=limits
                )

        before = {e: await c.count(e) for e in (E.START_ROLLBACK, E.ROLLBACK_CAP_HIT)}
        await rollback()
        for event, n in before.items():
            assert await c.count(event) == n + 1, event
            c.seen.add(event)

        async def rate_cap() -> None:
            u, cm = fixture_target(name="rubric_vis", user="stu2")
            for _ in range(5):
                await pg.run(
                    "super",
                    "INSERT INTO assess_start_attempts (site_id, cmid, student_userid, outcome) "
                    "VALUES (:s, :c, :u, 'requested')",
                    {"s": w.site, "c": cm, "u": u},
                )
            token = await w.presession(sub=u, cmid=cm)
            r = await w.client.post("/api/v1/assess/sessions", headers=bearer(token))
            assert r.status_code == 429

        await c.exactly_one(E.START_RATE_CAP_HIT, rate_cap)

    # every event of the enum is driven here, elsewhere, or documented as not drivable
    assert set(E) == c.seen | set(ELSEWHERE) | NOT_DRIVABLE
    for event, module in ELSEWHERE.items():
        if event is E.FLAG_ADDED and not (HERE / module).exists():
            continue  # REQ-103 (could) may be time-boxed out: then M5.9 records it
        assert event.value in (HERE / module).read_text(), (event, module)


async def test_an_audit_store_failure_aborts_the_action(pg, worker) -> None:  # type: ignore[no-untyped-def]
    """Fail closed: the Start's audit insert fails -> no session, no job, the jti unconsumed."""
    async with world(pg) as w:
        await seed_session(w, worker, "ELIGIBLE", assignment="scale")
        sub, cmid = fixture_target(name="scale")
        token = await w.presession(sub=sub, cmid=cmid)
        await pg.run(
            "super",
            "CREATE FUNCTION assess_test_audit_down() RETURNS trigger LANGUAGE plpgsql AS "
            "$$ BEGIN IF NEW.event = 'assess_start' THEN RAISE EXCEPTION 'audit store down'; "
            "END IF; RETURN NEW; END $$",
        )
        await pg.run(
            "super",
            "CREATE TRIGGER assess_test_audit_down BEFORE INSERT ON assess_audit_events "
            "FOR EACH ROW EXECUTE FUNCTION assess_test_audit_down()",
        )
        counts = (
            "SELECT (SELECT count(*) FROM assess_sessions WHERE student_userid = :u AND cmid = :c), "
            "(SELECT count(*) FROM assess_jobs WHERE kind = 'preflight'), "
            "(SELECT count(*) FROM assess_token_jti WHERE consumed_at IS NOT NULL)"
        )
        try:
            before = tuple((await pg.run("super", counts, {"u": sub, "c": cmid}))[0])
            # the failing insert either surfaces as a database error through the in-process
            # client, or as a 5xx response; anything else (a 2xx, a 4xx) fails the test
            try:
                r = await w.client.post(
                    "/api/v1/assess/sessions", headers=bearer(token)
                )
            except DBAPIError:
                r = None
            if r is not None:
                assert r.status_code >= 500, r.text
            after = tuple((await pg.run("super", counts, {"u": sub, "c": cmid}))[0])
            assert after == before
        finally:
            await pg.run(
                "super", "DROP TRIGGER assess_test_audit_down ON assess_audit_events"
            )
            await pg.run("super", "DROP FUNCTION assess_test_audit_down()")
