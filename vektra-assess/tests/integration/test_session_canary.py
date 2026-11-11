"""M5.7: no snapshot or turn content leaves the encrypted tables (NFR-016 canary grep).

A whole lifecycle runs with canaries in the student's messages and in the model's replies, plus a
snapshot canary taken from the decrypted opening turn (Part A is rendered from the snapshot):
Start, commit, a turn, an outage and its end, the server-side answer, a budget warning, Fine.
Then every place that is not the ciphertext is searched: the audit rows, every structlog event at
every level (captured, not only WARNING+), the Prometheus exposition and the job arguments.
"""

from __future__ import annotations

import asyncio
from datetime import UTC, datetime

import pytest
import structlog
from prometheus_client import REGISTRY, generate_latest
from sqlalchemy.ext.asyncio import create_async_engine
from support.preflight import FakeStreamRunner
from support.sessions import seed_session
from support.world import default_settings, world

from integration.test_session_preflight import _reset
from vektra_assess.api.pending import PendingTurnDriver
from vektra_assess.llm.types import ProviderCallRecord, ProviderOutageError
from vektra_assess.worker.outage_probe import outage_tick

pytestmark = pytest.mark.integration

STUDENT = "CANARY-student-7c1e"
BOT = "CANARY-bot-reply-93ad"


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


async def ok() -> bool:
    return True


async def test_no_canary_outside_the_ciphertext(pg, worker) -> None:  # type: ignore[no-untyped-def]
    now = datetime.now(UTC)
    runner = FakeStreamRunner(segments=(f"{BOT} uno. ", f"{BOT} due."))
    settings = default_settings(VEKTRA_ASSESS_SESSION_TOKEN_BUDGET=10_000)
    with structlog.testing.capture_logs() as logs:
        async with world(pg, settings, assess_runner=runner) as w:
            s = await seed_session(w, worker, "ACTIVE")
            url = f"/api/v1/assess/sessions/{s.session_id}"
            got = (await w.client.get(url, headers=bearer(s.session_token))).json()
            opening = got["turns"][0]["text"]
            snapshot_canary = max(opening.split("\n"), key=len)[:40]
            assert len(snapshot_canary) >= 20
            # a turn answered, then an outage, its end and the server-side answer
            r = await w.client.post(
                f"{url}/turns",
                json={"text": f"{STUDENT} uno"},
                headers=bearer(s.session_token),
            )
            assert r.status_code == 202
            await wait_idle(w)
            runner.fail = ProviderOutageError(
                ProviderCallRecord(now, None, now, "error")
            )
            r = await w.client.post(
                f"{url}/turns",
                json={"text": f"{STUDENT} due"},
                headers=bearer(s.session_token),
            )
            assert r.status_code == 202
            await wait_idle(w)
            runner.fail = None
            await pg.run(
                "super",
                "UPDATE assess_sessions SET tokens_used = 7900 WHERE id = :s",
                {"s": s.session_id},
            )
            await outage_tick(worker.connect, ok, min_credit_s=20)
            await PendingTurnDriver(w.app, min_age_s=0).run_once()
            await wait_idle(w)
            await w.client.post(f"{url}/retry", headers=bearer(s.session_token))
            r = await w.client.post(f"{url}/finish", headers=bearer(s.session_token))
            assert r.status_code == 200
    # the lifecycle really carried the canaries (in the ciphertext only)
    assert len(runner.turns) >= 2 and STUDENT in runner.turns[0].text

    audit = "\n".join(
        str(r[0])
        for r in await pg.run(
            "super", "SELECT row_to_json(a)::text FROM assess_audit_events a"
        )
    )
    jobs = "\n".join(
        str(r[0]) for r in await pg.run("super", "SELECT args::text FROM assess_jobs")
    )
    traces = "\n".join(
        str(r[0])
        for r in await pg.run(
            "super", "SELECT row_to_json(t)::text FROM query_traces t"
        )
    )
    log_text = "\n".join(repr(e) for e in logs)
    metrics = generate_latest(REGISTRY).decode()
    assert logs, "the capture saw the lifecycle's log events"
    for where, blob in (
        ("audit", audit),
        ("jobs", jobs),
        ("query_traces", traces),
        ("structlog", log_text),
        ("metrics", metrics),
    ):
        for canary in (STUDENT, BOT, snapshot_canary):
            assert canary not in blob, (where, canary)


SOLUTION_CANARY = "CANARY-solution-toggle-off-5b2d"


async def test_hidden_lms_content_and_a_toggled_off_solution_never_reach_the_session(
    pg, worker
) -> None:  # type: ignore[no-untyped-def]
    """NFR-015 / NFR-017 on the fake LMS (ADR-0033 D2, D4): the quiz review the provider hands
    over carries its right answer and hidden feedback (canaries, in the classes the provider's
    profile drops), and the activity has a sealed solution with ``show_solution`` OFF. The
    session commits, and neither the LMS canaries nor the solution reach the opening turns, the
    audit rows, the job arguments or any log event. The solution is never fetched: its stored
    blob cannot be opened by this pre-flight (no key), so a fetch would have failed it closed."""
    from support.fake_lms import SCENARIO_CANARIES, scenario_provider, scenario_target
    from support.world import configure_activity

    from vektra_assess.crypto.teacher_config import SOLUTION

    fake = scenario_provider()
    _sub, cmid = scenario_target("essay")
    with structlog.testing.capture_logs() as logs:
        async with world(pg) as w:
            await configure_activity(pg, w.site, cmid, kind="quiz")
            await pg.run(
                "super",
                "INSERT INTO assess_activity_config_sealed (site_id, cmid, field, "
                "sealed_blob, kid) VALUES (:s, :c, :f, :b, 'k1')",
                {"s": w.site, "c": cmid, "f": SOLUTION, "b": SOLUTION_CANARY.encode()},
            )
            s = await seed_session(
                w, worker, "ACTIVE", assignment="essay", provider=fake
            )
            got = (
                await w.client.get(
                    f"/api/v1/assess/sessions/{s.session_id}",
                    headers=bearer(s.session_token),
                )
            ).json()
            turns = "\n".join(t["text"] for t in got["turns"])
    # positive controls: the review with the canaries was read, and Part A was rendered
    assert fake.count("attempt_review") >= 1 and len(got["turns"]) == 2
    audit = "\n".join(
        str(r[0])
        for r in await pg.run(
            "super", "SELECT row_to_json(a)::text FROM assess_audit_events a"
        )
    )
    jobs = "\n".join(
        str(r[0]) for r in await pg.run("super", "SELECT args::text FROM assess_jobs")
    )
    log_text = "\n".join(repr(e) for e in logs)
    for where, blob in (
        ("turns", turns),
        ("audit", audit),
        ("jobs", jobs),
        ("structlog", log_text),
    ):
        for canary in (*SCENARIO_CANARIES.values(), SOLUTION_CANARY):
            assert canary not in blob, (where, canary)
