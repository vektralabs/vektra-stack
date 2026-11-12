"""Sink 6 of the canary matrix on real Postgres (NFR-017): the vertical's capture over the Moodle
provider's recorded reviews, persisted, then every audit row and a data-only dump of every assess
table scanned for the forbidden canaries (the joint guarantee of ADR-0033 D4).
"""

from __future__ import annotations

import os

import pytest

from .conftest import Pg, dump_data, make_session

pytestmark = pytest.mark.integration


async def test_canary_matrix_subset_leaves_no_canary_in_any_row(pg: Pg) -> None:
    """Sink 6 on real Postgres: persist one case per kind and shape, then scan the audit rows and
    a data-only dump of every assess table for the forbidden canaries."""
    from canary.matrix import build_matrix
    from canary.runner import run_case

    from vektra_assess.context.capture.capture import persist_capture
    from vektra_testkit.recorders import find_canaries

    picked = {}
    for c in build_matrix():
        key = (c.kind, c.shape or c.review)
        if c.version == "5.2" and not c.toggle and key not in picked:
            picked[key] = c
    forbidden: set[str] = set()
    persisted = 0
    for case in picked.values():
        out = await run_case(case)
        forbidden |= set(case.forbidden)
        if out.built is None:
            continue
        sid = await make_session(pg, "STARTING")
        async with pg.conn("worker") as c:
            await persist_capture(c, sid, os.urandom(32), out.built)
            await c.commit()
        persisted += 1
    assert persisted >= 8 and forbidden
    rows = await pg.run("super", "SELECT data::text FROM assess_audit_events")
    assert rows, "the audit sink is not empty"
    assert sum(find_canaries([r[0] for r in rows], forbidden).values()) == 0
    dump = dump_data(pg)
    assert "assess_snapshots" in dump
    assert sum(find_canaries(dump, forbidden).values()) == 0
