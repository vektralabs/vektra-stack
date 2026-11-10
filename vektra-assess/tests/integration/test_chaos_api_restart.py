"""Chaos: the API restarts with live sessions and resumes from the hand-off row (plan 13a, ADR-0031).

An API restart loses every in-memory session key: the DEK cache is process state. The cache is
dropped here exactly as a new process would start it, absent and created on first use. No worker
runs. The session must still read its full history, because the DEK is unsealed from the
``assess_session_handoff`` row with the API's own key, and that is the only path: without the row
the same read fails (negative control). Rows: 0 jobs queued, the hand-off row unchanged, the turn
count unchanged.
"""

from __future__ import annotations

import sys
from pathlib import Path
from typing import Any

import pytest
from support.sessions import seed_session
from support.world import world

from integration.test_delivery_handler import worker  # noqa: F401  (the fixture)

sys.path.insert(
    0, str(Path(__file__).resolve().parents[3] / "tests" / "chaos" / "assess")
)
from invariants import assert_invariants

pytestmark = pytest.mark.integration


def bearer(t: str) -> dict[str, str]:
    return {"Authorization": f"Bearer {t}"}


async def _rows(pg: Any, sid: Any) -> tuple[int, int, int]:
    r = (
        await pg.run(
            "super",
            "SELECT (SELECT count(*) FROM assess_jobs), "
            "(SELECT count(*) FROM assess_session_handoff WHERE session_id = :s), "
            "(SELECT count(*) FROM assess_turns WHERE session_id = :s)",
            {"s": sid},
        )
    )[0]
    return int(r[0]), int(r[1]), int(r[2])


async def test_a_restarted_api_resumes_live_sessions_from_the_handoff_row(
    pg,
    worker,  # noqa: F811  (the fixture imported above)
) -> None:  # type: ignore[no-untyped-def]
    async with world(pg) as w:
        s = await seed_session(w, worker, "ACTIVE")
        url = f"/api/v1/assess/sessions/{s.session_id}"
        first = await w.client.get(url, headers=bearer(s.session_token))
        assert first.status_code == 200, first.text
        turns = first.json()["turns"]
        before = await _rows(pg, s.session_id)
        assert before[1] == 1 and before[2] == len(turns) >= 1

        # the restart: the process-local DEK cache is gone, nothing else survives in memory
        assert getattr(w.app.state, "assess_dek_cache", None) is not None
        del w.app.state.assess_dek_cache
        again = await w.client.get(url, headers=bearer(s.session_token))
        assert again.status_code == 200, again.text
        assert again.json()["turns"] == turns
        assert (
            await _rows(pg, s.session_id) == before
        )  # no job, hand-off and turns intact
        await assert_invariants(pg, "api-restart:resume", expect_sessions=1)

        # negative control: a second restart with the hand-off row gone cannot read the history
        del w.app.state.assess_dek_cache
        await pg.run(
            "super",
            "DELETE FROM assess_session_handoff WHERE session_id = :s",
            {"s": s.session_id},
        )
        blind = await w.client.get(url, headers=bearer(s.session_token))
        assert blind.status_code != 200
        assert "turns" not in blind.text or blind.json().get("turns") is None
