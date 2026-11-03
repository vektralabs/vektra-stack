"""Idle memory of the worker (ARCH-073, plan assess-job-loop): at most 300 MB, measured, recorded.

Starts the real worker process with every production slot bound to a stub, WITHOUT the development
override (so it also proves that a fully bound worker starts), waits for ``ready`` plus a settle
window, and reads VmRSS and VmHWM from ``/proc`` on Linux (the CI integration job); elsewhere it
reads RSS through psutil, which is informational only (macOS counts memory differently).

It also records what ``litellm`` costs: the same interpreter with the worker's modules imported with
and without it. That figure is an input to the cost evaluation of plans 7 and 8 (the LLM runner).
"""

from __future__ import annotations

import asyncio
import os
import sys
import time
from pathlib import Path

import pytest
from harness import (
    interpreter_rss_mb,
    reset_tables,
    rss_mb,
    start_worker,
    wait_until,
    worker_ready,
    worker_secrets,
)

pytestmark = pytest.mark.integration

CEILING_MB = 300
SETTLE_S = float(os.environ.get("CHAOS_RSS_SETTLE_S", "60"))
ON_LINUX = sys.platform.startswith("linux")

_WORKER_MODULES = (
    "import vektra_assess.worker.runtime, vektra_assess.worker.ticks, vektra_assess.worker.listener, "
    "vektra_assess.worker.reconcile, vektra_assess.worker.slots, vektra_assess.worker.shutdown, "
    "vektra_assess.worker.heartbeat, vektra_assess.worker.cleanup, vektra_assess.worker.audit, "
    "vektra_assess.worker.watchdog, vektra_assess.worker.policy, vektra_assess.config\n"
)


async def test_idle_rss_of_a_fully_bound_worker_is_under_the_ceiling(
    pg, tmp_path: Path
):
    await reset_tables(pg)
    secrets = worker_secrets(tmp_path / "secrets")
    w = start_worker(
        pg,
        tmp_path,
        "rss",
        secrets,
        chaos_dir=tmp_path / "chaos",
        CHAOS_BIND_ALL="1",
        VEKTRA_ASSESS_WORKER_ALLOW_UNBOUND="false",  # the real fail-closed path must pass
    )
    try:
        await wait_until(lambda: worker_ready(pg), 120, "fully bound worker ready", w)
        t0 = time.monotonic()
        while time.monotonic() - t0 < SETTLE_S:
            assert w.proc.poll() is None, f"the worker died while idling: {w.output()}"
            await asyncio.sleep(1)
        rss, hwm = rss_mb(w.proc.pid)
    finally:
        w.stop()

    without = interpreter_rss_mb(_WORKER_MODULES)
    with_litellm = interpreter_rss_mb(
        _WORKER_MODULES
        + "from vektra_core.providers.litellm_provider import LitellmProvider\n"
    )
    print(
        f"\nRSS idle worker: {rss:.0f} MiB (peak {hwm:.0f} MiB) after {SETTLE_S:g}s idle"
        f" [{sys.platform}]; worker modules alone {without:.0f} MiB; with litellm {with_litellm:.0f} MiB"
        f" (litellm costs {with_litellm - without:.0f} MiB)"
    )
    # The ceiling is the ARCH-073 figure for Linux. Elsewhere the number is recorded, not enforced.
    if ON_LINUX:
        assert hwm <= CEILING_MB, (
            f"idle worker peak RSS {hwm:.0f} MiB > {CEILING_MB} MiB"
        )
    else:
        assert rss <= 2 * CEILING_MB, (
            f"idle worker RSS {rss:.0f} MiB is implausibly large"
        )
