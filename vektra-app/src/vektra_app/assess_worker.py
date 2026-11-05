"""Composition root of the assess worker process (plan assess-foundations 6.2, COMP-003).

``docker/entrypoint.sh assess-worker`` runs ``python -m vektra_app.assess_worker``. The
worker is the only process holding the KEK, the WS tokens and the SMTP account, and it
has no listener. Before doing anything it proves its own boundaries, and exits non-zero
if any check fails:

1. assess is enabled (a dormant deployment must not run the worker at all);
2. the key matrix for the worker role (required present, forbidden absent);
3. the crypto self-test (no weaker fallback, ADR-0031);
4. its DSN is its own role, ``vektra_assess_worker``, and not a superuser;
5. the import ceiling: none of the ML stack, the vector store client or the ingest and
   index components is loaded (ARCH-073), measured after ``LitellmProvider`` is imported
   so its cost is part of the idle footprint.

Then it composes the job loop (plan assess-job-loop): policy and slot table, the control
connection with its NOTIFY self-test, startup reconciliation, the heartbeat, the claim loop and
the ticks, and runs until SIGTERM/SIGINT, when it drains, releases its leases and exits 0.
Production slots that have no handler yet make it refuse to start; only in development
(``VEKTRA_ASSESS_WORKER_ALLOW_UNBOUND=true``) it starts anyway, and a job of an unbound kind
then fails with ``handler_not_bound`` instead of completing silently.

This module must not import ``vektra_app.main``: that builds the whole API.
"""

from __future__ import annotations

import asyncio
import contextlib
import os
import socket
import sys
import uuid
from collections.abc import AsyncIterator, Iterable
from contextlib import asynccontextmanager
from dataclasses import dataclass
from importlib import metadata
from typing import Any

import structlog

from vektra_shared.startup import StartupValidationError

log = structlog.get_logger(__name__)

STEP = "assess_worker_check"
WORKER_ROLE = "vektra_assess_worker"
APPLICATION_NAME = "vektra-assess-worker"
# ``fastapi`` is deliberately not listed: ``litellm`` (the LLM provider the worker needs) imports
# ``fastapi.exceptions`` itself, so it is always present with it. What ARCH-073 keeps out is the
# Vektra API stack (``vektra_app.main``, ``vektra_learn``, ``uvicorn``), checked here and in the
# fresh-process test.
FORBIDDEN_MODULES: tuple[str, ...] = (
    "sentence_transformers",
    "torch",
    "transformers",
    "qdrant_client",
    "vektra_index",
    "vektra_ingest",
    "vektra_learn",
    "uvicorn",
    "presidio_analyzer",
    "presidio_anonymizer",
    "spacy",
    "unstructured",
)


class DisabledError(RuntimeError):
    """VEKTRA_ASSESS_ENABLED is false: the worker refuses to run."""


def assert_import_ceiling(loaded: Iterable[str] | None = None) -> None:
    modules = set(sys.modules if loaded is None else loaded)
    present = sorted(m for m in FORBIDDEN_MODULES if m in modules)
    if present:
        raise StartupValidationError(
            step=STEP,
            detail=f"forbidden modules loaded in the assess worker: {present}",
            remediation=(
                "The worker must not import the ML stack or other components "
                "(ARCH-073). Find the import chain with `python -X importtime`."
            ),
        )


def make_engine(url: str, pool_size: int, command_timeout_s: float = 30.0) -> Any:
    """The worker's single pool: it serves the job SQL and the handlers' transactions.

    Pool plus the control connection is the 5 + 1 of the connection budget (INT-002): no
    overflow, and a bounded wait for a free connection instead of opening a sixth one. The
    pools allow more concurrent jobs than there are connections (a handler holds one only while it
    runs a statement), so a handler may queue here; 15 s is long enough to ride out a burst and
    short enough to fail inside a lease.
    """
    from sqlalchemy.ext.asyncio import create_async_engine

    return create_async_engine(
        url,
        pool_size=pool_size,
        max_overflow=0,
        pool_timeout=15,
        pool_pre_ping=True,
        connect_args={
            "server_settings": {"application_name": APPLICATION_NAME},
            # a half-open connection (database restart, dropped network) answers nothing and
            # raises nothing: without these bounds every worker call would wait on it forever
            "timeout": 5,
            "command_timeout": command_timeout_s,
        },
    )


@dataclass(frozen=True, slots=True)
class Checked:
    engine: Any  # AsyncEngine
    settings: Any  # VektraAssessSettings
    worker_url: str


async def run_checks() -> object:
    """Run checks 1-5. Returns the worker engine (an AsyncEngine)."""
    return (await _checks()).engine


async def _checks() -> Checked:
    from vektra_assess.config import VektraAssessSettings
    from vektra_assess.crypto.selftest import run_crypto_selftest
    from vektra_assess.db.roles import (
        assert_distinct_dsn,
        assert_session_role,
        dsn_with_password,
    )
    from vektra_assess.keys import enforce_key_matrix

    settings = VektraAssessSettings()
    if not settings.enabled:
        raise DisabledError(
            "VEKTRA_ASSESS_ENABLED is not true: the assess worker does not run on a "
            "deployment where assess is disabled."
        )
    loaded = enforce_key_matrix("worker", secrets_dir=settings.secrets_dir, step=STEP)
    if settings.moodle_allow_http and settings.env != "development":
        raise StartupValidationError(
            step=STEP,
            detail=(
                "VEKTRA_ASSESS_MOODLE_ALLOW_HTTP=true is only honoured with "
                f"VEKTRA_ENV=development (this process runs with VEKTRA_ENV={settings.env})"
            ),
            remediation="Unset VEKTRA_ASSESS_MOODLE_ALLOW_HTTP; use https to Moodle.",
        )
    if settings.worker_allow_unbound and settings.env != "development":
        raise StartupValidationError(
            step=STEP,
            detail=(
                "VEKTRA_ASSESS_WORKER_ALLOW_UNBOUND=true is only honoured with "
                f"VEKTRA_ENV=development (this process runs with VEKTRA_ENV={settings.env})"
            ),
            remediation=(
                "Unset VEKTRA_ASSESS_WORKER_ALLOW_UNBOUND: a production worker must have "
                "every handler bound."
            ),
        )
    run_crypto_selftest()
    if not settings.worker_database_url:
        raise StartupValidationError(
            step=STEP,
            detail="VEKTRA_ASSESS_WORKER_DATABASE_URL is unset",
            remediation="Set the DSN of the vektra_assess_worker role.",
        )
    worker_url = dsn_with_password(
        settings.worker_database_url,
        loaded["assess_db_worker_password"].value,  # type: ignore[union-attr]
        step=STEP,
    )
    main_url = os.environ.get("VEKTRA_DATABASE_URL")
    if main_url:
        assert_distinct_dsn(worker_url, main_url, step=STEP)

    # Imported for its cost: the worker's LLM provider is injected from here later.
    from vektra_core.providers.litellm_provider import LitellmProvider  # noqa: F401

    assert_import_ceiling()

    engine = make_engine(
        worker_url, settings.worker_pool_size, settings.worker_db_command_timeout_s
    )
    try:
        await assert_session_role(engine, WORKER_ROLE, step=STEP)
    except BaseException:
        await engine.dispose()
        raise
    return Checked(engine, settings, worker_url)


def _asyncpg_dsn(sqlalchemy_url: str) -> str:
    """The control connection speaks asyncpg directly: drop the SQLAlchemy driver suffix."""
    return sqlalchemy_url.replace("postgresql+asyncpg://", "postgresql://", 1)


def _version() -> str:
    try:
        return metadata.version("vektra-app")
    except metadata.PackageNotFoundError:
        return "unknown"


def _compose(checked: Checked, bindings: Any | None = None) -> Any:
    """Build runtime, scheduler, heartbeat and control; nothing is started or connected yet."""
    from vektra_assess.worker.audit import make_audit
    from vektra_assess.worker.cleanup import cleanup_done_jobs
    from vektra_assess.worker.heartbeat import Heartbeat
    from vektra_assess.worker.listener import ControlConnection
    from vektra_assess.worker.policy import WorkerPolicyError, load_policy
    from vektra_assess.worker.reconcile import reap_exhausted
    from vektra_assess.worker.runtime import SqlStore, WorkerRuntime
    from vektra_assess.worker.slots import (
        Bindings,
        SlotsNotBound,
        assert_all_bound,
        build_slots,
    )
    from vektra_assess.worker.ticks import TickScheduler

    settings, engine = checked.settings, checked.engine
    try:
        policy = load_policy(os.environ)
    except WorkerPolicyError as exc:
        raise StartupValidationError(
            step=STEP,
            detail=str(exc),
            remediation="Fix the VEKTRA_ASSESS_JOB_* / VEKTRA_ASSESS_POOL_* settings.",
        ) from exc

    @asynccontextmanager
    async def acquire_raw() -> AsyncIterator[Any]:
        """An asyncpg connection from the SQLAlchemy pool: one pool, one budget."""
        async with engine.connect() as sa_conn:
            raw = await sa_conn.get_raw_connection()
            yield raw.driver_connection

    worker_id = f"{socket.gethostname()}-{os.getpid()}-{uuid.uuid4().hex[:6]}"
    audit = make_audit(engine.connect)
    runtime = WorkerRuntime(
        SqlStore(acquire_raw),
        worker_id=worker_id,
        pools=policy.pools,
        acquire_db=engine.connect,
        audit=audit,
        poll_interval_s=settings.job_poll_interval_s,
        backoff_base_s=settings.job_backoff_base_s,
        backoff_cap_s=settings.job_backoff_cap_s,
    )
    wiring: dict[
        str, Any
    ] = {}  # the scheduler needs the heartbeat, the heartbeat its stats

    async def on_lost() -> None:
        await wiring["scheduler"].cancel_running()

    control = ControlConnection(
        _asyncpg_dsn(checked.worker_url), on_notify=runtime.wake, on_lost=on_lost
    )
    heartbeat = Heartbeat(
        acquire_raw,
        worker_id=worker_id,
        version=_version(),
        file_path=settings.worker_heartbeat_file,
        stats=lambda: {**runtime.stats(), **wiring["scheduler"].stats()},
    )

    async def cleanup() -> None:
        await cleanup_done_jobs(acquire_raw, settings.job_done_retention_h)

    async def lease_reaper() -> None:
        await reap_exhausted(acquire_raw, audit)

    extra = bindings or Bindings(handlers={}, ticks={})
    slots = build_slots(
        policy,
        settings,
        Bindings(
            handlers=extra.handlers,
            ticks={
                "heartbeat": heartbeat.beat,
                "job_cleanup": cleanup,
                "lease_reaper": lease_reaper,
                **extra.ticks,
            },
        ),
    )
    try:
        assert_all_bound(slots, enabled=not settings.worker_allow_unbound)
    except SlotsNotBound as exc:
        raise StartupValidationError(
            step=STEP,
            detail=str(exc),
            remediation=(
                "Bind every handler (later plans of the assess feature), or for development "
                "only set VEKTRA_ASSESS_WORKER_ALLOW_UNBOUND=true with VEKTRA_ENV=development."
            ),
        ) from exc
    if settings.worker_allow_unbound and slots.unbound():
        log.warning("assess_worker_unbound_slots", slots=slots.unbound())
    for h in slots.handlers:
        runtime.register_handler(h)
    scheduler = TickScheduler(control, list(slots.ticks))
    wiring["scheduler"] = scheduler
    return runtime, scheduler, heartbeat, control, acquire_raw, audit


async def _serve(
    checked: Checked, stop: asyncio.Event, bindings: Any | None = None
) -> None:
    from vektra_assess.worker.listener import ControlConnectionError
    from vektra_assess.worker.reconcile import ReconcileError, reconcile
    from vektra_assess.worker.shutdown import shutdown
    from vektra_assess.worker.watchdog import watchdog

    runtime, scheduler, heartbeat, control, acquire_raw, audit = _compose(
        checked, bindings
    )
    # One deadline for everything before "ready": connecting, the NOTIFY self-test, reconciliation
    # and the first heartbeat. The watchdog only judges a worker that is already ready, so without
    # this a statement stuck on a half-open connection during startup would hang the process.
    startup_s = checked.settings.worker_wedge_exit_s
    startup_deadline = asyncio.timeout(startup_s)
    supervisor: asyncio.Task[None] | None = None
    wedge: asyncio.Task[None] | None = None
    startup_ticks: asyncio.Task[None] | None = None
    try:
        try:
            async with startup_deadline:
                try:
                    await control.start()  # refuses behind a transaction-mode pooler
                except ControlConnectionError as exc:
                    raise StartupValidationError(
                        step=STEP,
                        detail=str(exc),
                        remediation="Give the worker a direct or session-mode Postgres connection.",
                    ) from exc
                supervisor = asyncio.create_task(control.supervise())
                try:
                    # Startup ticks are NOT run inside the reconcile lock: a slow one (the setup
                    # check makes about twenty live Moodle calls) would hold the lock, delay the
                    # first heartbeat and the claim loop, trip the container healthcheck and
                    # ignore SIGTERM.
                    await reconcile(control, acquire_raw, audit)
                except ReconcileError as exc:
                    raise StartupValidationError(
                        step=STEP,
                        detail=str(exc),
                        remediation="Stop the other worker or wait for its reconciliation to finish.",
                    ) from exc
                await heartbeat.beat()
                heartbeat.set_state("ready")
                await heartbeat.beat()
        except TimeoutError:
            if not startup_deadline.expired():
                raise  # an inner timeout (connect, statement): keep its own cause
            raise StartupValidationError(
                step=STEP,
                detail=f"startup did not complete within {startup_s:g} s",
                remediation=(
                    "The database is unreachable or not answering. Check the worker DSN and the "
                    "network; the supervisor restarts the worker."
                ),
            ) from None
        scheduler.start()
        startup_ticks = asyncio.create_task(scheduler.run_startup_ticks())
        run_task = asyncio.create_task(runtime.run())
        wedge = asyncio.create_task(
            watchdog(heartbeat, stop, exit_after_s=checked.settings.worker_wedge_exit_s)
        )
        log.info("assess_worker_started", role=WORKER_ROLE, worker_id=runtime.worker_id)
        await stop.wait()
        released = await shutdown(
            runtime=runtime,
            run_task=run_task,
            scheduler=scheduler,
            heartbeat=heartbeat,
            control=control,
            drain_s=checked.settings.worker_drain_s,
        )
        log.info("assess_worker_stopped", released=released)
    finally:
        for background in (wedge, startup_ticks):
            if background is not None:
                background.cancel()
        if supervisor is not None:
            supervisor.cancel()
            with contextlib.suppress(asyncio.CancelledError, Exception):
                await supervisor
        with contextlib.suppress(Exception):
            async with asyncio.timeout(3):
                await control.stop()


def check_imports() -> None:
    """``--check-imports``: import the whole composition and assert the ceiling, no DB or keys."""
    import importlib

    importlib.import_module(
        "vektra_core.providers.litellm_provider"
    )  # the LLM provider's cost
    for name in (
        "audit", "cleanup", "heartbeat", "listener", "policy", "reconcile",
        "runtime", "shutdown", "slots", "ticks", "watchdog",
    ):  # fmt: skip
        importlib.import_module(f"vektra_assess.worker.{name}")
    assert_import_ceiling()
    print("ceiling ok")


async def _amain(bindings: Any | None = None) -> int:
    from vektra_assess.config import VektraAssessSettings
    from vektra_assess.worker.shutdown import install_signal_handlers

    # A heartbeat left by a previous run must not vouch for this one.
    with contextlib.suppress(FileNotFoundError):
        VektraAssessSettings().worker_heartbeat_file.unlink()
    try:
        checked = await _checks()
    except DisabledError as exc:
        print(f"[ASSESS WORKER] {exc}", file=sys.stderr)
        return 2
    except StartupValidationError as exc:
        print(exc.to_plain_text(), file=sys.stderr)
        return 1

    stop = asyncio.Event()
    remove_handlers = install_signal_handlers(stop)
    try:
        await _serve(checked, stop, bindings)
    except StartupValidationError as exc:
        print(exc.to_plain_text(), file=sys.stderr)
        return 1
    finally:
        remove_handlers()
        # Bounded: a pooled connection with a pending query cancel can make ``dispose`` wait on a
        # silent peer without limit, and the process would then outlive its own shutdown.
        with contextlib.suppress(Exception):
            async with asyncio.timeout(5):
                await checked.engine.dispose()
    return 0


def main(bindings: Any | None = None) -> None:
    """Entry point. ``bindings`` is a test seam: the chaos harness binds its own bodies to
    production slots; ``python -m vektra_app.assess_worker`` always runs with ``None``."""
    if "--check-imports" in sys.argv[1:]:
        check_imports()
        raise SystemExit(0)
    # JSON on stderr with the assess allowlist on structlog AND stdlib records (plan
    # assess-context A): before this the worker's stdlib INFO lines went nowhere.
    from vektra_shared.log_safety import configure_assess_worker_logging

    configure_assess_worker_logging()
    raise SystemExit(asyncio.run(_amain(bindings)))


if __name__ == "__main__":
    main()
