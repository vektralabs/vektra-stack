"""The gate_refresh schedule over the whole supported render freshness (10 s .. 1 day)."""

from __future__ import annotations

import pytest

from vektra_assess.config import VektraAssessSettings
from vektra_assess.worker import render
from vektra_assess.worker.slots import _tick_table


@pytest.mark.parametrize(
    "fresh", [10, 11, 15, 60, 121, 299, 300, 301, 900, 3600, 86_400]
)
def test_a_pair_refreshed_early_is_refreshed_again_before_it_goes_stale(
    fresh: int,
) -> None:
    period, jitter = render.tick_period(fresh), render.tick_jitter(fresh)
    assert 0 <= jitter < period  # TickSpec's own rule
    early = render.early_threshold(fresh)
    # the worst case: the row is just under ``early`` at a tick, the next tick comes a whole
    # period plus jitter later, and its refresh job needs the margin to finish
    assert early + period + jitter + render._margin(fresh) <= fresh


@pytest.mark.parametrize("fresh", [10, 121, 300, 86_400])
def test_the_worker_tick_table_builds_at_every_freshness(fresh: int) -> None:
    s = VektraAssessSettings.model_validate({"VEKTRA_ASSESS_RENDER_FRESH_S": fresh})
    (tick,) = [t for t in _tick_table(s) if t.name == "gate_refresh"]
    assert tick.interval_s == render.tick_period(fresh)
    assert tick.jitter_s == render.tick_jitter(fresh)
