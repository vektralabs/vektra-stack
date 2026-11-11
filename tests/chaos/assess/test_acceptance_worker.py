"""Acceptance criteria of plan assess-job-loop that need the real worker process.

Each test states the criterion it proves. Evidence is rows and exit codes, never a status field.
"""

from __future__ import annotations

import asyncio
import subprocess
import time
from pathlib import Path

import pytest
from harness import (
    ROLE_PASSWORD,
    WORKER_MAIN,
    counts,
    enqueue,
    reset_tables,
    start_worker,
    wait_until,
    worker_ready,
    worker_secrets,
)

pytestmark = pytest.mark.integration


async def test_saturated_ws_pool_does_not_starve_other_pools_or_stop_the_heartbeat(
    pg, tmp_path: Path
):
    """COMP-003: with the ws pool full of stalled handlers, other pools keep claiming up to their
    own concurrency, and ticks keep firing (the heartbeat row never ages past 10 s)."""
    await reset_tables(pg)
    secrets = worker_secrets(tmp_path / "secrets")
    # four stalled ws jobs fill the ws pool (concurrency 2): two run, two wait
    await pg.run(
        "super",
        "INSERT INTO assess_jobs (kind, args, max_attempts) SELECT 'gate_refresh', "
        "jsonb_build_object('n', g), 1 FROM generate_series(1, 4) g",
    )
    await enqueue(pg, 30, sleep_ms=100)  # maintenance pool work, concurrency 6
    w = start_worker(
        pg,
        tmp_path,
        "a",
        secrets,
        chaos_dir=tmp_path / "chaos",
        CHAOS_STALL_WS="1",
        VEKTRA_ASSESS_JOB_GATE_REFRESH_BUDGET_S="600",
        VEKTRA_ASSESS_JOB_GATE_REFRESH_LEASE_S="1200",
    )
    try:
        await wait_until(lambda: worker_ready(pg), 90, "worker ready", w)

        async def stalled_two() -> bool:
            return len(list(w.chaos_dir.glob("stalled-*"))) == 2

        await wait_until(stalled_two, 30, "two ws handlers stalled (pool full)", w)

        async def effects_done() -> bool:
            return (await counts(pg))["effects"] == 30

        worst_age = 0.0
        deadline = time.monotonic() + 60
        while not await effects_done():
            assert time.monotonic() < deadline, (
                f"maintenance starved: {await counts(pg)}"
            )
            age = (
                await pg.run(
                    "super",
                    "SELECT extract(epoch FROM now() - beat_at) FROM assess_heartbeat",
                )
            )[0][0]
            worst_age = max(worst_age, float(age))
            await asyncio.sleep(0.2)
        assert (await counts(pg))["effects_distinct"] == 30
        assert worst_age < 10, (
            f"the heartbeat row aged to {worst_age:.1f}s while ws was saturated"
        )
        assert len(list(w.chaos_dir.glob("stalled-*"))) == 2, (
            "the ws pool stayed at its cap of 2"
        )
    finally:
        w.stop()


async def test_no_claim_before_reconciliation_completes(pg, tmp_path: Path):
    """Reconciliation (audit of exhausted leases) is written before the first job runs.

    Both clocks are the database's (``now()`` in the audit row and in the effect row)."""
    await reset_tables(pg)
    secrets = worker_secrets(tmp_path / "secrets")
    # an exhausted job whose lease expired: reconciliation must audit and kill it
    await pg.run(
        "super",
        "INSERT INTO assess_jobs (kind, state, lease_until, leased_by, attempts, max_attempts) "
        "VALUES ('purge', 'leased', now() - interval '1 minute', 'dead-worker', 1, 1)",
    )
    audit_before = (await pg.run("super", "SELECT count(*) FROM assess_audit_events"))[
        0
    ][0]
    await enqueue(pg, 5, sleep_ms=50)
    w = start_worker(pg, tmp_path, "a", secrets, chaos_dir=tmp_path / "chaos")
    try:

        async def five_done() -> bool:
            return (await counts(pg))["effects"] == 5

        await wait_until(five_done, 60, "jobs ran", w)
        audit_at = (
            await pg.run(
                "super",
                "SELECT max(created_at) FROM assess_audit_events "
                "WHERE event = 'assess_job_dead' AND reason_code = 'lease_expired_exhausted'",
            )
        )[0][0]
        first_effect = (
            await pg.run("super", "SELECT min(first_at) FROM assess_chaos_effects")
        )[0][0]
        assert audit_at is not None, "reconciliation did not audit the exhausted job"
        assert (await pg.run("super", "SELECT count(*) FROM assess_audit_events"))[0][
            0
        ] == audit_before + 1
        assert audit_at <= first_effect, (
            f"a job ran ({first_effect}) before reconciliation ({audit_at})"
        )
        dead = (
            await pg.run("super", "SELECT count(*) FROM assess_jobs WHERE state='dead'")
        )[0][0]
        assert dead == 1
    finally:
        w.stop()


def _run_worker_once(
    pg, tmp: Path, secrets: Path, **env: str
) -> subprocess.CompletedProcess[str]:
    """Run the worker entry to completion (it must refuse to start) and return its result."""
    import os
    import sys

    from sqlalchemy.engine import URL, make_url

    u = make_url(pg.super_url)
    dsn = URL.create(
        u.drivername, "vektra_assess_worker", None, u.host, u.port, u.database
    ).render_as_string(hide_password=False)
    full = {
        **{k: v for k, v in os.environ.items() if not k.startswith("VEKTRA_")},
        "VEKTRA_ENV": "development",
        "VEKTRA_ASSESS_ENABLED": "true",
        "VEKTRA_ASSESS_LMS_PROVIDER": "fake",  # worker_main.py's development-only provider
        "VEKTRA_ASSESS_SECRETS_DIR": str(secrets),
        "VEKTRA_ASSESS_WORKER_DATABASE_URL": dsn,
        "VEKTRA_ASSESS_WORKER_HEARTBEAT_FILE": str(tmp / "hb-refuse"),
        "CHAOS_DIR": str(tmp),
        **env,
    }
    return subprocess.run(
        [sys.executable, str(WORKER_MAIN)],
        env=full,
        capture_output=True,
        text=True,
        timeout=120,
    )


@pytest.mark.parametrize(
    ("label", "env", "needle"),
    [
        (
            "a production slot is unbound",
            # a relay is configured (plan 10b refuses to start without one before the slots
            # are checked), the unbound override is not set
            {
                "VEKTRA_ASSESS_SMTP_HOST": "relay.example.org",
                "VEKTRA_ASSESS_SMTP_FROM": "vektra@example.org",
                "VEKTRA_ASSESS_OPERATOR_EMAIL": "ops@example.org",
            },
            "job:transcript_generate",
        ),
        (
            "lease shorter than twice the budget",
            {
                "VEKTRA_ASSESS_WORKER_ALLOW_UNBOUND": "true",
                "VEKTRA_ASSESS_JOB_PURGE_BUDGET_S": "60",
                "VEKTRA_ASSESS_JOB_PURGE_LEASE_S": "100",
            },
            "at least 2 x budget_s",
        ),
        (
            "a forbidden module is loaded",
            {
                "VEKTRA_ASSESS_WORKER_ALLOW_UNBOUND": "true",
                "CHAOS_IMPORT_FORBIDDEN": "torch",
            },
            "forbidden modules loaded",
        ),
        (
            "the unbound override outside development",
            # outside development only a real provider loads (the fake is a test double)
            {
                "VEKTRA_ASSESS_WORKER_ALLOW_UNBOUND": "true",
                "VEKTRA_ENV": "production",
                "VEKTRA_ASSESS_LMS_PROVIDER": "moodle",
            },
            "only honoured with",
        ),
        # ADR-0033 D1: assess enabled and no LMS provider named: the vertical is unavailable
        (
            "no LMS provider",
            {"VEKTRA_ASSESS_LMS_PROVIDER": ""},
            "VEKTRA_ASSESS_LMS_PROVIDER is unset",
        ),
        # the development-only entry point offers a test double: never outside development
        (
            "the fake LMS provider outside development",
            {"VEKTRA_ENV": "production"},
            "is a test double",
        ),
    ],
)
def test_worker_refuses_to_start_with_a_structured_startup_error(
    pg, tmp_path: Path, label: str, env: dict[str, str], needle: str
):
    """Acceptance: non-zero exit and a structured ``[STARTUP ERROR]`` for each refusal."""
    secrets = worker_secrets(tmp_path / "secrets")
    res = _run_worker_once(pg, tmp_path, secrets, **env)
    assert res.returncode == 1, (label, res.returncode, res.stdout, res.stderr)
    assert "[STARTUP ERROR]" in res.stderr, (label, res.stderr)
    assert needle in res.stderr, (label, res.stderr)
    assert ROLE_PASSWORD not in res.stderr and ROLE_PASSWORD not in res.stdout


async def test_500_jobs_through_the_real_worker_never_exceed_six_connections(
    pg, tmp_path: Path
):
    """INT-002: ``pg_stat_activity`` count for the worker never exceeds 6 during the load."""
    await reset_tables(pg)
    secrets = worker_secrets(tmp_path / "secrets")
    n = 500
    await enqueue(pg, n, sleep_ms=5)
    w = start_worker(pg, tmp_path, "a", secrets, chaos_dir=tmp_path / "chaos")
    peak = 0
    try:
        await wait_until(lambda: worker_ready(pg), 90, "worker ready", w)
        deadline = time.monotonic() + 180
        while (await counts(pg))["done"] < n:
            assert time.monotonic() < deadline, (
                f"load did not finish: {await counts(pg)}"
            )
            conns = (
                await pg.run(
                    "super",
                    "SELECT count(*) FROM pg_stat_activity "
                    "WHERE application_name LIKE 'vektra-assess-worker%'",
                )
            )[0][0]
            peak = max(peak, int(conns))
            await asyncio.sleep(0.05)
        c = await counts(pg)
        assert (c["done"], c["effects"], c["effects_distinct"], c["dead"]) == (
            n,
            n,
            n,
            0,
        )
        assert 1 <= peak <= 6, f"the worker held {peak} connections (budget 5 + 1)"
        print(f"CHAOS peak worker connections over {n} jobs: {peak}")
    finally:
        w.stop()
