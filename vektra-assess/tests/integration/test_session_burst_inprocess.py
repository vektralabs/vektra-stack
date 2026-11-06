"""M1.11: the burst script's logic against the in-process stack and FAKES (not the NFR evidence).

40 concurrent Starts through the real API routes on a real Postgres, the real pre-flight handler
processing the jobs (fake Part B runner, one recorded snapshot reused for every student), admission
K = 10. It checks what does not depend on the target model: every Start gets an outcome, exactly K
sessions commit, every other Start is refused at the API or rolled back with ``capacity``, and the
refused ones consumed nothing (row counts). It writes no file under ``tests/nfr/results``: the NFR-018
/ NFR-020 evidence comes only from the real burst (``tests/nfr/test_assess_burst.py``).
"""

from __future__ import annotations

import asyncio
import importlib.util
import sys
import uuid
from pathlib import Path

import pytest
from assess_moodle_support import Replay, make_client
from sqlalchemy import text
from sqlalchemy.ext.asyncio import create_async_engine
from support.preflight import FakePartB, FakeSource, activate, deps, job_context
from support.sessions import fixture_target
from support.world import configure_activity, default_settings, world
from test_context_capture import InProcessParser

from integration.test_session_preflight import M, _reset
from vektra_assess.worker.preflight import StartTarget, make_preflight_handler
from vektra_assess.worker.start_source import MoodleStartSource

pytestmark = pytest.mark.integration

SCRIPT = Path(__file__).resolve().parents[3] / "scripts" / "assess_burst.py"
K, N = 10, 40


def _script():  # type: ignore[no-untyped-def]
    spec = importlib.util.spec_from_file_location("assess_burst_inproc", SCRIPT)
    assert spec is not None and spec.loader is not None
    mod = importlib.util.module_from_spec(spec)
    sys.modules[spec.name] = mod
    spec.loader.exec_module(mod)
    return mod


async def test_forty_starts_admit_k_and_refuse_the_rest_without_consuming(pg) -> None:  # type: ignore[no-untyped-def]
    burst = _script()
    await _reset(pg)
    await activate(pg)
    worker = create_async_engine(pg.url("worker"))
    settings = default_settings(VEKTRA_ASSESS_ADMISSION_K=K)
    try:
        async with world(pg, settings) as w:
            sub, cmid = fixture_target()
            await pg.run(
                "super",
                "UPDATE assess_sites SET release = :r WHERE id = :s",
                {"r": M["release"], "s": w.site},
            )
            await configure_activity(pg, w.site, cmid)
            # one real capture (stu1's recorded submission), reused for every fake student
            real = MoodleStartSource(
                make_client(Replay("4.3")), InProcessParser(), worker.connect
            )
            built = await real.capture(
                StartTarget(uuid.uuid4(), w.site, cmid, sub, None, "assign", 1, {})
            )
            handler = make_preflight_handler(
                deps(settings, w.handoff_public, FakeSource(built), FakePartB())
            )
            stop = asyncio.Event()

            async def worker_loop() -> None:
                done: set[uuid.UUID] = set()
                while not stop.is_set():
                    rows = await pg.run(
                        "super",
                        "SELECT id FROM assess_sessions WHERE state = 'STARTING' "
                        "ORDER BY created_at",
                    )
                    for (sid,) in rows:
                        if sid not in done:
                            done.add(sid)
                            await handler(job_context(worker, sid))
                    await asyncio.sleep(0.05)

            loop = asyncio.create_task(worker_loop())
            students = list(range(5000, 5000 + N))

            async def counts(outcomes):  # type: ignore[no-untyped-def]
                fn = await burst.db_counts(pg.super_url)
                return await fn(outcomes)

            doc = await burst.run_burst(
                w.client,
                burst=burst.Burst(cmid, None, students, 10),
                mint_key="mint",
                model="fake/part-b",
                counts=counts,
                timeout_s=60,
                poll_s=0.1,
            )
            stop.set()
            await loop
        assert burst.validate_result(doc) == [], doc["summary"]
        kinds = doc["summary"]["outcomes_by_kind"]
        assert kinds.get("commit") == K, kinds
        others = [o["outcome"] for o in doc["outcomes"] if o["outcome"] != "commit"]
        assert len(others) == N - K
        assert all(o in ("capacity_refused", "rolled_back:capacity") for o in others), (
            others
        )
        assert doc["row_counts"]["sessions_by_state"] == {"ACTIVE": K}
        assert doc["row_counts"]["assess_snapshots"] == K
        assert doc["row_counts"]["assess_session_keys"] == K
        assert doc["row_counts"]["assess_session_handoff"] == K
        assert doc["summary"]["commit_latency_s"]["n"] == K  # from the audit timestamps
        consumed = (
            await pg.run(
                "super",
                "SELECT count(*) FROM assess_sessions WHERE student_userid >= 5000 "
                "AND student_userid < 5040",
            )
        )[0][0]
        assert consumed == K  # refused Starts left no session row behind
        async with worker.begin() as conn:
            await conn.execute(
                text(
                    "UPDATE assess_sessions SET state = 'CLOSED', close_reason = 'finished' "
                    "WHERE student_userid >= 5000 AND student_userid < 5040"
                )
            )
    finally:
        await worker.dispose()
