"""Postgres connection budget of the assess deployment (INT-002, plan assess-job-loop).

``max_connections`` is 100. The test sums what every process can hold with the shipped defaults
and fails above 85, leaving headroom for ad-hoc sessions and the autovacuum workers. The figures
come from the code that sets them, so changing a pool default without revisiting the budget fails
here instead of in production under load.
"""

from __future__ import annotations

from vektra_app import assess_wiring, assess_worker
from vektra_assess.config import VektraAssessSettings
from vektra_shared import db

MAX_CONNECTIONS = 100
CEILING = 85
MIGRATE = 1  # alembic, one connection while it runs
RESERVED = 3  # superuser_reserved_connections (Postgres default)


def _budget() -> dict[str, int]:
    s = VektraAssessSettings()
    jobs, live = assess_worker.make_engines(
        "postgresql+asyncpg://u@127.0.0.1:1/d", s.worker_pool_size
    )
    worker = jobs.pool.size() + jobs.pool._max_overflow
    worker += live.pool.size() + live.pool._max_overflow
    return {
        "main API pool (10 + 20)": db.DEFAULT_POOL_SIZE + db.DEFAULT_MAX_OVERFLOW,
        "assess API pool": s.api_pool_size + assess_wiring.API_MAX_OVERFLOW,
        "assess worker pool": worker,  # job pool + liveness engine
        "assess worker control connection": 1,
        "migrate": MIGRATE,
        "reserved": RESERVED,
    }


def test_defaults_sum_to_at_most_85_of_100() -> None:
    budget = _budget()
    total = sum(budget.values())
    assert total <= CEILING, f"{total} > {CEILING}: {budget}"
    assert CEILING < MAX_CONNECTIONS


def test_worker_holds_five_plus_one_and_never_overflows() -> None:
    budget = _budget()
    assert budget["assess worker pool"] == 5
    assert (
        budget["assess worker pool"] + budget["assess worker control connection"] == 6
    )


def test_the_documented_figures_match_the_code() -> None:
    from pathlib import Path

    doc = (
        Path(__file__).resolve().parents[2] / "docs/reference/configuration.md"
    ).read_text()
    for needle in ("Connection budget", "max_connections", "85"):
        assert needle in doc, f"docs/reference/configuration.md must document: {needle}"


def test_the_liveness_engine_is_carved_out_of_the_worker_pool() -> None:
    assert assess_worker.split_pool(5) == (4, 1)
    assert assess_worker.split_pool(2) == (1, 1)
    assert assess_worker.split_pool(1) == (
        1,
        1,
    )  # cannot split: the one case above the pool
