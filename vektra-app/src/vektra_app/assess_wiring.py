"""Composition of vektra_assess into the API process (plan assess-foundations 6.1).

``vektra_assess`` imports no other component; this module is where the API process
wires it in. Two rules:

- Dormant by default (6.1b): with ``VEKTRA_ASSESS_ENABLED=false`` the router is not
  mounted at all, so no ``/api/v1/assess`` path exists to be reached.
- Step 12 always runs the forbidden half of the key matrix, enabled or not: an API
  container holding the WS tokens or the KEK is refused even while assess is off
  (ARCH-066).
"""

from __future__ import annotations

import os
from pathlib import Path
from typing import Any

import structlog
from fastapi import FastAPI
from pydantic import ValidationError

from vektra_assess.config import VektraAssessSettings
from vektra_shared.registry import ProviderRegistry
from vektra_shared.startup import StartupValidationError

log = structlog.get_logger(__name__)

STEP = "assess_check"
API_ROLE = "vektra_assess_api"


# SQLAlchemy's own default overflow, made explicit: the connection budget counts it
# (docs/reference/configuration.md, test_connection_budget).
API_MAX_OVERFLOW = 10


def load_assess_settings() -> VektraAssessSettings:
    try:
        return VektraAssessSettings()
    except ValidationError as exc:
        raise StartupValidationError(
            step=STEP,
            detail=str(exc),
            remediation="Fix the VEKTRA_ASSESS_* variables (docs/reference/configuration.md).",
        ) from exc


def include_assess_router(app: FastAPI) -> bool:
    """Mount the assess router only when enabled. Returns whether it was mounted."""
    try:
        enabled = VektraAssessSettings().enabled
    except ValidationError:
        # A broken assess config never mounts routes; step 12 reports the error.
        enabled = False
    if not enabled:
        return False
    from vektra_assess.api.router import router

    app.include_router(router)
    return True


PARSER_SOCKET_DIR = Path("/run/vektra-assess-parser")


def assert_no_parser_surface(
    socket_path: Path, socket_dir: Path = PARSER_SOCKET_DIR
) -> None:
    """Dormant-capability guard (plan assess-parser): the API never sees the parser.

    The parser socket volume belongs to the parser and the worker only. An API container
    that has it mounted, or a ``parser.sock`` anywhere on the path, could talk to the
    parser; refuse to start. The image carries an empty ``/run/vektra-assess-parser`` (so a
    named volume initialises with the right owner); that bare directory is not a finding,
    a mount point or a socket in it is.
    """
    problems: list[str] = []
    for candidate in {socket_path, socket_dir / "parser.sock"}:
        if candidate.exists() or candidate.is_symlink():
            problems.append(f"{candidate} exists")
    for d in {socket_dir, socket_path.parent}:
        if os.path.ismount(d):
            problems.append(f"{d} is a mounted volume")
        elif d.is_dir() and any(d.iterdir()):
            problems.append(f"{d} is not empty")
    if problems:
        raise StartupValidationError(
            step=STEP,
            detail="the parser socket is reachable from the API container: "
            + "; ".join(sorted(problems)),
            remediation=(
                "Mount the assess-parser socket volume only into vektra-assess-parser "
                "and vektra-assess-worker (docker-compose.assess.yml), never the API."
            ),
        )


async def step_12_assess_check(
    app: FastAPI, main_database_url: str, registry: ProviderRegistry | None = None
) -> None:
    """ARCH-057 step 12: key matrix (always), then crypto, DSN and role (when enabled).

    Registers the ``("health", "assess")`` readiness check in both cases, so
    ``GET /health/assess`` reports ``disabled`` rather than "no check registered".
    """
    from vektra_assess.health import make_readiness_check
    from vektra_assess.keys import enforce_key_matrix

    settings = load_assess_settings()
    assert_no_parser_surface(settings.parser_socket)
    app.state.assess_enabled = settings.enabled
    if registry is not None:
        registry.register(
            "health",
            "assess",
            make_readiness_check(
                enabled=settings.enabled,
                get_engine=lambda: getattr(app.state, "assess_engine", None),
                max_age_s=settings.heartbeat_max_age_s,
            ),
        )
    if not settings.enabled:
        enforce_key_matrix(
            "api", secrets_dir=settings.secrets_dir, forbidden_only=True, step=STEP
        )
        log.info("startup_step", step=STEP, status="disabled")
        return

    from sqlalchemy.ext.asyncio import create_async_engine

    from vektra_assess.crypto.selftest import run_crypto_selftest
    from vektra_assess.db.roles import (
        assert_distinct_dsn,
        assert_session_role,
        dsn_with_password,
    )

    loaded: dict[str, Any] = enforce_key_matrix(
        "api", secrets_dir=settings.secrets_dir, step=STEP
    )
    run_crypto_selftest()
    if not settings.api_database_url:
        raise StartupValidationError(
            step=STEP,
            detail="VEKTRA_ASSESS_ENABLED=true but VEKTRA_ASSESS_API_DATABASE_URL is unset",
            remediation="Set the DSN of the vektra_assess_api role.",
        )
    password = loaded["assess_db_api_password"].value
    api_url = dsn_with_password(settings.api_database_url, password, step=STEP)
    assert_distinct_dsn(api_url, main_database_url, step=STEP)
    engine = create_async_engine(
        api_url,
        pool_size=settings.api_pool_size,
        max_overflow=API_MAX_OVERFLOW,
        pool_pre_ping=True,
    )
    try:
        await assert_session_role(engine, API_ROLE, step=STEP)
    except BaseException:
        await engine.dispose()
        raise
    app.state.assess_engine = engine
    app.state.assess_keys = loaded
    app.state.assess_settings = settings
    # assess-local limiters (plan assess-session-lifecycle): the dormant global per-key limiter
    # (app.state.rate_limiter) stays unset
    app.state.assess_limiters = build_assess_limiters()
    log.info("startup_step", step=STEP, status="ok", keys=sorted(loaded))


def build_assess_limiters() -> dict[str, Any]:
    """One assess-local ``RateLimiter`` per route family (plan 8 M5.3). The global per-key
    limiter (``app.state.rate_limiter``) stays unset: the assess routes never arm it."""
    from vektra_admin.rate_limit import RateLimiter
    from vektra_assess.api.state import LIMITER_NAMES

    return {name: RateLimiter() for name in LIMITER_NAMES}


def start_assess_background(app: FastAPI) -> bool:
    """Start the API's own background work once the startup sequence passed (plan 8 M4.3).

    Called at the end of the shared startup, so it runs both under ``main()`` (uvicorn with
    ``lifespan="off"``, BUG-025) and under the ASGI lifespan (tests, local uvicorn). Only with a
    discussion runner on ``app.state`` (assess enabled and step 13 passed). Returns whether the
    ``PendingTurnDriver`` was started."""
    if getattr(app.state, "assess_runner", None) is None:
        return False
    from vektra_assess.api.pending import PendingTurnDriver

    driver = PendingTurnDriver(app)
    app.state.assess_pending_driver = driver
    driver.start()
    log.info("assess_pending_driver_started")
    return True


async def stop_assess_background(app: FastAPI) -> None:
    driver = getattr(app.state, "assess_pending_driver", None)
    if driver is not None:
        await driver.stop()
        app.state.assess_pending_driver = None
