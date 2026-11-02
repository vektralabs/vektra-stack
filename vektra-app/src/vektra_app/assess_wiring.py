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
        api_url, pool_size=settings.api_pool_size, pool_pre_ping=True
    )
    try:
        await assert_session_role(engine, API_ROLE, step=STEP)
    except BaseException:
        await engine.dispose()
        raise
    app.state.assess_engine = engine
    app.state.assess_keys = loaded
    log.info("startup_step", step=STEP, status="ok", keys=sorted(loaded))
