"""Test-only composition of the assess worker for the chaos harness (plan assess-job-loop D).

It runs the REAL composition (``vektra_app.assess_worker``: policy, slots, control connection,
reconciliation, heartbeat, drain) and only supplies what production deliberately leaves unbound:
a chaos body for the ``purge`` slot. The body lives in this file under ``tests/``, so it can never
be in the production image's composition (the slot table rejects names it does not know, and the
production entry passes no bindings).

Job args: ``n`` (the job's reference), ``sleep_ms`` (time spent "working"). Before sleeping the
body touches ``$CHAOS_DIR/started-<job id>-<attempt>`` so the harness can kill the process at a
chosen moment; after sleeping it writes its effect idempotently into ``assess_chaos_effects``
(``runs`` counts executions: a job re-run after ``kill -9`` shows ``runs`` > 1 instead of failing).
"""

from __future__ import annotations

import asyncio
import os
from pathlib import Path

from sqlalchemy import text

from vektra_app import assess_worker
from vektra_assess.jobs import JobKind
from vektra_assess.worker.runner import JobContext
from vektra_assess.worker.slots import Bindings

_UPSERT = text(
    "INSERT INTO assess_chaos_effects (job_n, runs) VALUES (:n, 1) "
    "ON CONFLICT (job_n) DO UPDATE SET runs = assess_chaos_effects.runs + 1"
)


async def chaos_stall(ctx: JobContext) -> None:
    """A handler that never returns on its own (a stalled Moodle call): it holds a pool slot until
    its hard timeout cancels it. Used to saturate the ``ws`` pool."""
    directory = Path(os.environ["CHAOS_DIR"])
    (directory / f"stalled-{ctx.job_id}-{ctx.attempt}").touch()
    await asyncio.sleep(3600)


async def chaos_effect(ctx: JobContext) -> None:
    directory = Path(os.environ["CHAOS_DIR"])
    (directory / f"started-{ctx.job_id}-{ctx.attempt}").touch()
    await asyncio.sleep(int(ctx.args.get("sleep_ms", 0)) / 1000)
    async with ctx.acquire() as conn:
        await conn.execute(_UPSERT, {"n": int(ctx.args["n"])})
        await conn.commit()


async def _stub(*_: object) -> None:
    """A body that does nothing: only for measuring the idle footprint, never for behaviour."""


def _bindings() -> Bindings:
    if os.environ.get("CHAOS_BIND_ALL") == "1":
        # Every production slot bound to a stub, so the worker starts WITHOUT the development
        # override (the real fail-closed path) and idles like a fully bound worker (RSS test).
        from vektra_assess.worker.slots import TICK_OWNERS

        framework = {"heartbeat", "job_cleanup"}  # bound by the composition itself
        return Bindings(
            handlers={k: _stub for k in JobKind},  # type: ignore[misc]
            ticks={n: _stub for n in TICK_OWNERS if n not in framework},  # type: ignore[misc]
        )
    handlers = {JobKind.PURGE: chaos_effect}
    if os.environ.get("CHAOS_STALL_WS") == "1":
        handlers[JobKind.GATE_REFRESH] = chaos_stall  # type: ignore[assignment]
    return Bindings(handlers=handlers, ticks={})  # type: ignore[arg-type]


if __name__ == "__main__":
    forbidden = os.environ.get("CHAOS_IMPORT_FORBIDDEN")
    if forbidden:
        # simulates something importing a forbidden module: the import ceiling must refuse to start
        import sys
        import types

        sys.modules[forbidden] = types.ModuleType(forbidden)
    assess_worker.main(_bindings())
