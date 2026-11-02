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

This stub then refreshes a heartbeat file every 10 s until SIGTERM (stopping well inside
the 30 s grace period). Plan assess-job-loop replaces the loop with the real job loop and
keeps every check above.

This module must not import ``vektra_app.main``: that builds the whole API.
"""

from __future__ import annotations

import asyncio
import contextlib
import os
import signal
import sys
from collections.abc import Iterable
from pathlib import Path
from typing import Any

import structlog

from vektra_shared.startup import StartupValidationError

log = structlog.get_logger(__name__)

STEP = "assess_worker_check"
WORKER_ROLE = "vektra_assess_worker"
HEARTBEAT_INTERVAL_S = 10.0
FORBIDDEN_MODULES: tuple[str, ...] = (
    "sentence_transformers",
    "torch",
    "transformers",
    "qdrant_client",
    "vektra_index",
    "vektra_ingest",
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


async def run_checks() -> object:
    """Run checks 1-5. Returns the worker engine (an AsyncEngine)."""
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

    from sqlalchemy.ext.asyncio import create_async_engine

    engine = create_async_engine(
        worker_url,
        pool_size=settings.worker_pool_size,
        pool_pre_ping=True,
    )
    try:
        await assert_session_role(engine, WORKER_ROLE, step=STEP)
    except BaseException:
        await engine.dispose()
        raise
    return engine


# a hung database must not delay the file heartbeat that the container healthcheck reads
DB_BEAT_TIMEOUT_S = 5.0
_BEAT_SQL = (
    "INSERT INTO assess_heartbeat (component, beat_at) VALUES ('worker', now()) "
    "ON CONFLICT (component) DO UPDATE SET beat_at = now()"
)


async def _beat_db(engine: Any) -> None:
    """Record the beat the API's /health/assess reads. A failure is logged, never fatal:
    the file heartbeat (the container healthcheck) keeps working without the database."""
    from sqlalchemy import text

    try:
        async with asyncio.timeout(DB_BEAT_TIMEOUT_S), engine.begin() as conn:
            await conn.execute(text(_BEAT_SQL))
    except Exception as exc:
        log.warning("assess_worker_heartbeat_db_failed", error=type(exc).__name__)


async def heartbeat_loop(
    path: Path,
    stop: asyncio.Event,
    interval_s: float = HEARTBEAT_INTERVAL_S,
    engine: Any | None = None,
) -> None:
    """Touch ``path`` (and the heartbeat row, when ``engine`` is given) every
    ``interval_s`` until ``stop`` is set."""
    while not stop.is_set():
        path.touch()
        if engine is not None:
            await _beat_db(engine)
        try:
            await asyncio.wait_for(stop.wait(), timeout=interval_s)
        except TimeoutError:
            continue


async def _amain() -> int:
    from vektra_assess.config import VektraAssessSettings

    # A heartbeat left by a previous run must not vouch for this one.
    with contextlib.suppress(FileNotFoundError):
        VektraAssessSettings().worker_heartbeat_file.unlink()
    try:
        engine = await run_checks()
    except DisabledError as exc:
        print(f"[ASSESS WORKER] {exc}", file=sys.stderr)
        return 2
    except StartupValidationError as exc:
        print(exc.to_plain_text(), file=sys.stderr)
        return 1

    stop = asyncio.Event()
    loop = asyncio.get_running_loop()
    for sig in (signal.SIGTERM, signal.SIGINT):
        loop.add_signal_handler(sig, stop.set)
    log.info("assess_worker_started", role=WORKER_ROLE)
    try:
        await heartbeat_loop(
            VektraAssessSettings().worker_heartbeat_file, stop, engine=engine
        )
    finally:
        await engine.dispose()  # type: ignore[attr-defined]
        log.info("assess_worker_stopped")
    return 0


def main() -> None:
    raise SystemExit(asyncio.run(_amain()))


if __name__ == "__main__":
    main()
