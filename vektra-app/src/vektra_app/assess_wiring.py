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

from vektra_app.assess_switch import assess_enabled
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
        from vektra_assess.config import explain_settings_error

        detail, remediation = explain_settings_error(exc)
        raise StartupValidationError(
            step=STEP, detail=detail, remediation=remediation
        ) from None


def include_assess_router(app: FastAPI) -> bool:
    """Mount the assess router only when enabled. Returns whether it was mounted. A broken
    assess config never mounts routes (``assess_enabled`` is False); step 12 reports it."""
    if not assess_enabled():
        return False
    from vektra_assess.api.router import router
    from vektra_assess.ui.router import router as ui_router

    app.include_router(router)
    app.include_router(
        ui_router
    )  # the discussion UI page (plan 9), dormant with assess
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
    from vektra_app.lms_providers import installed_provider_secrets
    from vektra_assess.health import make_readiness_check
    from vektra_assess.keys import enforce_key_matrix

    settings = load_assess_settings()
    # ARCH-066 (review of #207, finding 1): the secrets every installed LMS provider declares
    # on its entry point, read without building it, are refused to the API in the environment
    # too, assess enabled or not
    provider_secrets = installed_provider_secrets()
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
                keys_present=lambda: bool(getattr(app.state, "assess_keys", None)),
            ),
        )
    if not settings.enabled:
        enforce_key_matrix(
            "api",
            secrets_dir=settings.secrets_dir,
            forbidden_only=True,
            step=STEP,
            provider_secrets=provider_secrets,
        )
        log.info("startup_step", step=STEP, status="disabled")
        return

    from sqlalchemy.ext.asyncio import create_async_engine

    from vektra_assess.api.site_binding import install_site_binding
    from vektra_assess.config import erasure_mode_problem
    from vektra_assess.crypto.selftest import run_crypto_selftest
    from vektra_assess.db.roles import (
        assert_distinct_dsn,
        assert_session_role,
        dsn_with_password,
    )

    problem = erasure_mode_problem(settings)
    if problem is not None:
        raise StartupValidationError(
            step=STEP,
            detail=problem,
            remediation=(
                "Set VEKTRA_ASSESS_ERASURE_MODE to the DPO's decision (erase, the default "
                "behaviour of OQ-033, or retain_exam_evidence), docs/reference/configuration.md."
            ),
        )
    loaded: dict[str, Any] = enforce_key_matrix(
        "api",
        secrets_dir=settings.secrets_dir,
        step=STEP,
        provider_secrets=provider_secrets,
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
    # ADR-0033 D9.2: every transaction is bound to the request's assess site at its start (the
    # routes also install it on first use; here it is on before any request)
    install_site_binding(engine)
    try:
        await assert_session_role(engine, API_ROLE, step=STEP)
        await check_ui_origin(engine, settings)
    except BaseException:
        await engine.dispose()
        raise
    app.state.assess_engine = engine
    app.state.assess_keys = loaded
    app.state.assess_settings = settings
    # assess-local limiters (plan assess-session-lifecycle): the dormant global per-key limiter
    # (app.state.rate_limiter) stays unset
    app.state.assess_limiters = build_assess_limiters()
    start_metrics_refresher(app, engine)
    log.info("startup_step", step=STEP, status="ok", keys=sorted(loaded))


async def stop_metrics_refresher(app: FastAPI) -> None:
    """Shutdown: cancel the refresh and WAIT for it, so no query runs on a disposed engine."""
    import asyncio
    import contextlib

    task = getattr(app.state, "assess_metrics_task", None)
    if task is None:
        return
    task.cancel()
    with contextlib.suppress(asyncio.CancelledError):
        await task


def start_metrics_refresher(app: FastAPI, engine: Any) -> None:
    """Plan assess-privacy-retention 12b: the content-free assess series on ``/metrics``,
    read from ``assess_metrics_snapshot()`` by a background refresh (a scrape never waits on
    the database). Cancelled at shutdown before the engine is disposed."""
    import asyncio

    from vektra_assess.metrics import SnapshotCache, install

    cache = SnapshotCache()
    install(cache)
    app.state.assess_metrics_cache = cache
    app.state.assess_metrics_task = asyncio.get_running_loop().create_task(
        cache.run(engine), name="assess-metrics-refresh"
    )


async def check_ui_origin(engine: Any, settings: VektraAssessSettings) -> None:
    """Plan 9 (ARCH-076): refuse to start when ``VEKTRA_ASSESS_UI_ORIGIN`` is unset outside
    development, not https outside development, or equal to a registered site's LMS origin
    (the iframe's ``allow-same-origin`` sandbox is safe only across distinct origins). In
    development an unset origin starts; the UI page then answers 503 until it is set."""
    if not settings.ui_origin and settings.is_development:
        return
    from sqlalchemy import text

    from vektra_assess.ui.origins import (
        SITE_ORIGINS_SQL,
        site_origin_or_problem,
        ui_origin_problem,
    )

    async with engine.connect() as conn:
        rows = (await conn.execute(text(SITE_ORIGINS_SQL))).all()
    origins: list[str] = []
    for site_id, lms_origin, base_url in rows:
        origin, site_problem = site_origin_or_problem(lms_origin, base_url)
        if site_problem is not None:
            # the SITE is misconfigured, not the UI origin
            raise StartupValidationError(
                step=STEP,
                detail=f"assess site {site_id} refused: {site_problem}",
                remediation=(
                    "Set assess_sites.lms_origin of that site to its bare LMS origin "
                    "(https://host[:port]), or disable the site (assess_sites.disabled_at)."
                ),
            )
        if origin and not settings.is_development and not origin.startswith("https://"):
            # also the SITE's problem: its LMS origin is not https
            raise StartupValidationError(
                step=STEP,
                detail=f"assess site {site_id} refused: lms_origin_not_https",
                remediation=(
                    "Serve that LMS site over https and set assess_sites.lms_origin to "
                    "its https origin, or disable the site (assess_sites.disabled_at)."
                ),
            )
        if origin:
            origins.append(origin)
    problem = ui_origin_problem(
        settings.ui_origin, origins, development=settings.is_development
    )
    if problem is not None:
        raise StartupValidationError(
            step=STEP,
            detail=f"VEKTRA_ASSESS_UI_ORIGIN refused: {problem}",
            remediation=(
                "Serve the discussion UI on its own https origin, distinct from every LMS "
                "site's origin (docs/reference/configuration.md)."
            ),
        )


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
